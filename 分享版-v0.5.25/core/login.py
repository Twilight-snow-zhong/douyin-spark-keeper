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
QR_SELECTOR = "#animate_qrcode_container"
MAX_WAIT = 300  # 扫码超时（秒）

_active: dict[str, dict] = {}
_lock = threading.Lock()

GENERIC_NAMES = ("未命名账号", "默认账号", "新账号")


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
    """确保抖音登录弹窗出现。返回是否检测到二维码容器。"""
    # 1) 先等自动弹出的登录框（最多约 7.5 秒）
    for _ in range(5):
        try:
            qr = page.locator(QR_SELECTOR)
            if await qr.count() and await qr.first.is_visible():
                return True
        except Exception:
            pass
        await page.wait_for_timeout(1500)
    # 2) 依次尝试点击常见登录入口
    strategies = [
        page.get_by_text("登录", exact=True).first,
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
                qr = page.locator(QR_SELECTOR)
                if await qr.count() and await qr.first.is_visible():
                    return True
        except Exception:
            continue
    return False


async def _qr_image_of(page) -> tuple[str | None, str]:
    """级联抓取二维码图片。返回 (data_url, 说明)。"""
    # 1) 已知二维码容器 / 常见二维码元素
    for sel in (QR_SELECTOR, "img[src*='qr']", "img[src*='qrcode']", "canvas"):
        try:
            loc = page.locator(sel)
            n = await loc.count()
            for i in range(min(n, 5)):
                if await loc.nth(i).is_visible():
                    data = await loc.nth(i).screenshot(timeout=10000)
                    return "data:image/png;base64," + base64.b64encode(data).decode(), f"元素: {sel}[{i}]"
        except Exception:
            continue
    # 2) 含「扫码登录」文字的弹窗区域（截图其周边区域）
    try:
        txt = page.get_by_text("扫码登录", exact=False).first
        if await txt.count():
            box = await txt.bounding_box()
            if box:
                clip = {
                    "x": max(0, box["x"] - 100),
                    "y": max(0, box["y"] - 160),
                    "width": 420,
                    "height": 560,
                }
                data = await page.screenshot(clip=clip, timeout=10000)
                return "data:image/png;base64," + base64.b64encode(data).decode(), "区域: 扫码登录弹窗"
    except Exception:
        pass
    # 3) 整页兜底
    data = await page.screenshot(timeout=10000)
    return "data:image/png;base64," + base64.b64encode(data).decode(), f"整页兜底 @ {page.url}"


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
                kwargs["args"] = _CHROMIUM_ARGS
            if path_engine:
                kwargs["executable_path"] = path_engine
            browser = await launcher.launch(**kwargs)
            context = await browser.new_context(viewport={"width": 1000, "height": 800})
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


async def qr_image(acc_id: str) -> tuple[str | None, str]:
    """返回二维码 PNG 的 data URL 及抓取方式说明；拿不到返回 (None, 说明)。"""
    sess = _active.get(acc_id)
    if not sess or sess.get("done"):
        return None, "无进行中的登录会话"
    try:
        return await _qr_image_of(sess["page"])
    except Exception:
        return None, "截图失败"


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
