"""Playwright 自动化：在抖音网页版私信页面给指定好友发送消息。

多账号版要点：
- 所有操作以账号目录（data/accounts/<id>）为参数，登录态/配置/截图全部分开；
- 每个账号可选浏览器引擎 chromium / firefox / webkit，Cookie 完全隔离；
- 发送逻辑参考 douyin-cloud-streak（MIT）：
  点击后校验会话标题、搜索兜底、检测限流、发送后校验输入框已清空。
"""

from __future__ import annotations

import logging
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from .config import load_account_config, save_account_config

logger = logging.getLogger("douyin-spark")

CHAT_URL = "https://www.douyin.com/chat"

# 抓取聊天列表的实时阶段（key=账号id），供网页端显示进度
fetch_progress: dict[str, str] = {}

# Linux 容器里跑无头 Chromium 需要 --no-sandbox；Windows/macOS 不需要
_CHROMIUM_ARGS = ["--disable-dev-shm-usage", "--disable-gpu"]
if sys.platform.startswith("linux"):
    _CHROMIUM_ARGS += ["--no-sandbox", "--disable-setuid-sandbox"]

RATE_LIMIT_KEYWORDS = [
    "操作频繁",
    "操作太频繁",
    "发送过于频繁",
    "请稍后再试",
    "稍后再试",
    "安全验证",
    "滑动验证",
    "验证码",
    "验证中心",
    "人机验证",
    "网络异常",
    "请勿频繁",
]

LOGIN_TEXTS = ["扫码登录", "验证码登录", "登录后查看", "登录后即可"]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _launch_browser(p, engine: str, headless: bool):
    launcher = getattr(p, engine)
    kwargs: dict = {"headless": bool(headless)}
    if engine == "chromium":
        kwargs["args"] = _CHROMIUM_ARGS
    return launcher.launch(**kwargs)


def _screenshot(page, acc_dir: Path) -> None:
    try:
        page.screenshot(path=str(acc_dir / "last_error.png"), timeout=5000)
        logger.info("已保存页面截图: %s", acc_dir / "last_error.png")
    except Exception:
        pass


def check_login(page) -> tuple[bool, str]:
    """返回 (是否已登录, 说明)。宁可误报掉线，也不要带着过期登录态硬跑。"""
    url = page.url
    if "login" in url.lower() or "passport" in url.lower():
        return False, f"页面已跳转到登录页（{url}）"

    try:
        qr = page.locator("#animate_qrcode_container")
        if qr.count() and qr.first.is_visible():
            return False, "页面出现扫码登录二维码，登录态已过期"
    except Exception:
        pass

    for text in LOGIN_TEXTS:
        try:
            loc = page.get_by_text(text, exact=False)
            for i in range(min(loc.count(), 3)):
                if loc.nth(i).is_visible():
                    return False, f"页面出现登录提示「{text}」"
        except Exception:
            continue

    cookies = page.context.cookies()
    if not any(c["name"].startswith("sessionid") for c in cookies):
        return False, "未检测到 sessionid Cookie"
    return True, "ok"


def detect_rate_limit(page) -> str | None:
    for kw in RATE_LIMIT_KEYWORDS:
        try:
            loc = page.get_by_text(kw, exact=False)
            for i in range(loc.count()):
                if loc.nth(i).bounding_box():
                    return kw
        except Exception:
            continue
    return None


def _find_contact(page, name: str):
    """优先按全文精确匹配联系人标题，避免误点其他会话里的消息预览。"""
    exact = page.get_by_text(name, exact=True)
    if exact.count():
        return exact.first
    return page.locator(".conversationConversationItemtitle").filter(has_text=name).first


def verify_in_conversation(page, name: str) -> bool:
    """右侧会话顶部标题区域（x>300 且 y<100）出现目标昵称才算切换成功，防止错发。"""
    for exact in (True, False):
        try:
            loc = page.get_by_text(name, exact=exact)
            for i in range(loc.count()):
                try:
                    box = loc.nth(i).bounding_box()
                except Exception:
                    continue
                if box and box.get("x", 0) > 300 and box.get("y", 0) < 100:
                    return True
        except Exception:
            continue
    return False


