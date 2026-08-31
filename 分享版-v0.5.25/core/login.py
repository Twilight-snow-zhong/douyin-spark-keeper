"""网页端扫码登录：后台无头浏览器打开抖音登录页，把二维码截图返回给网页，轮询登录态。

用 Playwright 异步 API：所有登录相关接口都是 async 端点，跑在 uvicorn 同一个事件循环里，
避免 sync API 跨线程使用问题。
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from pathlib import Path

from playwright.async_api import async_playwright

from . import browsers
from .automation import _CHROMIUM_ARGS

logger = logging.getLogger("douyin-spark")

LOGIN_URL = "https://www.douyin.com/"
# 二维码候选选择器（抖音改版频繁，多备几个；会在所有 frame 里查找）
QR_SELECTORS = (
    "#animate_qrcode_container",
    "#login-qrcode",
    "[class*='qrcode']",
    "[class*='Qrcode']",
    "img[src*='qr']",
    "img[src*='qrcode']",
    "canvas",
)
MAX_WAIT = 300  # 扫码超时（秒）

_active: dict[str, dict] = {}
_lock = threading.Lock()

GENERIC_NAMES = ("未命名账号", "默认账号", "新账号")


async def _find_qr_locator(page):
    """在所有 frame 里找二维码元素，返回 (locator, 说明) 或 (None, '')。"""
    for f in page.frames:
        for sel in QR_SELECTORS:
            try:
                loc = f.locator(sel)
                n = await loc.count()
                for i in range(min(n, 8)):
                    try:
                        if await loc.nth(i).is_visible():
                            return loc.nth(i), f"{sel}[{i}] @ {f.url[:60]}"
                    except Exception:
                        continue
            except Exception:
                continue
    return None, ""


async def _close(acc_id: str) -> None:
    sess = _active.pop(acc_id, None)
    if not sess:
        return
    try:
        await sess["browser"].close()
    except Exception:
        pass
    try:
        await sess["p"].stop()
    except Exception:
        pass


async def _cleanup_all() -> None:
    for k in list(_active.keys()):
        await _close(k)


_ENGINES = ("chromium", "firefox", "webkit")


async def _open_login_popup(page) -> bool:
    """确保抖音登录弹窗出现。返回是否检测到二维码。"""
    # 1) 先等自动弹出的登录框（最多约 7.5 秒），跨 frame 找二维码
    for _ in range(5):
        loc, _ = await _find_qr_locator(page)
        if loc is not None:
            return True
        await page.wait_for_timeout(1500)
    # 2) 依次尝试点击常见登录入口
    strategies = [
        page.get_by_text("登录", exact=True).first,
        page.get_by_text("立即登录", exact=False).first,
        page.locator('[class*="login-btn"]').first,
        page.locator('[class*="avatar"]').first,
        page.locator('a[href*="passport"]').first,
        page.locator('[class*="header"] img').first,
    ]
    for el in strategies:
        try:
            if await el.count():
                await el.click(timeout=3000)
                await page.wait_for_timeout(3000)
                loc, _ = await _find_qr_locator(page)
                if loc is not None:
                    return True
        except Exception:
            continue
    # 3) 兜底：弹窗出现后点「扫码登录」页签（默认可能停在手机号登录）
    try:
        tabs = page.get_by_text("扫码登录", exact=False)
        for i in range(await tabs.count()):
            try:
                await tabs.nth(i).click(timeout=2000)
                await page.wait_for_timeout(2500)
                loc, _ = await _find_qr_locator(page)
                if loc is not None:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def _qr_image_of(page) -> tuple[str | None, str]:
    """级联抓取二维码图片。返回 (data_url, 说明)。"""
    # 1) 跨 frame 找二维码元素并直接截图（优先）
    loc, how = await _find_qr_locator(page)
    if loc is not None:
        try:
            data = await loc.screenshot(timeout=10000)
            return "data:image/png;base64," + base64.b64encode(data).decode(), f"二维码元素: {how}"
        except Exception:
            pass
    # 2) 含「扫码登录」文字的弹窗区域（截图其周边区域，主 frame）
    try:
        txt = page.get_by_text("扫码登录", exact=False).first
        if await txt.count():
            box = await txt.bounding_box()
            if box:
                clip = {
                    "x": max(0, box["x"] - 60),
                    "y": max(0, box["y"] - 200),
                    "width": 480,
                    "height": 620,
                }
                data = await page.screenshot(clip=clip, timeout=10000)
                return "data:image/png;base64," + base64.b64encode(data).decode(), f"弹窗区域 @ {page.url[:60]}"
    except Exception:
        pass
    # 3) 整页兜底
    data = await page.screenshot(timeout=10000)
    return "data:image/png;base64," + base64.b64encode(data).decode(), f"整页兜底 @ {page.url[:60]}"


async def start_login(acc_id: str, engine: str = "chromium", mode: str = "dialog") -> tuple[bool, str]:
    """启动一个扫码登录会话。返回 (是否成功, 错误信息或空)。

    mode: "dialog" 无头（二维码显示在网页弹窗里）；"window" 弹出真实浏览器窗口（最稳）。
    engine: 内置引擎 chromium/firefox/webkit，或本机 Chromium 系浏览器的 exe 路径
            （如 Edge/Chrome/夸克，通过 chromium 引擎 + executable_path 驱动）。
    """
    path_engine = None
    if browsers.looks_like_browser_path(engine):
        path_engine = engine
        engine = "chromium"
    engine = engine if engine in _ENGINES else "chromium"
    mode = mode if mode in ("dialog", "window") else "dialog"
    with _lock:
        sess = _active.get(acc_id)
        if sess and not sess.get("done"):
            return False, "该账号已有进行中的扫码登录，请先取消"
        await _cleanup_all()
        try:
            p = await async_playwright().start()
            launcher = getattr(p, engine)
            kwargs: dict = {"headless": mode == "dialog"}
            if engine == "chromium":
                args = list(_CHROMIUM_ARGS)
                if mode == "dialog":
                    # 无头模式降低自动化识别概率
                    args += ["--disable-blink-features=AutomationControlled"]
                kwargs["args"] = args
            if path_engine:
                kwargs["executable_path"] = path_engine
            browser = await launcher.launch(**kwargs)
            ctx_kwargs: dict = {
                "viewport": {"width": 1000, "height": 800},
                "locale": "zh-CN",
                "timezone_id": "Asia/Shanghai",
            }
            if engine == "chromium":
                ctx_kwargs["user_agent"] = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                )
            context = await browser.new_context(**ctx_kwargs)
            if engine == "chromium":
                try:
                    # 隐藏自动化标记，降低无头识别概率
                    await context.add_init_script(
                        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                    )
                except Exception:
                    pass
            page = await context.new_page()
            await page.goto(LOGIN_URL, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(4000)
            await _open_login_popup(page)
            _active[acc_id] = {
                "p": p, "browser": browser, "context": context, "page": page,
                "engine": engine, "mode": mode, "at": time.time(), "done": False,
                "sent_code": False,
            }
            logger.info(
                "【%s】扫码登录会话已启动（%s, %s）", acc_id,
                path_engine or engine, mode,
            )
            return True, ""
        except Exception as e:
            msg = str(e)[:100]
            if engine != "chromium" and ("executable" in msg.lower() or "doesn't exist" in msg.lower()):
                msg += "（未安装该浏览器，请运行 python -m playwright install firefox webkit）"
            elif path_engine and ("executable" in msg.lower() or "doesn't exist" in msg.lower()):
                msg += "（浏览器路径无效或版本不兼容，请检查路径）"
            if mode == "window" and "display" in msg.lower():
                msg += "（弹出窗口模式需要图形界面，服务器上请改用网页内二维码模式）"
            logger.error("【%s】启动扫码登录失败: %s", acc_id, msg)
            return False, f"启动失败: {msg}"


async def full_screenshot(acc_id: str) -> tuple[str | None, str]:
    """全视口截图（1000x800，手动操作模式用：看得全、坐标准）。"""
    sess = _active.get(acc_id)
    if not sess or sess.get("done"):
        return None, "无进行中的登录会话"
    try:
        data = await sess["page"].screenshot(timeout=10000)
        return "data:image/png;base64," + base64.b64encode(data).decode(), f"全屏 @ {sess['page'].url[:60]}"
    except Exception:
        return None, "截图失败"


async def qr_image(acc_id: str) -> tuple[str | None, str]:
    """返回二维码 PNG 的 data URL 及抓取方式说明；拿不到返回 (None, 说明)。"""
    sess = _active.get(acc_id)
    if not sess or sess.get("done"):
        return None, "无进行中的登录会话"
    try:
        return await _qr_image_of(sess["page"])
    except Exception:
        return None, "截图失败"


async def _verify_modal(page):
    """检测二次验证弹窗。返回 (类型, 验证码输入框 locator 或 None)。
    类型: 'sms'=短信验证码  'slider'=滑块/安全验证  None=无。
    """
    for f in page.frames:
        # 滑块/安全验证特征
        try:
            for sel in ("#captcha_container", "[class*='captcha']", "[class*='secsdk']", "[class*='verify']"):
                loc = f.locator(sel)
                if await loc.count() and await loc.first.is_visible():
                    return "slider", None
        except Exception:
            pass
        try:
            txt = f.get_by_text("安全验证", exact=False).first
            if await txt.count() and await txt.is_visible():
                return "slider", None
        except Exception:
            pass
        # 短信验证码特征：可见的「验证码」文字 + 附近输入框
        try:
            txt = f.get_by_text("验证码", exact=False).first
            if await txt.count() and await txt.is_visible():
                for sel in ("input[placeholder*='验证码']", "input[placeholder*='验证']",
                            "input[type='number']", "input[type='text']", "input:not([type])"):
                    inp = f.locator(sel).first
                    if await inp.count() and await inp.is_visible():
                        return "sms", inp
                return "sms", None
        except Exception:
            pass
    return None, None


async def verify_state(acc_id: str) -> dict:
    """查询当前是否需要二次验证。"""
    sess = _active.get(acc_id)
    if not sess or sess.get("done"):
        return {"need_verify": False, "type": None}
    try:
        vtype, _ = await _verify_modal(sess["page"])
        return {"need_verify": bool(vtype), "type": vtype}
    except Exception:
        return {"need_verify": False, "type": None}


async def submit_verify_code(acc_id: str, code: str) -> tuple[bool, str]:
    """把用户输入的验证码填入无头页面并提交。返回 (是否成功, 说明)。"""
    code = (code or "").strip()
    sess = _active.get(acc_id)
    if not sess or sess.get("done"):
        return False, "没有进行中的登录会话，请重新扫码"
    if not code:
        return False, "验证码不能为空"
    try:
        vtype, inp = await _verify_modal(sess["page"])
        # 输入框没匹配到时，扩大搜索：任意可见的文本/数字输入框
        if inp is None:
            for f in sess["page"].frames:
                for sel in ("input[type='text']", "input[type='number']", "input:not([type])",
                            "input[placeholder]", "input"):
                    try:
                        cand = f.locator(sel).first
                        if await cand.count() and await cand.is_visible():
                            inp = cand
                            break
                    except Exception:
                        continue
                if inp is not None:
                    break
        if inp is None:
            return False, "未检测到验证码输入框（可改用「手动操作」：点画面里的输入框 → 输入 → 点确定）"
        await inp.fill(code)
        # 点确认类按钮：扫所有 frame 的所有可见匹配（不只第一个），找不到再回车
        clicked = False
        for f in sess["page"].frames:
            for txt in ("确定", "确认", "下一步", "提交", "完成"):
                try:
                    btns = f.locator(f"button:has-text('{txt}')")
                    n = await btns.count()
                    for i in range(min(n, 5)):
                        try:
                            if await btns.nth(i).is_visible():
                                await btns.nth(i).click(timeout=2000)
                                clicked = True
                                break
                        except Exception:
                            continue
                    if clicked:
                        break
                except Exception:
                    continue
            if clicked:
                break
        if not clicked:
            # 也试试非 button 元素上的文字（抖音可能是 div 按钮）
            for f in sess["page"].frames:
                for txt in ("确定", "确认", "下一步"):
                    try:
                        el = f.get_by_text(txt, exact=True).first
                        if await el.count() and await el.is_visible():
                            await el.click(timeout=2000)
                            clicked = True
                            break
                    except Exception:
                        continue
                if clicked:
                    break
        if not clicked:
            await inp.press("Enter")
        logger.info("【%s】已提交二次验证码", acc_id)
        return True, "验证码已提交，等待验证…"
    except Exception as e:
        return False, f"提交失败: {str(e)[:80]}"


async def resend_verify_code(acc_id: str) -> tuple[bool, str]:
    """点击「获取/重新获取验证码」按钮（匹配任意可见元素，含倒计时文案）。"""
    sess = _active.get(acc_id)
    if not sess or sess.get("done"):
        return False, "没有进行中的登录会话，请重新扫码"
    candidates = ("获取验证码", "重新获取", "重新发送", "发送验证码", "再次发送", "点击获取", "发送短信验证码")
    try:
        for f in sess["page"].frames:
            for txt in candidates:
                try:
                    el = f.get_by_text(txt, exact=False).first
                    if await el.count() and await el.is_visible():
                        await el.click(timeout=2000)
                        return True, f"已点击「{txt}」，请查收手机验证码"
                except Exception:
                    continue
        return True, "未找到发送按钮，验证码可能已自动发送——请查收手机；没收到可稍后重试，或手动点画面里的按钮"
    except Exception as e:
        return False, f"操作失败: {str(e)[:80]}"


async def manual_scroll(acc_id: str, dy: float) -> tuple[bool, str]:
    """远程手动操作：滚动页面（正数向下，负数向上）。用 JS 滚窗口 + 验证容器，比鼠标滚轮可靠。"""
    sess = _active.get(acc_id)
    if not sess or sess.get("done"):
        return False, "没有进行中的登录会话"
    try:
        dy = float(dy)
        for f in sess["page"].frames[:3]:
            try:
                await f.evaluate(
                    """(dy) => {
                        window.scrollBy(0, dy);
                        const sels = ["#captcha_container", "[class*='captcha']", "[class*='verify']", "[class*='slider']"];
                        for (const s of sels) {
                            document.querySelectorAll(s).forEach((el) => {
                                if (el.scrollHeight > el.clientHeight) el.scrollTop += dy;
                            });
                        }
                    }""",
                    dy,
                )
            except Exception:
                continue
        return True, "已滚动"
    except Exception as e:
        return False, f"滚动失败: {str(e)[:80]}"


async def manual_drag(acc_id: str, x1: float, y1: float, x2: float, y2: float) -> tuple[bool, str]:
    """远程模拟拖动（滑块验证用）：从 (x1,y1) 按住拖到 (x2,y2)，带人类化轨迹。"""
    sess = _active.get(acc_id)
    if not sess or sess.get("done"):
        return False, "没有进行中的登录会话"
    try:
        page = sess["page"]
        await page.mouse.move(float(x1), float(y1))
        await page.mouse.down()
        dist = float(x2) - float(x1)
        steps = max(18, min(35, int(abs(dist) / 8)))
        for i in range(1, steps + 1):
            progress = i / steps
            eased = progress * progress * (3 - 2 * progress)  # ease-in-out
            x = float(x1) + dist * eased
            y = float(y1) + __import__("random").uniform(-1.5, 1.5)  # 轻微上下抖动
            await page.mouse.move(x, y, steps=1)
            await page.wait_for_timeout(__import__("random").randint(8, 25))
        await page.mouse.up()
        logger.info("【%s】已模拟拖动滑块", acc_id)
        return True, "已模拟拖动，等待结果…"
    except Exception as e:
        return False, f"拖动失败: {str(e)[:80]}"


async def manual_click(acc_id: str, x: float, y: float) -> tuple[bool, str]:
    """远程手动操作：在页面指定坐标点击（解决自动点击匹配不到的问题）。"""
    sess = _active.get(acc_id)
    if not sess or sess.get("done"):
        return False, "没有进行中的登录会话"
    try:
        await sess["page"].mouse.click(float(x), float(y))
        return True, "已点击"
    except Exception as e:
        return False, f"点击失败: {str(e)[:80]}"


async def _ensure_input_focus(page) -> None:
    """确保页面当前有聚焦的输入框：没有就自动点验证码/任意可见输入框。"""
    try:
        tag = await page.evaluate(
            "() => { const el = document.activeElement; return el ? (el.tagName + '|' + (el.getAttribute('type') || '')) : ''; }"
        )
        if tag.lower().startswith(("input|", "textarea")):
            return
    except Exception:
        pass
    # 依次尝试点验证码输入框 / 任意可见输入框
    for f in page.frames:
        for sel in ("input[placeholder*='验证码']", "input[placeholder*='验证']",
                    "input[type='number']", "input[type='text']", "input:not([type])",
                    "input[placeholder]", "input", "textarea"):
            try:
                cand = f.locator(sel).first
                if await cand.count() and await cand.is_visible():
                    await cand.click(timeout=2000)
                    await page.wait_for_timeout(200)
                    return
            except Exception:
                continue


async def manual_type(acc_id: str, text: str) -> tuple[bool, str]:
    """远程手动操作：向当前聚焦的输入框输入文字（自动确保聚焦）。"""
    sess = _active.get(acc_id)
    if not sess or sess.get("done"):
        return False, "没有进行中的登录会话"
    text = (text or "").strip()
    if not text:
        return False, "输入内容为空"
    try:
        await _ensure_input_focus(sess["page"])
        await sess["page"].keyboard.type(text, delay=60)
        return True, "已输入"
    except Exception as e:
        return False, f"输入失败: {str(e)[:80]}"


async def manual_enter(acc_id: str) -> tuple[bool, str]:
    """远程手动操作：确保聚焦后按回车（常用于提交）。"""
    sess = _active.get(acc_id)
    if not sess or sess.get("done"):
        return False, "没有进行中的登录会话"
    try:
        await _ensure_input_focus(sess["page"])
        await sess["page"].keyboard.press("Enter")
        return True, "已回车"
    except Exception as e:
        return False, f"操作失败: {str(e)[:80]}"


async def visible_text(acc_id: str, max_len: int = 800) -> str:
    """提取无头页面各 frame 的可见文字（诊断用，优先验证弹窗内容）。"""
    sess = _active.get(acc_id)
    if not sess or sess.get("done"):
        return ""
    out: list[str] = []
    try:
        for f in sess["page"].frames[:4]:
            # 1) 优先抓验证/滑块容器文字
            for sel in ("#captcha_container", "[class*='captcha']", "[class*='secsdk']",
                        "[class*='verify']", "[class*='slider']"):
                try:
                    el = f.locator(sel).first
                    if await el.count():
                        t = await el.inner_text(timeout=3000)
                        t = " ".join(t.split())[:500]
                        if t:
                            out.append(f"[captcha: {sel} @ {f.url[:50]}]\n{t}")
                except Exception:
                    continue
            # 2) 整页 body 文字（兜底）
            try:
                t = await f.locator("body").inner_text(timeout=3000)
                t = " ".join(t.split())[:400]
                if t:
                    out.append(f"[frame: {f.url[:70]}]\n{t}")
            except Exception:
                continue
    except Exception:
        pass
    return "\n\n".join(out)[:max_len]


async def poll_login(acc_id: str, acc_dir: Path) -> dict:
    """轮询登录结果。登录成功时保存登录态并关闭会话。"""
    sess = _active.get(acc_id)
    if not sess:
        return {"logged_in": False, "message": "未找到登录会话，请重新开始"}
    if sess.get("done"):
        return {"logged_in": False, "message": "登录会话已结束"}
    if time.time() - sess["at"] > MAX_WAIT:
        await _close(acc_id)
        return {"logged_in": False, "message": "扫码超时（5 分钟），请重新开始"}
    try:
        cookies = await sess["context"].cookies()
        if any(c["name"].startswith("sessionid") for c in cookies):
            acc_dir.mkdir(parents=True, exist_ok=True)
            await sess["context"].storage_state(path=str(acc_dir / "state.json"))
            await _close(acc_id)
            logger.info("【%s】扫码登录成功，登录态已保存", acc_id)
            return {"logged_in": True, "message": "ok"}
        # 未登录：检测是否卡在二次验证
        vtype, _ = await _verify_modal(sess["page"])
        if vtype == "sms":
            # 弹窗出现且还没发过码 → 自动点一次「发送验证码」，不用用户手动点
            if not sess.get("sent_code"):
                sess["sent_code"] = True
                try:
                    ok, msg = await resend_verify_code(acc_id)
                    if ok:
                        return {"logged_in": False, "message": "验证码已发送到手机，请输入", "need_verify": True, "verify_type": "sms"}
                except Exception:
                    pass
            return {"logged_in": False, "message": "二次验证（短信）：① 点画面里的「获取/发送验证码」② 点验证码输入框 ③ 输入后点提交", "need_verify": True, "verify_type": "sms"}
        if vtype == "slider":
            return {"logged_in": False, "message": "触发了滑块/安全验证：点「🖐 模拟拖动」，先点画面里的滑块手柄，再点缺口位置", "need_verify": True, "verify_type": "slider"}
        return {"logged_in": False, "message": "等待扫码…"}
    except Exception as e:
        await _close(acc_id)
        return {"logged_in": False, "message": f"登录检查异常: {str(e)[:80]}"}


async def cancel_login(acc_id: str) -> None:
    await _close(acc_id)
    logger.info("【%s】扫码登录已取消", acc_id)


def resolve_nickname(acc_dir: Path) -> str | None:
    """用该账号登录态打开个人主页读取昵称（同步 API，请在后台线程调用）。"""
    state = acc_dir / "state.json"
    if not state.exists():
        return None
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
            try:
                context = browser.new_context(
                    storage_state=str(state), viewport={"width": 1366, "height": 768}
                )
                page = context.new_page()
                page.goto("https://www.douyin.com/user/self", timeout=60000, wait_until="domcontentloaded")
                page.wait_for_timeout(10000)
                title = (page.title() or "").strip()
                if title and "登录" not in title:
                    candidate = title.replace(" - 抖音", "").replace("抖音", "").strip()
                    if candidate:
                        return candidate
                for sel in ("h1", "div[class*='nickname']"):
                    try:
                        el = page.locator(sel).first
                        if el.count():
                            txt = (el.inner_text() or "").strip()
                            if txt:
                                return txt
                    except Exception:
                        continue
            finally:
                browser.close()
    except Exception:
        pass
    return None


def maybe_rename_after_login(acc_id: str, acc_dir: Path) -> None:
    """登录成功后，若账号名还是默认名，尝试用主页昵称自动重命名（后台线程调用）。"""
    try:
        from . import accounts
        from .config import load_account_config

        cfg = load_account_config(acc_dir)
        if cfg.get("name") not in GENERIC_NAMES:
            return
        nick = resolve_nickname(acc_dir)
        if nick and nick != cfg.get("name"):
            accounts.rename_account(acc_id, nick)
            logger.info("【%s】已根据主页昵称自动命名为 %s", acc_id, nick)
    except Exception as e:
        logger.info("自动重命名失败: %s", str(e)[:80])