def search_and_open(page, name: str) -> bool:
    try:
        box = page.get_by_placeholder("搜索", exact=False).first
        if box.count() == 0:
            return False
        box.click()
        box.fill(name)
        time.sleep(4)
        # 优先直接点搜索结果里的「发消息」按钮，最可靠
        btn = page.get_by_text("发消息", exact=False).first
        if btn.count():
            btn.click(force=True)
            time.sleep(4)
            return True
        # 否则点精确匹配的结果卡片，再找「发消息」入口
        candidate = page.get_by_text(name, exact=True).first
        if candidate.count() == 0:
            candidate = page.get_by_text(name, exact=False).first
        if candidate.count() == 0:
            return False
        candidate.click(force=True)
        time.sleep(3)
        btn = page.get_by_text("发消息", exact=False).first
        if btn.count():
            btn.click(force=True)
            time.sleep(3)
        return True
    except Exception as e:
        logger.info("搜索打开 %s 失败: %s", name, e)
        return False


def _type_and_send(page, input_box, msg_text: str) -> bool:
    """把文字输入输入框并按 Enter 发送，返回文字是否成功进入输入框。"""
    try:
        input_box.click()
        time.sleep(0.4)
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        time.sleep(0.3)
        page.keyboard.type(msg_text, delay=100)
        time.sleep(0.8)
        cur = input_box.inner_text() or ""
        if msg_text not in cur:
            logger.warning("文字未进入输入框，当前内容: %r", cur[:30])
            return False
        page.keyboard.press("Enter")
        return True
    except Exception as e:
        logger.info("输入/发送异常: %s", str(e)[:100])
        return False


def _wait_input_cleared(input_box, msg_text: str, wait: float = 8) -> bool:
    """消息发出后输入框应不再包含发送文字，以此确认真正发出。"""
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(1)
        try:
            cur = input_box.inner_text() or ""
            if msg_text not in cur:
                return True
        except Exception:
            pass
    return False


def send_to_contact(page, name: str, msg_text: str, dry_run: bool) -> tuple[bool, str]:
    switched = False
    for attempt in range(5):
        try:
            target = _find_contact(page, name)
            if target.count():
                target.click(force=True, timeout=10000)
                time.sleep(random.uniform(2, 4))
                if verify_in_conversation(page, name):
                    switched = True
                    break
            else:
                # 目标可能因列表懒加载尚未渲染，滚动侧边栏继续找
                try:
                    page.mouse.move(200, 350)
                    page.mouse.wheel(0, 600)
                except Exception:
                    pass
                time.sleep(1.5)
        except Exception as e:
            logger.info("点击联系人 %s 异常: %s", name, str(e)[:100])
        time.sleep(random.uniform(1, 2))

    if not switched and search_and_open(page, name):
        time.sleep(random.uniform(1, 3))
        switched = verify_in_conversation(page, name)

    if not switched:
        return False, "未能切换到该好友会话（名字不在聊天列表，或页面结构变化）"

    if detect_rate_limit(page):
        return False, "检测到「操作频繁 / 安全验证」提示"

    input_box = page.locator('div[contenteditable="true"]').first
    try:
        if input_box.count() == 0 or input_box.bounding_box() is None:
            return False, "找不到聊天输入框"
        input_box.wait_for(state="visible", timeout=8000)
    except Exception:
        return False, "找不到聊天输入框"

    if dry_run:
        return True, "dry-run"

    try:
        if detect_rate_limit(page):
            return False, "发送前检测到验证提示"
        if not _type_and_send(page, input_box, msg_text):
            return False, "文字未能输入到输入框"
        if _wait_input_cleared(input_box, msg_text, wait=8):
            return True, "ok"
        logger.warning("未检测到消息发出，重试一次：%s", name)
        if detect_rate_limit(page):
            return False, "重试时检测到验证提示"
        if not _type_and_send(page, input_box, msg_text):
            return False, "重试时文字未能输入"
        if _wait_input_cleared(input_box, msg_text, wait=8):
            return True, "ok"
        return False, "发送后输入框未清空，消息可能未发出"
    except Exception as e:
        logger.info("向 %s 发送异常: %s", name, e)
        return False, f"发送异常: {e}"


def _enabled_friend_names(cfg: dict) -> list[str]:
    return [f["name"] for f in cfg.get("friends", []) if f.get("enabled", True)]


_STREAK_EXTRACT_JS = """
    () => {
        const out = [];
        const seen = new Set();
        const findRoot = (wrap) => {
            let root = wrap;
            for (let k = 0; k < 3 && root.parentElement; k++) {
                const cls = String(root.parentElement.className || '');
                if (/ConversationItem/i.test(cls)) { root = root.parentElement; } else break;
            }
            return root;
        };
        const inTitle = (el, root) => {
            let p = el;
            while (p && p !== root) {
                if (/title/i.test(String(p.className || ''))) return true;
                p = p.parentElement;
            }
            return false;
        };
        const pickAvatar = (wrap) => {
            const root = findRoot(wrap);
            const candidates = [];
            const push = (u) => {
                if (u && /^https?:/.test(u) && !/data:|\\\\.svg|flame_icon|icon/i.test(u) && !candidates.includes(u)) candidates.push(u);
            };
            for (const im of root.querySelectorAll('img')) {
                if (inTitle(im, root)) continue;
                push(im.currentSrc); push(im.src); push(im.getAttribute('data-src'));
                (im.getAttribute('srcset') || '').split(',').forEach(p => push(p.trim().split(' ')[0]));
            }
            for (const el of root.querySelectorAll('*')) {
                if (inTitle(el, root)) continue;
                const st = el.getAttribute && el.getAttribute('style');
                if (st) { const m = st.match(/url\\(["']?(.*?)["']?\\)/i); if (m) push(m[1]); }
                try {
                    const bg = getComputedStyle(el).backgroundImage;
                    const m = bg && bg.match(/url\\(["']?(.*?)["']?\\)/);
                    if (m) push(m[1]);
                } catch (e) {}
            }
            if (!candidates.length) return '';
            const score = (u) => (/avatar|tos-cn-av|aweme|douyinpic|byteimg/i.test(u) ? 4 : 0) + (u.length / 500);
            candidates.sort((a, b) => score(b) - score(a));
            return candidates[0];
        };
        document.querySelectorAll('.conversationConversationItemtitle').forEach(t => {
            const name = (t.textContent || '').trim();
            if (!name || seen.has(name)) return;
            seen.add(name);
            const wrap = t.parentElement;
            const s = wrap ? wrap.querySelector('.commonStreaknormalText') : null;
            const timeEl = wrap ? wrap.querySelector('[class*="timeStr"]') : null;
            out.push({
                name: name,
                streak: s ? (s.textContent || '').trim() : '',
                avatar: pickAvatar(wrap),
                time: timeEl ? (timeEl.textContent || '').trim() : '',
            });
        });
        return out;
    }
"""

_STREAK_DEBUG_JS = None  # 诊断已完成，已移除


def _streak_num(s) -> int:
    """从 '30' / '30+' 等字符串里取出数字；取不到返回 0。"""
    m = re.search(r"\d+", str(s or ""))
    return int(m.group()) if m else 0


def _merge_streaks(cfg: dict, contacts: list[dict]) -> dict:
    """把抓到的联系人火花天数合并进好友名单，检测是否重燃。返回变更摘要。"""
    names_map = {c.get("name"): (c.get("streak") or "") for c in contacts}
    changed = False
    updated = 0
    rekindled_list: list[str] = []
    for f in cfg.get("friends", []):
        name = (f.get("name") or "").strip()
        if not name or name not in names_map:
            continue
        new_streak = str(names_map.get(name) or "").strip()
        old_streak = str(f.get("streak") or "").strip()
        # 重燃 = 火花熄过又重新烧起来：新天数比上次记录的天数少
        rekindled = bool(
            old_streak
            and _streak_num(new_streak)
            and _streak_num(new_streak) < _streak_num(old_streak)
        )
        if f.get("streak") != new_streak or bool(f.get("rekindled")) != rekindled:
            changed = True
            updated += 1
        f["streak"] = new_streak
        f["rekindled"] = rekindled
        if rekindled:
            rekindled_list.append(name)
    return {"changed": changed, "updated": updated, "rekindled": rekindled_list}


def _refresh_streaks_from_page(acc_dir: Path, cfg: dict, page) -> int:
    """利用已打开的私信页轻量刷新好友火花天数（只滚动几次，不重复启动浏览器）。"""
    collected: list[dict] = []
    try:
        page.wait_for_selector(".conversationConversationItemtitle", timeout=15000)
    except Exception:
        return 0
    for _ in range(3):
        try:
            data = page.evaluate(_STREAK_EXTRACT_JS) or []
            for x in data:
                if x not in collected:
                    collected.append(x)
            page.mouse.wheel(0, 600)
            page.wait_for_timeout(600)
        except Exception:
            break
    if not collected:
        return 0
    summary = _merge_streaks(cfg, collected)
    if summary["changed"]:
        save_account_config(acc_dir, cfg)
    if summary["rekindled"]:
        logger.info("已刷新 %s 位好友火花天数，重燃：%s", summary["updated"], "、".join(summary["rekindled"]))
    elif summary["updated"]:
        logger.info("已刷新 %s 位好友火花天数", summary["updated"])
    return summary["updated"]


def sync_friend_streaks(acc_dir: Path) -> dict:
    """完整抓取聊天列表，把好友名单的火花天数/是否重燃更新到最新。"""
    result = {"error": None, "updated": 0, "rekindled": [], "at": None}
    cfg = load_account_config(acc_dir)
    if not cfg.get("friends"):
        result["error"] = "好友名单为空，无需同步"
        return result
    data = fetch_chat_contacts(acc_dir)
    result["at"] = data.get("at")
    if data.get("error"):
        result["error"] = data["error"]
        return result
    summary = _merge_streaks(cfg, data.get("names", []))
    if summary["changed"]:
        save_account_config(acc_dir, cfg)
    result["updated"] = summary["updated"]
    result["rekindled"] = summary["rekindled"]
    return result


def fetch_chat_contacts(acc_dir: Path) -> dict:
    """从抖音私信页左侧聊天列表读取联系人（含火花天数），供网页端勾选。"""
    result = {"at": _now(), "names": [], "error": None}
    state_path = acc_dir / "state.json"
    if not state_path.exists():
        result["error"] = "该账号尚未上传登录态 state.json"
        return result

    # 实时阶段上报（key=账号id），网页端轮询显示进度
    acc_id = acc_dir.name
    fetch_progress[acc_id] = "launch"

    cfg = load_account_config(acc_dir)
    engine = cfg.get("browser", "chromium")
    headless = cfg.get("headless", True)

    browser = None
    try:
        p = sync_playwright().start()
        try:
            fetch_progress[acc_id] = "open"
            browser = _launch_browser(p, engine, headless)
            context = browser.new_context(
                storage_state=str(state_path),
                viewport={"width": 1366, "height": 768},
            )
            page = context.new_page()

            goto_ok = False
            for attempt in range(3):
                try:
                    page.goto(CHAT_URL, timeout=90000, wait_until="domcontentloaded")
                    goto_ok = True
                    break
                except Exception as e:
                    logger.info("获取联系人时第 %s 次打开页面失败: %s", attempt + 1, str(e)[:80])
                    time.sleep(5)
            if not goto_ok:
                result["error"] = "无法打开抖音私信页面"
                return result

            fetch_progress[acc_id] = "wait"
            page.wait_for_timeout(10000)
            fetch_progress[acc_id] = "login"
            logged, why = check_login(page)
            if not logged:
                result["error"] = why
                return result

            fetch_progress[acc_id] = "scrape"
            collected: list[dict] = []
            for attempt in range(3):
                try:
                    page.wait_for_selector(".conversationConversationItemtitle", timeout=45000)
                except Exception:
                    logger.info("第 %s 次等待联系人列表超时", attempt + 1)

                stable = 0
                for _ in range(20):
                    data = page.evaluate(_STREAK_EXTRACT_JS) or []
                    new_items = [x for x in data if x not in collected]
                    if new_items:
                        collected.extend(new_items)
                        stable = 0
                    else:
                        stable += 1
                        if stable >= 2:
                            break
                    try:
                        page.mouse.move(200, 350)
                        page.mouse.wheel(0, 800)
                    except Exception:
                        pass
                    page.wait_for_timeout(1200)

                if collected:
                    break
                try:
                    page.reload(wait_until="domcontentloaded", timeout=90000)
                    page.wait_for_timeout(12000)
                except Exception:
                    pass

            result["names"] = collected
            logger.info("已读取聊天列表联系人 %s 个", len(result["names"]))
            # 头像抓取结果统计：全成功一行日志；未抓全则折叠报错（日志页点击展开详情）
            total = len(collected)
            with_avatar = sum(1 for c in collected if c.get("avatar"))
            missing = [c.get("name", "") for c in collected if not c.get("avatar")]
            if total == 0:
                logger.info("头像抓取：聊天列表为空，无需获取头像")
            elif with_avatar == total:
                logger.info("头像抓取成功：%s/%s 个联系人均已获取头像", with_avatar, total)
            elif with_avatar > 0:
                logger.warning(
                    "【头像抓取部分成功】%s/%s 个联系人已获取头像，%s 个未获取到",
                    with_avatar, total, total - with_avatar,
                )
                logger.info("【头像抓取详情】未获取到头像：%s", "、".join(missing[:10]) + (" 等" if len(missing) > 10 else ""))
            else:
                logger.warning(
                    "【头像抓取失败】%s 个联系人未获取到任何头像（0/%s）",
                    total, total,
                )
                logger.info("【头像抓取详情】未获取到头像：%s", "、".join(missing[:10]) + (" 等" if len(missing) > 10 else ""))
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            p.stop()
    except Exception as e:
        logger.error("获取联系人异常: %s", e)
        result["error"] = f"获取联系人异常: {e}"
    finally:
        fetch_progress.pop(acc_id, None)
    return result


def run_send(acc_dir: Path, dry_run: bool = False, only_names: list[str] | None = None) -> dict:
    """给指定账号的好友发送消息（only_names 为 None 时发全部启用的好友）。"""
    cfg = load_account_config(acc_dir)
    friends = _enabled_friend_names(cfg)
    if only_names is not None:
        friends = [f for f in friends if f in only_names]
    messages = cfg.get("messages") or ["🔥"]
    # 不能用 `or N` 兜底：max_friends_per_run=0 表示不限制，0 or 20 会错误地变成 20
    max_n = int(cfg.get("max_friends_per_run", 20))
    gap_min = max(1, int(cfg.get("send_gap_min", 6)))
    gap_max = max(gap_min, int(cfg.get("send_gap_max", 12)))
    engine = cfg.get("browser", "chromium")
    headless = cfg.get("headless", True)

    result = {
        "at": _now(),
        "dry_run": bool(dry_run),
        "ok": [],
        "failed": [],
        "logged_out": False,
        "rate_limited": False,
    }

    state_path = acc_dir / "state.json"
    if not state_path.exists():
        result["failed"].append({"name": "_system", "reason": "该账号尚未上传登录态 state.json"})
        return result

    targets = friends[:max_n] if max_n > 0 else friends
    if not targets:
        # 明确给出原因，避免网页端显示“成功 0 失败 0”让人摸不着头脑
        if cfg.get("friends"):
            reason = "名单里有好友但全部处于停用状态（请到「好友与消息」开启）"
        else:
            reason = "未配置启用的好友（请到「好友与消息」页添加并保存）"
        result["failed"].append({"name": "_system", "reason": reason})
        return result

    browser = None
    try:
        p = sync_playwright().start()
        try:
            browser = _launch_browser(p, engine, headless)
            context = browser.new_context(
                storage_state=str(state_path),
                viewport={"width": 1366, "height": 768},
            )
            page = context.new_page()

            goto_ok = False
            for attempt in range(3):
                try:
                    page.goto(CHAT_URL, timeout=60000, wait_until="domcontentloaded")
                    goto_ok = True
                    break
                except Exception as e:
                    logger.info("第 %s 次打开页面失败: %s", attempt + 1, str(e)[:80])
                    time.sleep(5)
            if not goto_ok:
                result["failed"].append({"name": "_system", "reason": "无法打开抖音私信页面"})
                return result

            time.sleep(8)
            logged, why = check_login(page)
            if not logged:
                result["logged_out"] = True
                result["failed"].append({"name": "_system", "reason": why})
                _screenshot(page, acc_dir)
                return result

            logger.info("待发送好友 %s 人，dry_run=%s，浏览器=%s", len(targets), dry_run, engine)
            # 顺手刷新好友名单的火花天数（页面已在私信列表，轻量抓取，不额外开浏览器）
            try:
                _refresh_streaks_from_page(acc_dir, cfg, page)
            except Exception as e:
                logger.info("刷新火花天数失败: %s", str(e)[:80])
            for name in targets:
                msg = random.choice(messages)
                ok, why = send_to_contact(page, name, msg, dry_run)
                if ok:
                    result["ok"].append(name)
                    logger.info("已发送给 %s：%s", name, msg if not dry_run else "(干跑，未真实发送)")
                else:
                    result["failed"].append({"name": name, "reason": why})
                    logger.warning("发送给 %s 失败：%s", name, why)
                    if detect_rate_limit(page):
                        result["rate_limited"] = True
                        logger.warning("疑似触发限流，停止本轮")
                        break
                time.sleep(random.uniform(gap_min, gap_max))
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            p.stop()
    except Exception as e:
        msg = str(e)
        logger.error("运行异常: %s", msg)
        # 引擎未安装/路径无效时给出明确的中文指引（而不是一串英文报错）
        if "Executable doesn't exist" in msg or "executable doesn't exist" in msg.lower():
            hint = (
                f"浏览器引擎不可用（未安装或路径无效，当前引擎: {engine}）。"
                "请在网页「定时与浏览器」页把该账号的浏览器引擎改为 Chromium，"
                "或运行 python -m playwright install firefox webkit 安装对应浏览器。"
            )
            result["failed"].append({"name": "_system", "reason": hint})
        else:
            result["failed"].append({"name": "_system", "reason": f"运行异常: {msg[:150]}"})
    return result
