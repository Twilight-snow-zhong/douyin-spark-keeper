"""Playwright 自动化：在抖音网页版私信页面给指定好友发送消息。

多账号版要点：
- 所有操作以账号目录（data/accounts/<id>）为参数，登录态/配置/截图全部分开；
- 每个账号可选浏览器引擎 chromium / firefox / webkit，Cookie 完全隔离；
- 发送逻辑参考 douyin-cloud-streak（MIT）：
  点击后校验会话标题、搜索兜底、检测限流、发送后校验输入框已清空。
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from .config import DATA_DIR, load_account_config, save_account_config
from . import runtime as runtime_state
from .errors import (
    AUTHENTICATION,
    PERMANENT,
    RATE_LIMIT,
    RATE_LIMIT_KEYWORDS,
    TRANSIENT,
    backoff,
    classify_error,
    should_stop_all,
)

logger = logging.getLogger("douyin-spark")

CHAT_URL = "https://www.douyin.com/chat"
# 左侧会话列表：虚拟列表（视窗外行会卸载），搜索态下整个容器会被 display:none
_LIST_WRAP_SEL = '[class*="conversationConversationListwrapper"]'
_LIST_TITLE_SEL = ".conversationConversationItemtitle"
# 会话列表两轮等待仍未渲染出任何行时的统一报错（发送路径必须等列表就绪再找好友）
_LIST_NOT_READY_MSG = (
    "私信页未就绪：未能进入会话，私信页未加载或该登录态未完成网页端授权。"
    "建议重试，或重新扫码登录并在网页端确认能看到会话列表"
)

# 表情/图片库目录：data/emojis（全局共享）
EMOJIS_DIR = DATA_DIR / "emojis"
# 消息模板里的图片标记：【图:文件名.png】 / 【图片:文件名.png】
_EMOJI_MARKER_RE = re.compile(r"【(?:图|图片):([^】]+)】")
# 原生火花贴纸标记：【火花】 / 【火焰】（走抖音网页端自带贴纸面板，对方收到原生大表情）
_FLAME_MARKER_RE = re.compile(r"【(?:火花|火焰)】")
# 任意原生贴纸标记：【贴纸:名字】如【贴纸:比心】【贴纸:笑死】（名字在表情面板里可见）
_STICKER_MARKER_RE = re.compile(r"【贴纸:([^】]+)】")
# 抖音网页表情面板已见的贴纸名（面板文字标签，可按名定位）
KNOWN_STICKER_SYNONYMS = {
    "续火花": ["续火花", "火花", "火焰", "🔥"],
    "比心": ["比心"],
    "V我50": ["V我50", "v我50", "v我 50"],
    "在干嘛": ["在干嘛"],
    "笑死": ["笑死"],
    "麻了": ["麻了"],
    "躺平": ["躺平"],
    "摇骰子": ["摇骰子", "骰子"],
    "猜拳": ["猜拳"],
    "开心": ["开心"],
    "嗨": ["嗨"],
    "吹泡泡": ["吹泡泡", "泡泡"],
    "黑人问号": ["黑人问号"],
    "早上好": ["早上好"],
    "晚上好": ["晚上好"],
    "早点睡": ["早点睡"],
    "爱心": ["爱心"],
    "便便": ["便便"],
    "戳一戳": ["戳一戳", "戳戳"],
    "绝了": ["绝了"],
    "已阅": ["已阅"],
    "生日祝福": ["生日祝福"],
}
# 兜底：常见贴纸对应的面板格子锚点（行1列1=续火花）。新贴纸可先录一次点位加入
_STICKER_CELL_ANCHOR = {
    "续火花": (1014, 466),
}

# 抓取聊天列表的实时阶段（key=账号id），供网页端显示进度
fetch_progress: dict[str, str] = {}



# Linux 容器里跑无头 Chromium 需要 --no-sandbox；Windows/macOS 不需要
_CHROMIUM_ARGS = ["--disable-dev-shm-usage", "--disable-gpu"]
if sys.platform.startswith("linux"):
    _CHROMIUM_ARGS += ["--no-sandbox", "--disable-setuid-sandbox"]

LOGIN_TEXTS = ["扫码登录", "验证码登录", "登录后查看", "登录后即可"]


def _pause_account(acc_dir: Path | None, err_class: str, reason: str) -> str:
    """AUTHENTICATION / RATE_LIMIT → 记录 paused_until（默认 2 小时）。"""
    if not acc_dir or not should_stop_all(err_class):
        return ""
    try:
        until = runtime_state.pause_account(acc_dir, runtime_state.PAUSE_MINUTES_DEFAULT, reason)
        logger.warning(
            "账号已暂停至 %s（类别=%s，原因：%s）—— 到点自动恢复；未到点手动发送也会被拒绝",
            until, err_class, str(reason)[:120],
        )
        return until
    except Exception as e:
        logger.warning("写入 paused_until 失败（忽略）: %s", str(e)[:100])
        return ""


def _remember_names(names) -> None:
    """登记昵称，供日志脱敏（只影响日志，不改前端数据）。"""
    try:
        runtime_state.register_sensitive_names(names)
    except Exception:
        pass

def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _launch_browser(p, engine: str, headless: bool):
    launcher = getattr(p, engine)
    kwargs: dict = {"headless": bool(headless)}
    if engine == "chromium":
        kwargs["args"] = _CHROMIUM_ARGS
    return launcher.launch(**kwargs)


# 与 Playwright 默认 HeadlessChrome UA 区分开：无头 UA 本身是一个自动化特征
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _state_cookies(state_path: Path | str) -> list[dict]:
    """从 Playwright storage_state 文件里取出 cookies（丢掉没有 name 的脏项）。"""
    try:
        data = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except Exception as e:
        logger.info("读取登录态文件失败: %s", str(e)[:80])
        return []
    cookies = data.get("cookies") if isinstance(data, dict) else None
    return [c for c in (cookies or []) if isinstance(c, dict) and c.get("name")]


def _storage_mode(acc_dir: Path | None) -> str:
    """该账号偏好的登录态注入方式：cookie（默认，只注入 cookies）/ state（整份 storage_state）。

    见《借鉴/对照表.md》痛点 1 ①：抖音私信页 `word word` 占位页、列表接口被拒，多半是
    storage_state 里过期的 localStorage 与 cookies 不匹配导致 → 默认只注入 cookies。
    个别账号确实需要 localStorage，降级成功后会写 runtime.json 的 storage_mode=state 记住。
    """
    if not acc_dir:
        return "cookie"
    try:
        p = Path(acc_dir) / "runtime.json"
        if p.exists():
            mode = str((json.loads(p.read_text(encoding="utf-8")) or {}).get("storage_mode") or "").strip().lower()
            if mode == "state":
                return "state"
    except Exception:
        pass
    return "cookie"


def _set_storage_mode(acc_dir: Path | None, mode: str) -> None:
    """记住该账号的登录态注入偏好（runtime.json: storage_mode = cookie | state）。"""
    if not acc_dir:
        return
    mode = "state" if str(mode or "").strip().lower() == "state" else "cookie"
    try:
        from .runtime import update_runtime   # 懒加载：只写一个字段，不改动 runtime.py 本身

        update_runtime(Path(acc_dir), storage_mode=mode)
        logger.info("已记住该账号登录态注入偏好：storage_mode=%s", mode)
    except Exception as e:
        logger.info("写入 storage_mode 失败（忽略）: %s", str(e)[:80])


def _new_context(browser, state_path: Path | str, acc_dir: Path | None = None,
                 mode: str | None = None):
    """新建浏览器上下文：默认**只注入 cookies**，不注入 localStorage/origins。

    对照表痛点 1 ①（唯一直接解释“列表加载不出来”的一句）：storage_state 会把上次的
    localStorage/IndexedDB 一起注入，抖音前端轮换过 key 之后页面会渲染成 `word word`
    骨架屏、列表接口被拒 → 等 60 秒也不出列表。只 add_cookies 可避免该问题。
    mode="state" 时保留整份 storage_state 注入（降级兜底 / 该账号的历史偏好）。
    """
    mode = (mode or _storage_mode(acc_dir) or "cookie").strip().lower()
    kwargs: dict = {
        "viewport": {"width": 1366, "height": 768},
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "user_agent": _CHROME_UA,
    }
    if mode == "state":
        context = browser.new_context(storage_state=str(state_path), **kwargs)
        logger.info("登录态注入：整份 storage_state（cookie + localStorage）")
        return context
    context = browser.new_context(**kwargs)
    cookies = _state_cookies(state_path)
    if cookies:
        try:
            context.add_cookies(cookies)
        except Exception as e:
            logger.warning("仅注入 cookies 失败（继续以未登录态打开）: %s", str(e)[:80])
    logger.info("登录态注入：仅 cookies %s 条（不注入 localStorage/origins）", len(cookies))
    return context


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


def _norm_name(s: str) -> str:
    """名字归一化：好友名里混着 NBSP(\xa0) 等非常规空白时，统一成普通空格再比较。"""
    return re.sub(r"\s+", " ", (s or "")).strip()


def _find_contact(page, name: str):
    """按名字找联系人：优先返回「列表里真的有坐标的可见行」，找不到再退回文本匹配。

    抖音左侧会话列表是**虚拟列表**：视窗外的行会从 DOM 卸载，滚动后同一位置可能换成别人；
    偶发还会留下零尺寸的隐藏节点，点它必然抛 Element is not visible。
    """
    want = _norm_name(name)
    try:
        titles = page.locator(_LIST_TITLE_SEL)
        for i in range(min(titles.count(), 60)):
            el = titles.nth(i)
            try:
                if _norm_name(el.inner_text()) != want:
                    continue
                box = el.bounding_box()
            except Exception:
                continue
            if box and box["width"] >= 2 and box["height"] >= 2 and box["x"] < 330:
                return el
    except Exception:
        pass
    exact = page.get_by_text(name, exact=True)
    if exact.count():
        return exact.first
    return page.locator(_LIST_TITLE_SEL).filter(has_text=name).first


def verify_in_conversation(page, name: str) -> bool:
    """右侧会话顶部标题区域（x>300 且 y<100）出现目标昵称才算切换成功，防止错发。"""
    want = _norm_name(name)
    for exact in (True, False):
        try:
            loc = page.get_by_text(name, exact=exact)
            for i in range(loc.count()):
                try:
                    el = loc.nth(i)
                    box = el.bounding_box()
                except Exception:
                    continue
                if box and box.get("x", 0) > 300 and box.get("y", 0) < 100:
                    return True
        except Exception:
            continue
    # 兜底：名字带 NBSP 时 get_by_text 可能匹配不上，改成按归一化文字扫描右侧标题区
    try:
        box = page.evaluate(
            """(want) => {
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                let hit = null;
                document.querySelectorAll('div, span, a').forEach((el) => {
                    if (hit || el.children.length > 1) return;
                    if (norm(el.textContent) !== want) return;
                    const r = el.getBoundingClientRect();
                    if (r.x > 300 && r.y < 100 && r.width > 0 && r.height > 0) hit = true;
                });
                return hit;
            }""",
            want,
        )
        if box:
            return True
    except Exception:
        pass
    return False


def _list_state(page) -> dict:
    """左侧会话列表容器的状态（搜索态下它会被 display:none）。"""
    try:
        return page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return {found: false};
                const cs = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return {
                    found: true,
                    visible: cs.display !== 'none' && cs.visibility !== 'hidden' && r.width > 0,
                    top: Math.round(el.scrollTop), h: el.scrollHeight, client: el.clientHeight,
                };
            }""",
            _LIST_WRAP_SEL,
        )
    except Exception:
        return {"found": False}


def _list_scroll_top(page) -> None:
    try:
        page.evaluate("""(sel) => { const el = document.querySelector(sel); if (el) el.scrollTop = 0; }""",
                      _LIST_WRAP_SEL)
    except Exception:
        pass


def _list_scroll(page, dy: int, wait: float = 1.0) -> None:
    try:
        page.mouse.move(150, 400)
        page.mouse.wheel(0, dy)
    except Exception:
        pass
    time.sleep(wait)


def _ensure_list_visible(page) -> bool:
    """退出残留的搜索态。

    一旦搜索框有内容，抖音会把整个会话列表 `display:none`；此时任何列表点击都报
    "Element is not visible"，后续好友会**连续全部失败**（日志里 ambulances 连续 5 次异常即此因）。
    先试 Esc / 清空搜索框，仍不行就重载私信页兜底恢复。
    """
    st = _list_state(page)
    if st.get("found") and st.get("visible"):
        return True
    for _ in range(2):
        try:
            page.keyboard.press("Escape")
            time.sleep(0.6)
            box = page.get_by_placeholder("搜索", exact=False).first
            if box.count():
                box.click()
                box.fill("")
                page.keyboard.press("Escape")
            time.sleep(0.6)
            page.mouse.click(150, 420)
            time.sleep(0.8)
        except Exception:
            pass
        st = _list_state(page)
        if st.get("found") and st.get("visible"):
            return True
    try:
        logger.info("左侧会话列表仍被搜索态遮住，重载私信页恢复")
        page.goto(CHAT_URL, timeout=60000, wait_until="domcontentloaded")
        time.sleep(8)
    except Exception as e:
        logger.info("重载私信页失败: %s", str(e)[:80])
    st = _list_state(page)
    return bool(st.get("found") and st.get("visible"))


def _visible_text_center(page, text: str, x_max: float | None = None):
    """找「按归一化文字完全相等且真的有坐标」的元素，返回中心坐标（用于坐标点击）。"""
    want = _norm_name(text)
    try:
        titles = page.locator(_LIST_TITLE_SEL)
        for i in range(min(titles.count(), 60)):
            el = titles.nth(i)
            try:
                if _norm_name(el.inner_text()) != want:
                    continue
                box = el.bounding_box()
            except Exception:
                continue
            if box and box["width"] >= 2 and box["height"] >= 2 and (x_max is None or box["x"] < x_max):
                return (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    except Exception:
        pass
    try:
        return page.evaluate(
            """([want, xMax]) => {
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                let hit = null;
                document.querySelectorAll('div, span, button, a, p').forEach((el) => {
                    if (hit || el.children.length > 0) return;
                    if (norm(el.textContent) !== want) return;
                    const r = el.getBoundingClientRect();
                    if (r.width < 2 || r.height < 2) return;
                    if (xMax !== null && r.x >= xMax) return;
                    hit = [r.x + r.width / 2, r.y + r.height / 2];
                });
                return hit;
            }""",
            [want, x_max],
        )
    except Exception:
        return None


def _click_visible_contact(page, name: str) -> bool:
    """只点「有实际坐标的可见行」，用真实鼠标点击（绕开 Playwright 的可见性/可操作性检查）。"""
    center = _visible_text_center(page, name, x_max=330)
    if not center:
        return False
    x, y = center
    if y < 55:  # 顶部标题栏区域不算会话行
        return False
    try:
        page.mouse.move(x, y)
        time.sleep(0.25)
        page.mouse.click(x, y)
        return True
    except Exception as e:
        logger.info("点击联系人坐标失败 %s: %s", name, str(e)[:80])
        return False


def _list_step_px(page) -> int:
    """会话列表滚轮步长 = 列表可视高度 × 0.6（与抓联系人同一套换算）。

    实测列表可视高约 708、行高约 67，固定 560/600/800 会跨屏跳行，
    虚拟列表一旦跳过目标行，就再也点不到该好友。
    """
    step = 560
    try:
        lh = int(page.evaluate(_LIST_HEIGHT_JS) or 0)
        if lh > 200:
            step = max(280, int(lh * 0.6))
    except Exception:
        pass
    return step


def open_contact_by_list(page, name: str, max_scrolls: int = 14) -> tuple[bool, int]:
    """在左侧会话列表里「逐个容器（主列表 → 折叠组 → 陌生人组）先回顶部，再逐屏向下」找目标会话。

    修复根因 1：群发时被点过的会话会跳到列表顶部，而虚拟列表把原来位置的行卸载，
    目标往往在**上方**；旧实现只在找不到时向下滚 600px 一次，越滚越远 →
    该好友固定失败（日志中 Yellow\xa0flashbang 连续多日失败）。这里改为双向可收敛的扫描。
    修复根因 2：左侧是多个独立滚动容器（主列表/折叠组/陌生人组），只滚主容器会漏掉
    "好友根本在折叠组或陌生人消息下面"这一类「怎么滚都找不到」。
    返回 (是否切成功, 扫过的屏数)，屏数用于失败时写诊断详情。
    """
    _ensure_list_visible(page)
    scanned = 0
    hit = {"ok": False}

    def _try_click() -> bool:
        if _click_visible_contact(page, name):
            time.sleep(random.uniform(1.6, 2.6))
            if verify_in_conversation(page, name):
                hit["ok"] = True
                return True
        return False

    for kind, sel, label in _LIST_CONTAINERS:
        n = _container_count(page, sel)
        if n == 0:
            if kind != "main":
                logger.info("%s：页面无此容器（该账号没有折叠/陌生会话），跳过查找「%s」", label, name)
            continue
        for idx in range(min(n, 4)):
            st = _container_state(page, sel, idx)
            if not st.get("found") or not st.get("visible"):
                logger.info("%s[%s]：容器不可见（折叠/收起），跳过查找", label, idx)
                continue
            screens = _walk_container(page, sel, f"{label}[{idx}]", _try_click, idx=idx,
                                     max_steps=max_scrolls)
            scanned += screens
            if hit["ok"]:
                logger.info("在%s找到了「%s」（累计扫描 %s 屏）", label, name, scanned)
                return True, scanned
            logger.info("%s[%s] 未找到「%s」（本容器扫 %s 屏）", label, idx, name, screens)
    # 兜底：回主列表顶部再点一次（虚拟列表重排后目标可能已被顶回顶部）
    _container_scroll_to(page, _LIST_CONTAINER_SEL["main"], 0, 0)
    time.sleep(0.8)
    if _try_click():
        return True, scanned
    return False, scanned


# 诊断只读「白名单属性」：tag / type / placeholder / role / aria-label / class（各截 60 字符）。
# 绝不 dump innerText / innerHTML —— 会话行里是好友昵称与私信内容，进了日志就等于把
# 好友列表泄给任何能看到日志的人（对照表 §1.3「安全诊断」的做法）。
_SAFE_META_JS = r"""
([sel, limit]) => {
  const cut = (s) => String(s || '').slice(0, 60);
  const attr = (el, n) => (el && el.getAttribute ? cut(el.getAttribute(n)) : '');
  return Array.from(document.querySelectorAll(sel)).slice(0, limit).map((el) => ({
    tag: el.tagName ? el.tagName.toLowerCase() : '',
    type: attr(el, 'type'),
    placeholder: attr(el, 'placeholder'),
    role: attr(el, 'role'),
    aria_label: attr(el, 'aria-label'),
    cls: cut(typeof el.className === 'string' ? el.className : ''),
  }));
}
"""


def _safe_node_meta(page, selector: str, limit: int = 3) -> list[dict]:
    """只取白名单属性节点指纹（不读任何文本）。"""
    try:
        return page.evaluate(_SAFE_META_JS, [selector, max(1, int(limit))]) or []
    except Exception:
        return []


def _fmt_meta(metas: list[dict]) -> str:
    if not metas:
        return "(无节点)"
    return " / ".join(
        "{tag}.{cls}".format(tag=m.get("tag") or "?", cls=(m.get("cls") or "")[:48]) for m in metas
    )


def _list_row_meta(page, limit: int = 2) -> tuple[int, list[dict]]:
    """左侧会话行数量 + 首/末行节点指纹（不输出行文本）。"""
    try:
        count = page.locator(_LIST_TITLE_SEL).count()
    except Exception:
        count = -1
    return count, _safe_node_meta(page, _LIST_TITLE_SEL, limit)


def _header_meta(page) -> str:
    """右侧会话标题区的节点指纹（不输出标题文本）。"""
    metas = _safe_node_meta(page, '[class*="RightPanelHeader"]', 3)
    return _fmt_meta(metas)


def _switch_diag(page, name: str, scanned: int = 0, searched: bool = False) -> str:
    """切换会话失败的诊断详情（只含数量/坐标/节点属性，没有别人的昵称）。"""
    want = _norm_name(name)
    st = _list_state(page)
    count, metas = _list_row_meta(page, 2)
    hit = 1 if _visible_text_center(page, name, x_max=330) else 0
    raw = name.encode("unicode_escape").decode("ascii")
    return (
        f'诊断：目标="{name}"（转义 {raw}｜归一化 "{want}"）\n'
        f"  · 会话列表：可见={st.get('visible')} scrollTop={st.get('top')}/{st.get('h')} "
        f"可视高={st.get('client')}｜DOM 当前渲染 {count if count >= 0 else '?'} 行（已扫描 {scanned} 屏）\n"
        f"  · 名字命中（可见行）：{hit}｜首行节点：{_fmt_meta(metas[:1])}｜末行节点：{_fmt_meta(metas[-1:])}\n"
        f"  · 右侧标题元素：{_header_meta(page)}｜搜索兜底：{'已尝试' if searched else '未尝试'}\n"
        f"  · 页面：{page.url[:90]}"
    )


def _search_diag(page, name: str) -> str:
    """搜索兜底失败时的诊断（只输出布尔/数量/节点属性，不输出搜索词或结果文本）。"""
    try:
        info = page.evaluate(
            """(want) => {
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const box = document.querySelector('input[placeholder*="搜索"]');
                let msgBtn = 0;
                let sameVisible = 0;
                document.querySelectorAll('div, span, button, a, p').forEach((el) => {
                    if (el.children.length) return;
                    const t = norm(el.textContent);
                    const r = el.getBoundingClientRect();
                    if (t === '发消息' && r.width > 2 && r.height > 2) msgBtn++;
                    if (t === want && r.width > 2 && r.height > 2) sameVisible++;
                });
                return {
                    hasBox: !!box,
                    hasValue: !!(box && box.value),
                    msgBtn, sameVisible,
                    listVisible: !!document.querySelector('[class*="conversationConversationListwrapper"]'),
                };
            }""",
            _norm_name(name),
        )
    except Exception as e:
        return f"  · 搜索态读取失败: {str(e)[:80]}"
    box_meta = _fmt_meta(_safe_node_meta(page, 'input[placeholder*="搜索"]', 1))
    return (
        f"  · 搜索框：存在={info.get('hasBox')} 有内容={info.get('hasValue')} {box_meta}"
        f"｜可见「发消息」入口 {info.get('msgBtn')} 个"
        f"｜搜索结果里可见同名项 {info.get('sameVisible')} 个"
        f"｜列表可见={info.get('listVisible')}"
    )


def search_and_open(page, name: str) -> bool:
    """兜底：用搜索框找好友。

    - 搜索时抖音会把左侧列表隐藏，**无论成败都必须退出搜索态**，否则后续好友全部点不到
    - 只点「真的有坐标」的可见结果，绝不点零尺寸隐藏节点（旧实现在这里抛 Element is not visible）
    - 用配置里的原名搜索（名字含 NBSP 时也要原样输入，抖音按字面匹配）
    """
    opened = False
    try:
        box = page.get_by_placeholder("搜索", exact=False).first
        if box.count() == 0:
            return False
        box.click()
        box.fill(name)
        time.sleep(4)
        btn = _visible_text_center(page, "发消息", x_max=420)
        if btn:
            page.mouse.click(btn[0], btn[1])
            time.sleep(4)
            opened = verify_in_conversation(page, name)
        if not opened:
            cand = _visible_text_center(page, name, x_max=420)
            if cand:
                page.mouse.click(cand[0], cand[1])
                time.sleep(3)
                btn = _visible_text_center(page, "发消息", x_max=420)
                if btn:
                    page.mouse.click(btn[0], btn[1])
                    time.sleep(3)
                opened = verify_in_conversation(page, name)
        if not opened:
            logger.info("搜索未找到 %s 的可点结果（名字含特殊字符/NBSP 或非可搜索用户时属正常）\n%s",
                        name, _search_diag(page, name))
    except Exception as e:
        logger.info("搜索打开 %s 失败: %s\n%s", name, str(e)[:120], _search_diag(page, name))
    finally:
        _ensure_list_visible(page)
    return opened


def _type_text(page, input_box, msg_text: str) -> bool:
    """把文字输入输入框（只输入，不按 Enter），返回文字是否成功进入输入框。"""
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
            # 只报长度/是否为空，不 dump 输入框内容（可能含上一条私信）
            logger.warning("文字未进入输入框：输入框字符数=%s（空=%s）", len(cur), not bool(cur.strip()))
            return False
        return True
    except Exception as e:
        logger.info("输入异常: %s", str(e)[:100])
        return False


def _type_and_send(page, input_box, msg_text: str) -> bool:
    """兼容旧调用：输入并立即按 Enter。"""
    if not _type_text(page, input_box, msg_text):
        return False
    try:
        page.keyboard.press("Enter")
        return True
    except Exception as e:
        logger.info("发送按键异常: %s", str(e)[:100])
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


# ---------------- P1：发送回执三态（confirmed / not_sent / uncertain） ----------------
#
# 只判「输入框清空」= 一定会假成功（登录态被服务端作废时页面不弹二维码、发送 API 静默失败，
# 输入框却会因 DOM 重排被清空）。这里按《借鉴/对照表.md》§4 Top3 第 3 条 +
# douyin-cloud-streak 的 MessageSendTracker 实现：监听 /v1/message/send 的 JSON 回执，
# path **结尾锚定**（避免 imapi 域下 conv/sync/ack 长轮询被误当回执）；
# 无回执时回退「发送前后气泡计数」；仍无法判定 → uncertain（绝不自动重试，更不能当成功）。
_CONFIRMED = "confirmed"
_NOT_SENT = "not_sent"
_UNCERTAIN = "uncertain"

# 只认发送接口本身：path 结尾必须是 message/send（或 send_message），
# 不能是 /conv、/sync、/ack 之类的长轮询。
_SEND_PATH_RE = re.compile(r"(?:v1/message/send|imapi\d*/(?:message/send|send_message))/?$")


class MessageSendTracker:
    """只读监听抖音私信底层的 /v1/message/send 响应，提取官方投递回执。

    reset() 记录 `_armed_at`：只接受本轮武装之后到达的回包，上一轮的迟到回包
    不会污染本轮判定（否则会把上一轮的成功当成本轮成功）。
    """

    def __init__(self, page):
        self.page = page
        self.last_receipt: dict | None = None
        self._armed_at = 0.0
        self._handler = self._on_response
        try:
            page.on("response", self._handler)
        except Exception:
            pass

    def _on_response(self, resp) -> None:
        try:
            path = urlsplit(resp.url or "").path
            if not _SEND_PATH_RE.search(path):
                return
            if resp.status != 200:
                return
            data = resp.json()
            if not isinstance(data, dict):
                return
            status_code = data.get("status_code")
            # 缺状态码的响应一律丢弃：默认 0 会把无关响应翻译成「发送成功」
            if status_code is None:
                return
            nested = data.get("data") or {}
            if not isinstance(nested, dict):
                nested = {}
            msg_id = nested.get("server_message_id") or nested.get("message_id")
            self.last_receipt = {
                "status_code": status_code,
                "server_message_id": str(msg_id) if msg_id else "",
                "status_msg": str(data.get("status_msg") or data.get("message") or "ok"),
                "time": time.time(),
            }
        except Exception:
            pass

    def reset(self) -> None:
        """进入新一轮发送前调用：清空旧回执并记录武装时间。"""
        self.last_receipt = None
        self._armed_at = time.time()

    def pop_recent(self, within_seconds: float = 15.0) -> dict | None:
        rec = self.last_receipt
        if rec and rec.get("time", 0) >= self._armed_at \
                and (time.time() - rec.get("time", 0)) <= within_seconds:
            self.last_receipt = None
            return rec
        return None

    def close(self) -> None:
        try:
            self.page.remove_listener("response", self._handler)
        except Exception:
            pass


def _receipt_verdict(receipt: dict) -> tuple[str, str]:
    """把服务端回执翻译成三态（cloud-streak 实测语义）。

    0=成功；1=对方不在单聊/已解散（陌生人只能发 1 条）；3=文案被安全审核拦截；
    5=被拉黑或对方已注销；未知状态码一律 uncertain（不默认成功）。
    """
    code = receipt.get("status_code")
    msg_id = receipt.get("server_message_id") or "-"
    if code == 0:
        return _CONFIRMED, f"服务端回执 status_code=0（server_message_id={msg_id}）"
    if code == 1:
        logger.warning(
            "发送回执 status_code=1：对方不在单聊会话中/会话已解散 —— "
            "识别为「陌生人只能发 1 条」场景，本轮标记 not_sent 且不重试"
        )
        return _NOT_SENT, "发送回执 status_code=1：对方不在单聊会话或会话已解散（陌生人只能发 1 条），本轮不重试"
    if code == 3:
        logger.warning("发送回执 status_code=3：文案被抖音安全审核拦截 → 请更换文案后重试")
        return _NOT_SENT, f"发送回执 status_code=3：文案被抖音安全审核拦截（{receipt.get('status_msg')}），请更换文案"
    if code == 5:
        logger.warning("发送回执 status_code=5：已被对方拉黑或对方已注销")
        return _NOT_SENT, "发送回执 status_code=5：已被对方拉黑或对方已注销"
    logger.warning("发送回执出现未知状态码 %s（%s）→ 无法判定，标记 uncertain 且不自动重试",
                   code, receipt.get("status_msg"))
    return _UNCERTAIN, f"未知服务端回执 status_code={code}（{receipt.get('status_msg')}），不自动重试"


def _count_msg_bubble(page, msg_text: str, input_top: float) -> int:
    """统计右侧会话消息区里与文案全等的可见元素数。

    位置过滤排除左侧列表预览（x<=300）与底部输入框（y>=input_top）。
    发送前后对比：数量增加 = 气泡真的出现在会话里 = 端到端已发出。
    """
    n = 0
    try:
        loc = page.get_by_text(msg_text, exact=True)
        for i in range(min(loc.count(), 10)):
            try:
                box = loc.nth(i).bounding_box()
            except Exception:
                continue
            if box and box.get("x", 0) > 300 and 60 < box.get("y", 0) < input_top - 6:
                n += 1
    except Exception:
        pass
    return n


def _settle_send(page, input_box, tracker: MessageSendTracker | None, msg_text: str,
                 bubble_before: int = 0, wait: float = 8, grace: float = 3.0) -> tuple[str, str]:
    """综合服务端回执 / 输入框 / 气泡计数，给出本轮发送的三态判定。

    - 有回执：以回执 status_code 为准（服务端权威结论）；
    - 无回执：输入框已清空时，必须在右侧消息区核实到「发送前没有、发送后出现」的气泡；
    - 仍无法判定：uncertain（点过发送但拿不到证据 → 绝不自动重试）。
    """
    cleared = _wait_input_cleared(input_box, msg_text, wait=wait)
    deadline = time.time() + grace
    while True:
        if tracker:
            rec = tracker.pop_recent(within_seconds=15.0)
            if rec:
                return _receipt_verdict(rec)
        if time.time() >= deadline:
            break
        time.sleep(0.5)

    if not cleared:
        return _UNCERTAIN, "发送后输入框未清空且未捕获发送回执 → 无法确认是否发出（不自动重试，请人工确认）"

    input_top = 9999.0
    try:
        b = input_box.bounding_box()
        if b:
            input_top = float(b.get("y", 9999))
    except Exception:
        pass
    bubble_after = _count_msg_bubble(page, msg_text, input_top)
    if bubble_after > bubble_before:
        logger.info("未捕获服务端回执，但右侧会话区气泡已核实新增（%s→%s），判定发送成功",
                    bubble_before, bubble_after)
        return _CONFIRMED, (f"无服务端回执，但会话区气泡计数 {bubble_before}→{bubble_after}"
                            f"（+{bubble_after - bubble_before}），判定已发出")
    logger.warning(
        "未捕获发送回执，且右侧会话区未核实到新增气泡（发送前 %s / 发送后 %s）→ 无法确认是否发出；"
        "常见原因：登录态被服务端作废（页面不弹二维码），请人工确认后必要时重新扫码",
        bubble_before, bubble_after,
    )
    return _UNCERTAIN, (f"未捕获发送回执，且会话区气泡数未增加（{bubble_before}→{bubble_after}）"
                        "→ 无法确认是否发出（不自动重试，请人工确认）")


def _send_text_segment(page, seg_text: str, tracker: MessageSendTracker | None = None) -> tuple[str, str]:
    """发送一段纯文字，返回三态 (confirmed / not_sent / uncertain, 说明)。"""
    send_attempted = False
    try:
        input_box = page.locator('div[contenteditable="true"]').first
        if input_box.count() == 0 or input_box.bounding_box() is None:
            return _NOT_SENT, "找不到聊天输入框"
        if detect_rate_limit(page):
            return _NOT_SENT, "检测到验证提示"
        if not _type_text(page, input_box, seg_text):
            return _NOT_SENT, "文字未能输入到输入框"
        # 发送前基线：右侧会话区里与文案全等的元素数（无回执时的兜底判据）
        input_top = 9999.0
        try:
            b = input_box.bounding_box()
            if b:
                input_top = float(b.get("y", 9999))
        except Exception:
            pass
        bubble_before = _count_msg_bubble(page, seg_text, input_top)
        if tracker:
            tracker.reset()
        send_attempted = True
        page.keyboard.press("Enter")
        return _settle_send(page, input_box, tracker, seg_text, bubble_before=bubble_before)
    except Exception as e:
        logger.info("文字段发送异常: %s", e)
        if send_attempted:
            return _UNCERTAIN, f"文字段发送异常（已点过发送，无法确认）：{str(e)[:80]}"
        return _NOT_SENT, f"文字段发送异常: {str(e)[:80]}"


def split_message_segments(msg_text: str, emojis_dir: Path | None = None) -> list[tuple[str, str]]:
    """把消息模板拆段：(text/image/flame/sticker, payload)。
    图片【图:文件名】；原生火花【火花】/【火焰】；任意贴纸【贴纸:名字】如【贴纸:比心】。"""
    emojis_dir = emojis_dir or EMOJIS_DIR
    segments: list[tuple[str, str]] = []
    if not (_EMOJI_MARKER_RE.search(msg_text) or _FLAME_MARKER_RE.search(msg_text) or _STICKER_MARKER_RE.search(msg_text)):
        return [("text", msg_text)]
    img_hits = list(_EMOJI_MARKER_RE.finditer(msg_text))
    flame_hits = list(_FLAME_MARKER_RE.finditer(msg_text))
    sticker_hits = list(_STICKER_MARKER_RE.finditer(msg_text))
    events = (
        [(m.start(), "img", m) for m in img_hits]
        + [(m.start(), "flame", m) for m in flame_hits]
        + [(m.start(), "sticker", m) for m in sticker_hits]
    )
    events.sort(key=lambda e: e[0])
    pos = 0
    for st, kind, m in events:
        if st > pos:
            seg = msg_text[pos:st].strip()
            if seg:
                segments.append(("text", seg))
        if kind == "img":
            fname = m.group(1).strip()
            fp = (emojis_dir / fname).resolve()
            try:
                fp.relative_to(emojis_dir.resolve())
            except ValueError:
                logger.warning("消息模板图片标记文件名非法，已跳过: %r", fname)
                pos = m.end()
                continue
            if fp.is_file():
                segments.append(("image", str(fp)))
            else:
                logger.warning("消息模板引用的图片不存在，已跳过: %s（图片目录: %s）", fname, emojis_dir)
        elif kind == "flame":
            segments.append(("flame", "续火花"))
        else:
            segments.append(("sticker", m.group(1).strip()))
        pos = m.end()
    tail = msg_text[pos:].strip()
    if tail:
        segments.append(("text", tail))
    return segments or [("text", msg_text)]


# 笑脸按钮 / 火花格参考坐标（录制得到；用于兜底与就近匹配）
_SMILEY_ANCHOR = (1279, 720)
_FLAME_CELL_ANCHOR = (1047, 501)


def _wait_consent_gone(page, timeout: float = 14.0) -> None:
    """等待可能的「同意/登录信息」弹窗自动消失（约 5 秒），期间不点击，避免点到遮罩。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            bad = page.evaluate(
                """() => {
                    const t = document.body ? document.body.innerText : '';
                    return /同意|记录|登录信息|个性化/.test(t);
                }"""
            )
        except Exception:
            bad = False
        # 简单起见：等待期直接空转，让弹窗自愈消失
        if not bad:
            # 再确认没有全屏遮罩盖住输入区
            try:
                ce = page.locator('[contenteditable="true"]').first
                if ce.count():
                    box = ce.bounding_box()
                    if box:
                        return  # 输入框可见可点即认为可以了
            except Exception:
                pass
        time.sleep(1)
    time.sleep(1)


def _bottom_icon_nearest(page, anchor_x: int, anchor_y: int):
    """输入区底部右侧图标中找离 anchor 最近的（排除文件上传/播放器）。"""
    return page.evaluate(
        """([ax, ay]) => {
            const cands = [];
            document.querySelectorAll('svg,path,img,div,[class*="icon" i]').forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width < 12 || r.width > 80 || r.height < 12 || r.height > 80) return;
                if (r.x < 1000 || r.y < 650 || r.y > 800) return;
                if (el.closest('.semi-upload') || el.closest('[class*="FileUpload"]') || el.closest('[class*="player" i]')) return;
                const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
                cands.push({ x: Math.round(cx), y: Math.round(cy), cls: String(el.className || '').slice(0, 50), tag: el.tagName });
            });
            if (!cands.length) return null;
            cands.sort((a, b) => (Math.abs(a.x - ax) + Math.abs(a.y - ay)) - (Math.abs(b.x - ax) + Math.abs(b.y - ay)));
            return cands[0];
        }""",
        [anchor_x, anchor_y],
    )


def _panel_cell_nearest(page, anchor_x: int, anchor_y: int):
    """在弹开的贴纸/表情面板（含 ≥6 个等大图片格的容器）里找离 anchor 最近的格。"""
    return page.evaluate(
        """([ax, ay]) => {
            const cands = [];
            document.querySelectorAll('img').forEach(im => {
                const r = im.getBoundingClientRect();
                if (r.width < 36 || r.width > 240 || r.height < 36 || r.height > 240) return;
                if (r.y < 250 || r.y > 760 || r.x < 700) return;
                // 所在容器必须是“图片格容器”（含不少同级/后代图片格）
                let p = im.parentElement, container = null;
                for (let k = 0; k < 4 && p; k++) {
                    const imgs = p.querySelectorAll ? p.querySelectorAll('img') : [];
                    let cnt = 0;
                    imgs.forEach(o => { const q = o.getBoundingClientRect(); if (q.width >= 30 && q.width <= 260 && q.height >= 30) cnt++; });
                    if (cnt >= 6) { container = p; break; }
                    p = p.parentElement;
                }
                if (!container) return;
                const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
                cands.push({ x: Math.round(cx), y: Math.round(cy), cls: String(im.className || '').slice(0, 60), src: (im.src || '').slice(0, 80) });
            });
            if (!cands.length) return null;
            cands.sort((a, b) => (Math.abs(a.x - ax) + Math.abs(a.y - ay)) - (Math.abs(b.x - ax) + Math.abs(b.y - ay)));
            return cands[0];
        }""",
        [anchor_x, anchor_y],
    )


def _panel_open_check(page) -> dict:
    """判断贴纸面板是否打开：返回面板容器信息与图片格数量。"""
    return page.evaluate(
        """() => {
            const out = { containers: [], grids: 0 };
            document.querySelectorAll('div').forEach(div => {
                const r = div.getBoundingClientRect();
                if (r.width < 200 || r.height < 80 || r.y > 760 || r.x < 700) return;
                const imgs = div.querySelectorAll('img');
                let cnt = 0;
                imgs.forEach(o => { const q = o.getBoundingClientRect(); if (q.width >= 30 && q.width <= 260 && q.height >= 30 && q.height <= 260) cnt++; });
                if (cnt >= 6 && !out.containers.some(c => c.cls === String(div.className || ''))) {
                    out.containers.push({ cls: String(div.className || '').slice(0, 70), x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height), imgs: cnt });
                }
            });
            out.containers = out.containers.slice(0, 5);
            out.grids = out.containers.length;
            return out;
        }"""
    )


def send_native_flame(page) -> tuple[bool, str]:
    """发抖音原生「火花」贴纸：点输入框右侧笑脸 → 点贴纸面板里的火花格 → 自动发出。"""
    try:
        _wait_consent_gone(page)
        # 点笑脸（就近匹配锚点）
        icon = _bottom_icon_nearest(page, _SMILEY_ANCHOR[0], _SMILEY_ANCHOR[1])
        if not icon:
            return False, "底部找不到笑脸/表情图标"
        page.mouse.click(icon["x"], icon["y"])
        logger.info("原生火花：已点表情图标 (%s,%s)", icon["x"], icon["y"])
        time.sleep(2.5)
        panel = _panel_open_check(page)
        logger.info("原生火花：面板检测=%s", panel)
        # 点火花格（就近锚点，且必须在图片格容器里）
        cell = _panel_cell_nearest(page, _FLAME_CELL_ANCHOR[0], _FLAME_CELL_ANCHOR[1])
        if not cell:
            return False, "表情面板里没找到贴纸格（可能没弹出）"
        page.mouse.click(cell["x"], cell["y"])
        logger.info("原生火花：已点火花贴纸格 (%s,%s) %s src=%s", cell["x"], cell["y"], cell.get("cls"), cell.get("src"))
        # 点贴纸即自动发送；留时间让它发出
        time.sleep(3.5)
        # 兜底：如果没自动发，输入框如有内容按 Enter
        try:
            ce = page.locator('[contenteditable="true"]').first
            if ce.count():
                ce.click()
                page.keyboard.press("Enter")
        except Exception:
            pass
        time.sleep(1.5)
        logger.info("已发送原生火花贴纸给当前会话")
        return True, "ok"
    except Exception as e:
        logger.info("原生火花发送异常: %s", str(e)[:120])
        return False, f"原生火花发送异常: {str(e)[:90]}"


def _normalize_sticker_name(raw: str) -> str | None:
    """把用户输入的贴纸名映射成面板里公认的名字（大小写/空格/别名）。"""
    q = (raw or "").strip().lower().replace(" ", "")
    for canon, aliases in KNOWN_STICKER_SYNONYMS.items():
        if canon.lower().replace(" ", "") == q or any(a.lower().replace(" ", "") == q for a in aliases):
            return canon
    # 兜底：原样作为面板文字查找（可能面板里有）
    return raw.strip() or None


def _find_sticker_cell_by_label(page, canon: str) -> dict | None:
    """在表情面板里按文字标签找贴纸格子：先把标签滚入可视区再返回格子中心坐标。"""
    aliases = [canon] + KNOWN_STICKER_SYNONYMS.get(canon, [])
    return page.evaluate(
        """async (labels) => {
            const pick = () => {
                const out = [];
                document.querySelectorAll('div,span').forEach(el => {
                    if (el.children.length !== 0) return;
                    const t = (el.textContent || '').trim();
                    if (!t || t.length > 12) return;
                    if (!labels.some(L => L === t)) return;
                    let p = el.parentElement;
                    for (let k = 0; k < 5 && p; k++) {
                        const imgs = p.querySelectorAll ? p.querySelectorAll('img') : [];
                        let cnt = 0, cand = null;
                        imgs.forEach(o => { const r = o.getBoundingClientRect(); if (r.width >= 30 && r.width <= 120 && r.height >= 30) { cnt++; if (!cand) cand = o; } });
                        if (cnt >= 1) {
                            const r = cand.getBoundingClientRect();
                            if (r.y > 250 && r.y < 800 && r.x > 700) {
                                out.push({ el, cand, label: t });
                                return;
                            }
                        }
                        p = p.parentElement;
                    }
                });
                return out[0] || null;
            };
            let found = pick();
            if (!found) return null;
            try { found.el.scrollIntoView({ block: 'center' }); } catch (e) {}
            await new Promise(res => setTimeout(res, 350));
            found = pick();
            if (!found) return null;
            const r = found.cand.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) return null;
            return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), label: found.label };
        }""",
        aliases,
    )


def send_native_sticker(page, raw_name: str) -> tuple[bool, str]:
    """发任意原生贴纸（表情面板内的名字，如 续火花/比心/笑死…）：点笑脸 → 按名定位格子 → 点即自动发。"""
    canon = _normalize_sticker_name(raw_name)
    if not canon:
        return False, f"贴纸名不能为空"
    try:
        _wait_consent_gone(page)
        icon = _bottom_icon_nearest(page, _SMILEY_ANCHOR[0], _SMILEY_ANCHOR[1])
        if not icon:
            return False, "底部找不到表情图标"
        page.mouse.click(icon["x"], icon["y"])
        time.sleep(2.5)
        # 先按文字标签精确定位
        cell = _find_sticker_cell_by_label(page, canon)
        if not cell and canon in _STICKER_CELL_ANCHOR:
            logger.info("贴纸「%s」未按文字定位，用锚点兜底", canon)
            a = _STICKER_CELL_ANCHOR[canon]
            cell = _panel_cell_nearest(page, a[0], a[1])
        if not cell:
            known = "、".join(KNOWN_STICKER_SYNONYMS.keys())
            return False, f"面板里没找到贴纸「{raw_name}」（目前可发：{known} 等）"
        page.mouse.click(cell["x"], cell["y"])
        logger.info("贴纸「%s」已点格子 (%s,%s)", canon, cell["x"], cell["y"])
        time.sleep(3.5)
        try:
            ce = page.locator('[contenteditable="true"]').first
            if ce.count():
                ce.click()
                page.keyboard.press("Enter")
        except Exception:
            pass
        time.sleep(1.5)
        logger.info("已发送原生贴纸「%s」给当前会话", canon)
        return True, "ok"
    except Exception as e:
        logger.info("贴纸「%s」发送异常: %s", raw_name, str(e)[:120])
        return False, f"贴纸「{raw_name}」发送异常: {str(e)[:90]}"


def send_media_message(page, msg_text: str, emojis_dir: Path | None = None, to_name: str = "",
                       tracker: "MessageSendTracker | None" = None) -> tuple[str, str]:
    """按模板段依次发送：文字走 Enter，图片走原生图片通道，火花/贴纸走原生贴纸面板。

    返回三态 (confirmed / not_sent / uncertain, 说明)。多段模板只要有一段点过发送但
    拿不到证据，整条模板就不能重试（重发会刷屏）→ 记 uncertain。
    """
    segments = split_message_segments(msg_text, emojis_dir)
    sent_any = False
    for kind, payload in segments:
        if kind == "text":
            state, why = _send_text_segment(page, payload, tracker=tracker)
        else:
            ok, why = (send_image_to_page(page, payload) if kind == "image"
                       else send_native_sticker(page, payload) if kind == "sticker"
                       else send_native_flame(page))
            # 图片/贴纸/火花无法用 message/send 的 JSON 回执判定：失败时按「已发起但无法确认」处理，
            # 保证不会被上层当成「发送前失败」而整条重发。
            state = _CONFIRMED if ok else _UNCERTAIN
        if state == _CONFIRMED:
            sent_any = True
            continue
        seg_desc = {"text": "文字", "image": "图片", "sticker": "原生贴纸", "flame": "原生火花"}.get(kind, kind)
        payload_desc = (payload if kind == "text" else Path(str(payload)).name if kind == "image" else str(payload))
        payload_desc = str(payload_desc).replace("\n", " ")[:60]
        detail = (f"段类型={seg_desc}｜段内容={payload_desc!r}｜右侧标题元素={_header_meta(page)}"
                  f"｜完整模板长度={len(msg_text)}")
        logger.warning("向 %s 发送失败（%s 段，%s）：%s\n  · %s", to_name, seg_desc, state, why, detail)
        # 已发过前面的段 → uncertain；第一段就没发出去 → not_sent
        if state == _UNCERTAIN or sent_any:
            return _UNCERTAIN, f"{why}（{detail}）"
        return _NOT_SENT, f"{why}（{detail}）"
    return (_CONFIRMED if sent_any else _NOT_SENT), "ok"


def send_image_to_page(page, image_path: str) -> tuple[bool, str]:
    """在当前已打开会话里发一张本地图片：挂附件 → 等抖音「发送给 XX」确认框 → 点「发送」。"""
    fname = Path(image_path).name
    try:
        fi = page.locator('.messageEditorimChatEditorContainer input[type="file"]').first
        if fi.count() == 0:
            fi = page.locator('[class*="MsgInputFileUpload"] input[type="file"]').first
        if fi.count() == 0:
            fi = page.locator('input.semi-upload-hidden-input').first
        if fi.count() == 0:
            return False, "找不到图片上传框"
        fi.set_input_files(image_path)
        sure = page.locator('button.MsgInputSendFileModalbtnSure').first
        ok_click = False
        for _ in range(2):  # 确认框可能晚一点点出现
            try:
                sure.wait_for(state="visible", timeout=6000)
            except Exception:
                pass
            if sure.count() and sure.is_visible():
                sure.click(force=True, timeout=5000)
                ok_click = True
                break
            time.sleep(1.5)
        if not ok_click:
            # 兜底：没弹确认框时按 Enter（旧界面形态）
            ce = page.locator('[contenteditable="true"]').first
            if ce.count():
                ce.click()
                page.keyboard.press("Enter")
                ok_click = True
        if not ok_click:
            return False, "未能点下图片发送按钮"
        # 等确认框关闭（附件已发出）
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                if sure.count() == 0 or not sure.is_visible():
                    break
            except Exception:
                break
            time.sleep(0.5)
        time.sleep(1.5)
        logger.info("已发送图片给当前会话：%s", fname)
        return True, "ok"
    except Exception as e:
        logger.info("发送图片 %s 异常: %s", fname, str(e)[:120])
        return False, f"发送图片异常: {str(e)[:80]}"


def _clear_search_box(page) -> None:
    """P3：清空左侧搜索框（搜索失败后列表可能仍被搜索态遮住/0 行）。"""
    try:
        box = page.get_by_placeholder("搜索", exact=False).first
        if box.count():
            box.click()
            box.fill("")
            page.keyboard.press("Escape")
            time.sleep(0.6)
    except Exception:
        pass


def _reload_chat_until_ready(page, timeout_s: int = 60) -> int:
    """P3：搜索兜底结束后 → 清空搜索框 → 重载 /chat → 等会话列表就绪。

    搜索态下抖音会把左侧列表 display:none；搜索失败后直接判「找不到好友」会把
    偶发的渲染失败当永久失败。这里强制重载并复用 _wait_conversation_rows 等就绪，
    返回可见行数（0 = 仍未就绪，由调用方返回 PERMANENT 并提示重试/重新扫码）。
    """
    _clear_search_box(page)
    logger.warning(
        "搜索兜底结束，左侧会话列表可能未就绪 → 清空搜索框并重载私信页，"
        "等待会话列表就绪（最多 %s 秒）", timeout_s,
    )
    try:
        page.goto(CHAT_URL, timeout=90000, wait_until="domcontentloaded")
    except Exception as e:
        logger.info("重载私信页失败: %s", str(e)[:80])
    rows = _wait_conversation_rows(page, timeout_s)
    if rows:
        logger.info("私信页已重新就绪：当前可见 %s 行", rows)
    else:
        logger.warning("私信页仍未就绪：等待 %s 秒后仍为 0 行，建议重试或重新扫码", timeout_s)
    return rows


def send_to_contact(page, name: str, msg_text: str, dry_run: bool,
                    tracker: "MessageSendTracker | None" = None) -> tuple[str, str]:
    """切到好友会话并发送；返回三态 (confirmed / not_sent / uncertain, 说明)。"""
    # 上一轮如果走过搜索兜底，左侧列表可能仍被搜索态遮住 → 先复位，否则所有点击都报 not visible
    _ensure_list_visible(page)

    # 会话列表实测 24~37 秒才渲染：先等到出现会话行，再执行「查找 → 点击 → 输入 → 发送」。
    # 列表还没出来时单元格/搜索都点不到，绝不能直接报「未能在会话列表里找到该好友」。
    rows = 0
    try:
        rows = int(page.evaluate(_VISIBLE_ROWS_JS) or 0)
    except Exception:
        rows = 0
    if rows <= 0:
        logger.info("等待会话列表渲染后再查找好友：%s", name)
        rows = _wait_conversation_list_ready(page)
    if rows <= 0:
        logger.warning(
            "未能进入会话：等待会话列表 2 轮仍为 0 行（目标 %s），列表状态=%s",
            name, _list_state(page),
        )
        return _NOT_SENT, _LIST_NOT_READY_MSG

    switched, scanned = open_contact_by_list(page, name)
    searched = False
    if not switched:
        searched = True
        switched = search_and_open(page, name)
        if switched:
            logger.info("经搜索兜底切到会话：%s", name)
        else:
            # P3：搜索失败后列表可能 0 行 / 仍被搜索态遮住 → 先恢复列表再二次查找，
            # 不能把一次偶发渲染失败直接判成「好友不存在」。
            rows2 = _reload_chat_until_ready(page, 60)
            if rows2 <= 0:
                detail = _switch_diag(page, name, scanned, True)
                logger.warning(
                    "搜索兜底后重载私信页仍未就绪（0 行），本轮放弃；建议重试或重新扫码。\n%s", detail,
                )
                return _NOT_SENT, _LIST_NOT_READY_MSG + "\n" + detail
            switched, scanned2 = open_contact_by_list(page, name)
            scanned += scanned2
            if switched:
                logger.info("重载列表后二次查找切到会话：%s（累计扫描 %s 屏）", name, scanned)
    elif scanned > 1:
        logger.info("在会话列表扫描 %s 屏后切到会话：%s", scanned, name)
    if not switched:
        detail = _switch_diag(page, name, scanned, searched)
        logger.warning("未能切到该好友的会话，放弃本次发送：\n%s", detail)
        return _NOT_SENT, "未能在会话列表里找到该好友（已滚动查找 + 搜索兜底 + 重载后二次查找）\n" + detail
    logger.info("已切到该好友的会话：%s（扫描 %s 屏）", name, scanned)

    if detect_rate_limit(page):
        return _NOT_SENT, "检测到「操作频繁 / 安全验证」提示"

    input_box = page.locator('div[contenteditable="true"]').first
    try:
        if input_box.count() == 0 or input_box.bounding_box() is None:
            return _NOT_SENT, "找不到聊天输入框"
        input_box.wait_for(state="visible", timeout=8000)
    except Exception:
        return _NOT_SENT, "找不到聊天输入框"

    if dry_run:
        return _CONFIRMED, "dry-run（干跑，未真实发送）"

    # 含【图:】【火花】【贴纸:】标记的模板走分段发送（文字 + 原生图片 + 原生贴纸）
    if _EMOJI_MARKER_RE.search(msg_text) or _FLAME_MARKER_RE.search(msg_text) or _STICKER_MARKER_RE.search(msg_text):
        return send_media_message(page, msg_text, to_name=name, tracker=tracker)

    if detect_rate_limit(page):
        return _NOT_SENT, "发送前检测到验证提示"
    return _send_text_segment(page, msg_text, tracker=tracker)


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
        const pickConvId = (el) => {
            // 只认显式 data-* 会话标识，不用 React 自动生成的 id（跨会话不稳定）
            let p = el;
            for (let k = 0; k < 5 && p; k++) {
                for (const a of ['data-conv-id', 'data-conversation-id', 'data-conversationid', 'data-cid', 'data-uid']) {
                    const v = p.getAttribute && p.getAttribute(a);
                    if (v && String(v).trim()) return String(v).trim();
                }
                p = p.parentElement;
            }
            return '';
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
                conv_id: pickConvId(wrap) || pickConvId(findRoot(wrap)),
                streak: s ? (s.textContent || '').trim() : '',
                avatar: pickAvatar(wrap),
                time: timeEl ? (timeEl.textContent || '').trim() : '',
            });
        });
        return out;
    }
"""

# 会话列表就绪检测：只数当前 DOM 里存在的会话行（虚拟列表会卸载滚出屏幕的行）
_VISIBLE_ROWS_JS = r"""
    () => {
        const a = document.querySelectorAll('[data-e2e="conversation-item"], .conversationConversationItemwrapper').length;
        if (a) return a;
        return document.querySelectorAll('.conversationConversationItemtitle').length;
    }
"""

# 会话列表可视高度：滚轮步长按它打折，保证相邻两屏有重叠、不会跳过行
_LIST_HEIGHT_JS = r"""
    () => {
        const el = document.querySelector('[class*="conversationConversationListwrapper"]');
        return el ? el.clientHeight : 0;
    }
"""

# 左侧其实是**多个独立滚动容器**：主列表 / 折叠组 / 陌生人组。
# 只滚主容器会把「怎么滚都找不到」固化成 bug —— 好友可能根本在折叠组或陌生人组里。
# 折叠组、陌生人组容器选择器出自 DouYinSparkFlow 的独家发现（对照表附录 A，可信度★★★★）。
_LIST_CONTAINERS: tuple[tuple[str, str, str], ...] = (
    ("main", '[class*="conversationConversationListwrapper"]', "主列表"),
    ("fold", '[class*="conversationFoldConversationListlistWrapper"]', "折叠组"),
    ("stranger", '[class*="conversationStrangerConversationListlistWrapper"]', "陌生人组"),
)
_LIST_CONTAINER_SEL = {kind: sel for kind, sel, _label in _LIST_CONTAINERS}

_CONTAINER_STATE_JS = r"""
    ([sel, idx]) => {
        const els = [...document.querySelectorAll(sel)];
        const el = els[idx];
        if (!el) return { found: false };
        const cs = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return {
            found: true,
            top: Math.round(el.scrollTop),
            client: el.clientHeight,
            height: el.scrollHeight,
            visible: cs.display !== 'none' && cs.visibility !== 'hidden' && r.width > 0 && r.height > 0,
        };
    }
"""

_CONTAINER_SCROLL_JS = r"""
    ([sel, idx, top]) => {
        const els = [...document.querySelectorAll(sel)];
        const el = els[idx];
        if (!el) return false;
        el.scrollTo({ top: Math.max(0, top), behavior: 'instant' });
        return true;
    }
"""


def _container_state(page, sel: str, idx: int = 0) -> dict:
    """某个滚动容器的位置/高度/是否可见（逐个容器扫描的基础）。"""
    try:
        return page.evaluate(_CONTAINER_STATE_JS, [sel, idx]) or {"found": False}
    except Exception:
        return {"found": False}


def _container_scroll_to(page, sel: str, idx: int, top: float) -> None:
    """把指定容器滚到绝对坐标 top（绝对坐标比 +=600 可靠：布局变化不会累积误差）。"""
    try:
        page.evaluate(_CONTAINER_SCROLL_JS, [sel, idx, int(top)])
    except Exception:
        pass


def _container_count(page, sel: str) -> int:
    try:
        return int(page.evaluate("(sel) => document.querySelectorAll(sel).length", sel) or 0)
    except Exception:
        return 0


def _walk_container(page, sel: str, label: str, on_screen, idx: int = 0,
                    max_steps: int = 100) -> int:
    """把**单个**滚动容器从顶滚到底，每换一屏调一次 on_screen()（返回 True 立即结束）。

    真到底判定（对照表痛点 3 ②，比“连续两屏没动”严谨）：
      ① top + client >= scrollHeight - 8，且再等 1 秒高度不再增长（虚拟列表到底会补一页）；
      ② 或连续两屏 scrollTop 都不再变化。
    步长取可视高 0.4（1 屏步长会跳过行）。返回走过的屏数（0 = 容器不存在/不可见）。
    """
    st = _container_state(page, sel, idx)
    if not st.get("found"):
        return 0
    if not st.get("visible"):
        logger.info("%s：容器存在但当前不可见（折叠/收起），跳过", label)
        return 0
    client = int(st.get("client") or 0)
    if client <= 0:
        return 0
    _container_scroll_to(page, sel, idx, 0)
    try:
        page.wait_for_timeout(500)
    except Exception:
        time.sleep(0.5)
    step = max(280, int(client * 0.4))
    stall = 0
    prev_top = -1
    screens = 0
    for _ in range(max_steps):
        screens += 1
        try:
            # 只认显式 True 作为“找到/收齐，提前结束”；普通回调返回行数等其它值不算
            if on_screen and on_screen() is True:
                return screens
        except Exception as e:
            logger.info("%s：本屏处理异常（继续滚动）: %s", label, str(e)[:80])
        st = _container_state(page, sel, idx)
        top = int(st.get("top") or 0)
        height = int(st.get("height") or 0)
        client = int(st.get("client") or 0)
        if client and height and top + client >= height - 8:
            # 真到底：先跨过前端 1000ms 防抖，高度不再增长才算到底
            try:
                page.wait_for_timeout(1000)
            except Exception:
                time.sleep(1.0)
            st2 = _container_state(page, sel, idx)
            if int(st2.get("height") or 0) <= height:
                if on_screen:
                    try:
                        on_screen()      # 最后一屏再补一次（虚拟列表到底才补齐）
                    except Exception:
                        pass
                break
            height = int(st2.get("height") or 0)
        if top == prev_top:
            stall += 1
            if stall >= 2:
                break
        else:
            stall = 0
        prev_top = top
        _container_scroll_to(page, sel, idx, min(top + step, max(0, height - client)))
        try:
            page.wait_for_timeout(600)
        except Exception:
            time.sleep(0.6)
    return screens


def _scan_all_containers(page, on_screen, max_steps: int = 100) -> list[tuple[str, int]]:
    """按「主列表 → 折叠组 → 陌生人组」逐个容器滚到底，返回 [(标签, 屏数), ...]。"""
    walked: list[tuple[str, int]] = []
    for kind, sel, label in _LIST_CONTAINERS:
        n = _container_count(page, sel)
        if n == 0:
            if kind == "main":
                logger.warning("%s：未找到滚动容器 %s", label, sel)
            else:
                logger.info("%s：页面无此容器（该账号没有折叠/陌生会话）", label)
            continue
        logger.info("扫描%s：发现 %s 个滚动容器", label, n)
        for idx in range(min(n, 4)):
            screens = _walk_container(page, sel, f"{label}[{idx}]", on_screen, idx=idx,
                                     max_steps=max_steps)
            if screens:
                walked.append((f"{label}[{idx}]", screens))
                logger.info("%s[%s] 扫描完成：%s 屏", label, idx, screens)
    return walked


def _rescan_avatars_from_top(page, collected_by_name: dict, avatar_map: dict,
                             collect_screen) -> int:
    """P4：整轮扫描结束后回到各容器顶部，再扫一遍可见区，补采缺失头像。

    只补当前屏（不全量滚动），成本低；有就补上，仍无则保留空（前端有首字母兜底）。
    collect_screen 为抓当前屏头像的回调（生产环境是 _collect_avatar_screen）。
    返回补采成功个数。
    """
    missing = [n for n in collected_by_name if not avatar_map.get(n)]
    if not missing:
        return 0
    have_before = sum(1 for n in collected_by_name if avatar_map.get(n))
    for _kind, sel, _label in _LIST_CONTAINERS:
        n = _container_count(page, sel)
        if n == 0:
            continue
        for idx in range(min(n, 2)):
            st = _container_state(page, sel, idx)
            if not st.get("found") or not st.get("visible"):
                continue
            _container_scroll_to(page, sel, idx, 0)
            try:
                page.wait_for_timeout(1200)   # 等顶部首屏头像图加载
            except Exception:
                time.sleep(1.2)
            collect_screen(tries=3)
    have_after = sum(1 for n in collected_by_name if avatar_map.get(n))
    added = max(0, have_after - have_before)
    logger.info(
        "头像整轮结束后回顶补采：补采成功 %s 个（%s→%s/%s 已有头像）",
        added, have_before, have_after, len(collected_by_name),
    )
    return added

# 当前可见屏「名字 → 头像URL」：虚拟滚动下头像只存在于当前屏，必须每滚一屏记一次。
# 对照表痛点 2「★ 3 个真根因」都在这段 JS 里处理：
#   ① 只认「真的加载完」的图（img.complete && naturalWidth>0），没加载好的标记 pending，
#      下一屏/下一轮再补采；顺手回退 data-src / srcset（懒加载占位符）。
#   ② 协议相对 URL（//p3-pc-sign...）补 https:。
#   ③ 名字以行内 title 文本为准，并带上 kind/slot（容器 + 行位置），
#      供 Python 侧识别「同一行改名漂移：以最新一次为准并保留头像」。
_VISIBLE_AVATAR_JS = r"""
    () => {
      const out = [];
      const normUrl = (u) => {
        if (!u) return '';
        u = String(u).trim();
        if (u.startsWith('//')) u = 'https:' + u;
        return /^https?:/i.test(u) ? u : '';
      };
      const rows = [...document.querySelectorAll('[data-e2e="conversation-item"], .conversationConversationItemwrapper')];
      for (const r of rows) {
        const t = r.querySelector('.conversationConversationItemtitle');
        if (!t) continue;
        const name = (t.textContent || '').trim();     // 行内 title 文本为准（改名后 DOM 直接换文本）
        if (!name) continue;
        let src = '';
        let ready = false;
        let via = '';
        const im = r.querySelector('[class*="rowArea1"] img') || r.querySelector('img[class*="ConversationIcon"]') || r.querySelector('img');
        if (im) {
          const loaded = !!(im.complete && im.naturalWidth > 0);   // ★ 必须“真的加载完”才算数
          if (loaded) {
            src = normUrl(im.currentSrc || im.src);
            via = 'img';
          }
          if (!src) src = normUrl(im.getAttribute('data-src'));     // 懒加载占位符回退
          if (!src) {
            const ss = im.getAttribute('srcset') || '';
            const first = ss.split(',')[0] || '';
            src = normUrl(first.trim().split(/\s+/)[0]);
          }
          ready = !!(src && loaded);
        }
        if (!ready) {
          // 兜底：背景图（同样做协议相对归一化）；CSS 图无法判断 complete，视为可用
          let bg = '';
          for (const el of r.querySelectorAll('*')) {
            const st = el.getAttribute('style') || '';
            const m = st.match(/url\(["']?(.*?)["']?\)/i);
            if (m && m[1]) { bg = normUrl(m[1]); if (bg) { via = 'css'; break; } }
          }
          if (bg) { src = bg; ready = true; }
        }
        let kind = 'main';
        if (r.closest('[class*="conversationFoldConversationListlistWrapper"]')) kind = 'fold';
        else if (r.closest('[class*="conversationStrangerConversationListlistWrapper"]')) kind = 'stranger';
        const rect = r.getBoundingClientRect();
        out.push({
          name: name,
          src: src || '',
          ok: ready,
          pending: !ready,
          via: via,
          kind: kind,
          slot: kind + ':' + Math.round(rect.y / 8),
        });
      }
      return out;
    }
"""

_STREAK_DEBUG_JS = None  # 诊断已完成，已移除


# ---------------- 头像本地缓存（移植 cloud-streak/core/avatar.py，含两处改进） ----------------
# 抖音头像 URL 带 x-expires / x-signature，过期即 403 —— 任何形式的“URL 转发”都不可靠，
# 所以抓列表时就把头像落到 data/avatars/<账号>/<hash>.<ext>，前端长期可用。
_AVATAR_TIMEOUT = 4                       # 单次下载超时（秒）
_AVATAR_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_AVATAR_LOCK_MAX = 2 * 1024 * 1024        # 单张上限 2MB
_AVATAR_LOCKS: dict[str, threading.Lock] = {}
_AVATAR_LOCKS_GUARD = threading.Lock()


def _avatar_lock(key: str) -> threading.Lock:
    with _AVATAR_LOCKS_GUARD:
        lock = _AVATAR_LOCKS.get(key)
        if lock is None:
            lock = _AVATAR_LOCKS[key] = threading.Lock()
        return lock


def avatar_hash(url: str) -> str:
    """头像缓存 key：只取 URL 的 path（去 host、去 query），并归一尺寸变体。

    抖音头像 URL 形如
      https://p3-pc-sign.douyinpic.com/aweme/100x100/<id>.jpeg?x-expires=...&x-signature=...
    ① 直接 md5 整条 URL → 签名每次都变 → 永不命中缓存；
    ② 只去 query 仍带 host，而 douyinpic 会在 p3/p9/p26 之间轮换 → 同样不命中；
    所以用 urlsplit(url).path，并把 /aweme/100x100/ 与 /aweme/1080x1080/ 视为同一张。
    """
    u = (url or "").strip()
    if u.startswith("//"):
        u = "https:" + u
    try:
        path = urlsplit(u).path or ""
    except Exception:
        path = u.split("?")[0]
    if not path:
        path = u.split("?")[0]
    path = re.sub(r"/aweme/\d+x\d+/", "/aweme/_/", path)      # 尺寸目录归一
    if "~" in path:                                           # ~c5_100x100.jpeg 等处理参数只留扩展名
        head, _sep, tail = path.partition("~")
        ext = Path(tail).suffix or Path(path).suffix
        path = head + (ext if ext else "")
    return hashlib.md5(path.encode("utf-8")).hexdigest()[:16]


def _avatar_ext(url: str) -> str:
    try:
        ext = Path(urlsplit(url).path).suffix.lower()
    except Exception:
        ext = ""
    return ext if ext in _AVATAR_EXTS else ".jpg"


def account_data_root(acc_dir: Path | None = None) -> Path:
    """账号数据根目录（跟随工作区）：acc_dir = <root>/accounts/<id> → <root>。"""
    if acc_dir is None:
        return DATA_DIR
    try:
        return Path(acc_dir).resolve().parent.parent
    except Exception:
        return DATA_DIR


def avatars_dir(acc_dir: Path | None = None) -> Path:
    """头像缓存根目录：<数据根>/avatars（默认工作区即 data/avatars）。"""
    return account_data_root(acc_dir) / "avatars"


def save_avatar_file(url: str, acc_id: str, avatar_dir: Path) -> str | None:
    """下载头像到 <avatar_dir>/<acc_id>/<hash>.<ext>，返回 /avatars/<acc_id>/<fname>。

    avatar_dir 传 <数据根>/avatars（见 avatars_dir()）。
    必带 Referer + UA（不带就是 403）；超时 4s、最多重试 1 次；失败**静默返回 None**，
    调用方回退原始 URL。写盘走 .tmp + replace 原子替换，并发用文件名锁，杜绝半张图。
    """
    if not url or not isinstance(url, str):
        return None
    try:
        u = url.strip()
        if u.startswith("//"):
            u = "https:" + u
        if not re.match(r"^https?://", u, re.I):
            return None
        ext = _avatar_ext(u)
        fname = f"{avatar_hash(u)}{ext}"
        d = Path(avatar_dir) / acc_id
        fp = d / fname
        if fp.exists() and fp.stat().st_size > 0:                 # 锁外快路径
            return f"/avatars/{acc_id}/{fname}"
        with _avatar_lock(f"{acc_id}:{fname}"):
            if fp.exists() and fp.stat().st_size > 0:             # 锁内二次检查
                return f"/avatars/{acc_id}/{fname}"
            d.mkdir(parents=True, exist_ok=True)
            content = None
            for _attempt in range(2):
                try:
                    req = urllib.request.Request(u, headers={
                        "Referer": "https://www.douyin.com/",     # ★ 不带就 403
                        "User-Agent": _CHROME_UA,
                    })
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(req, timeout=_AVATAR_TIMEOUT) as resp:
                        content = resp.read(_AVATAR_LOCK_MAX)
                    if content:
                        break
                except Exception:
                    content = None
            if not content:
                return None
            tmp = fp.with_suffix(fp.suffix + ".tmp")
            tmp.write_bytes(content)
            tmp.replace(fp)                                        # ★ 原子替换
        return f"/avatars/{acc_id}/{fname}"
    except Exception as e:
        logger.info("头像缓存失败（忽略，回退原始 URL）: %s", str(e)[:120])
        return None


def cached_avatar_path(url: str, acc_id: str | None = None,
                       avatar_dir: Path | None = None) -> Path | None:
    """按头像 URL 找本地缓存文件（/api/avatar 的“本地文件优先”用它）。

    avatar_dir 传 <数据根>/avatars。不指定 acc_id 时依次在所有账号目录里找
    （前端 <img> 只带 url，不带账号）。
    """
    try:
        key = avatar_hash(url)
    except Exception:
        return None
    base = Path(avatar_dir) if avatar_dir else (DATA_DIR / "avatars")
    if not base.is_dir():
        return None
    if acc_id:
        dirs = [base / acc_id]
    else:
        try:
            dirs = [p for p in base.iterdir() if p.is_dir()]
        except Exception:
            return None
    for d in dirs:
        for ext in _AVATAR_EXTS:
            fp = d / f"{key}{ext}"
            if fp.is_file() and fp.stat().st_size > 0:
                return fp
    return None


def _wait_conversation_rows(page, timeout_s: int = 60) -> int:
    """轮询直到左侧会话列表出现会话行，返回当前可见行数；超时返回 0。

    抖音私信页实测要 24~37 秒才渲染出会话列表（且是虚拟列表）。抓联系人和发送好友
    都必须先等它就绪，否则会在列表还没出来时立刻判定「找不到该好友」并误报失败。
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            n = int(page.evaluate(_VISIBLE_ROWS_JS) or 0)
        except Exception:
            n = 0
        if n > 0:
            return n
        try:
            page.wait_for_timeout(1000)
        except Exception:
            time.sleep(1)
    return 0


def _wait_conversation_list_ready(page, rounds: int = 2, each_timeout: int = 60) -> int:
    """等会话列表就绪：每轮最多 each_timeout 秒，未就绪就 reload 再给一轮（总约 2 分钟）。

    就绪立即返回行数（不再空等）；两轮都 0 行才返回 0，由调用方报明确的登录态错误。
    """
    rows = 0
    for i in range(rounds):
        rows = _wait_conversation_rows(page, each_timeout)
        if rows:
            logger.info("会话列表已就绪：当前可见 %s 行", rows)
            return rows
        logger.warning("第 %s 次等待会话列表超时（已轮询 %s 秒）", i + 1, each_timeout)
        if i < rounds - 1:
            try:
                page.reload(wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(3000)
            except Exception as e:
                logger.info("重新加载私信页失败: %s", str(e)[:80])
    return 0


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


_UPLOAD_OBSERVE_JS = """
    () => {
        const out = {
            imgs: document.images.length,
            bigRightImgs: 0,
            inputs: [],
            hasEditor: false,
        };
        // 右侧主区（x>300）宽度>100 的大图数量：新增的图片消息气泡会体现为增量
        document.querySelectorAll('img').forEach(im => {
            const r = im.getBoundingClientRect();
            if (r.width > 100 && r.x > 300) out.bigRightImgs++;
        });
        // 全页文件输入框清单（判定哪个真正属于会话上传器）
        document.querySelectorAll('input[type="file"]').forEach(el => {
            const p = el.parentElement;
            out.inputs.push({
                cls: String(el.className || '').slice(0, 45),
                accept: String(el.accept || '').slice(0, 70),
                parent: (p ? String(p.className || '').slice(0, 70) : ''),
                inEditor: !!(el.closest('.messageEditorimChatEditorContainer') || el.closest('[class*="MsgInputFileUpload"]')),
                vis: !!(el.offsetWidth || el.offsetHeight),
            });
        });
        // 只回报「编辑器是否存在」，不 dump outerHTML/innerText（诊断白名单原则）
        out.hasEditor = !!document.querySelector('.messageEditorimChatEditorContainer');
        return out;
    }
"""


def test_send_image(acc_dir: Path, target_name: str) -> dict:
    """测试原生图片发送：打开目标会话 → 上传 data/emojis 里第一张图 → 观察并完成发送。"""
    result = {"error": None, "sent": False}
    state_path = acc_dir / "state.json"
    if not state_path.exists():
        result["error"] = "该账号尚未上传登录态 state.json"
        return result
    emojis_dir = acc_dir.parent.parent / "emojis"
    images = sorted(p for p in emojis_dir.glob("*.png") if p.is_file())
    images += sorted(p for p in emojis_dir.glob("*.jpg") if p.is_file())
    if not images:
        result["error"] = f"表情目录没有图片（{emojis_dir}），请先放一张 png/jpg"
        return result
    image_path = str(images[0])
    cfg = load_account_config(acc_dir)
    engine = cfg.get("browser", "chromium")
    headless = cfg.get("headless", True)

    browser = None
    try:
        p = sync_playwright().start()
        try:
            browser = _launch_browser(p, engine, headless)
            # 只注入 cookies（不注入 localStorage），个别账号降级整份 storage_state
            context = _new_context(browser, state_path, acc_dir)
            page = context.new_page()
            for attempt in range(3):
                try:
                    page.goto(CHAT_URL, timeout=60000, wait_until="domcontentloaded")
                    break
                except Exception as e:
                    logger.info("图片测试：第 %s 次打开页面失败: %s", attempt + 1, str(e)[:80])
                    time.sleep(5)
            page.wait_for_timeout(10000)
            logged, why = check_login(page)
            if not logged:
                result["error"] = why
                return result
            # 打开目标会话（与定时发送 send_to_contact 同款重试 + 详细诊断）
            switched = False
            try:
                page.wait_for_selector(".conversationConversationItemtitle", timeout=25000)
            except Exception as e:
                logger.info("【图片测试】等待会话列表超时: %s", str(e)[:80])
            time.sleep(2)  # 等 React 应用完全水合再点击（定时发送前有刷新热身的等效等待）
            try:
                metas = _safe_node_meta(page, _LIST_TITLE_SEL, 4)
                count = page.locator(_LIST_TITLE_SEL).count()
                logger.info("【图片测试】侧栏会话 %s 个｜前几行节点: %s", count, _fmt_meta(metas))
                logger.info("【图片测试】目标在侧栏=%s", _visible_text_center(page, target_name, x_max=330) is not None)
            except Exception as e:
                logger.info("【图片测试】读取侧栏列表失败: %s", str(e)[:80])
            for attempt in range(5):
                try:
                    target = _find_contact(page, target_name)
                    if target.count():
                        box = None
                        try:
                            box = target.bounding_box()
                        except Exception:
                            pass
                        logger.info(
                            "【图片测试】第%s次点击: 命中=%s box=%s", attempt + 1, target.count(), box
                        )
                        target.click(force=True, timeout=10000)
                        time.sleep(random.uniform(2.5, 3.5))
                        v = verify_in_conversation(page, target_name)
                        logger.info("【图片测试】第%s次 verify=%s", attempt + 1, v)
                        if v:
                            switched = True
                            break
                    else:
                        try:
                            page.mouse.move(200, 350)
                            page.mouse.wheel(0, 600)
                        except Exception:
                            pass
                        time.sleep(1.5)
                        logger.info("【图片测试】第%s次未命中，已滚动侧栏", attempt + 1)
                except Exception as e:
                    logger.info("【图片测试】点击 %s 异常: %s", target_name, str(e)[:100])
                time.sleep(random.uniform(1, 2))
            if not switched:
                logger.info("【图片测试】列表点击未成功，改用搜索打开")
                if search_and_open(page, target_name):
                    time.sleep(2)
                    switched = verify_in_conversation(page, target_name)
                    logger.info("【图片测试】搜索打开后 verify=%s", switched)
            if not switched:
                logger.info("【图片测试】打开失败时 url=%s", page.url)
                _screenshot(page, acc_dir)
                result["error"] = "未能打开目标会话"
                return result
            time.sleep(1.5)
            # ===== 取证1：当前文件输入框清单 =====
            f0 = page.evaluate(_UPLOAD_OBSERVE_JS) or {}
            logger.info("【图片测试】初始 输入框=%s", f0.get("inputs"))
            logger.info(
                "【图片测试】初始 大图=%s 页面img=%s 编辑器存在=%s",
                f0.get("bigRightImgs"), f0.get("imgs"), bool(f0.get("hasEditor")),
            )

            # ===== 网络监听：全部 XHR/fetch + 控制台错误 =====
            traffic: list[str] = []

            def _on_resp(resp):
                try:
                    if resp.status and resp.request.method in ("POST", "PUT"):
                        u = resp.url
                        if len(u) < 150:
                            traffic.append(f"{resp.status} {resp.request.method} {u}")
                except Exception:
                    pass

            page.on("response", _on_resp)

            js_errors: list[str] = []

            def _on_console(msg):
                try:
                    if msg.type in ("error", "warning"):
                        t = msg.text or ""
                        if len(t) < 200 and t not in js_errors:
                            js_errors.append(t)
                except Exception:
                    pass

            page.on("console", _on_console)

            def _pick_input():
                """按优先级挑一个真正属于会话编辑器的文件输入框。"""
                for sel in (
                    '.messageEditorimChatEditorContainer input[type="file"]',
                    '[class*="MsgInputFileUpload"] input[type="file"]',
                    'input.semi-upload-hidden-input',
                    'input[type="file"]',
                ):
                    loc = page.locator(sel)
                    if loc.count():
                        return loc.first
                return None

            file_input = _pick_input()
            if file_input is None:
                result["error"] = "找不到文件上传框 input[type=file]"
                return result
            logger.info("【图片测试】选用输入框 → set_input_files: %s", Path(image_path).name)
            file_input.set_input_files(image_path)
            time.sleep(2)  # 等附件 chip 渲染进编辑器

            def _leaf_texts():
                return (
                    page.evaluate(
                        "() => { const s = new Set(); document.querySelectorAll('div,span,li,button').forEach(el => {"
                        " if (el.children.length === 0) { const t = (el.textContent || '').trim(); "
                        " if (t && t.length < 20) s.add(t); } }); return [...s].slice(0, 80); }"
                    )
                    or []
                )

            # ===== 确认附件已挂载 + 发送按钮已出现 =====
            leaf0 = _leaf_texts()
            fname_mark = Path(image_path).name
            has_attach = any(fname_mark in t or ".png" in t or ".jpg" in t for t in leaf0)
            has_sendbtn = any(
                t.replace("\u00a0", " ").startswith("发送给") or t in ("发送", "Send") for t in leaf0
            )
            logger.info("【图片测试】附件chip=%s 发送按钮=%s 相关文本=%s", has_attach, has_sendbtn,
                        [t for t in leaf0 if "发送" in t or fname_mark in t][:6])
            if has_attach:
                page.screenshot(path=str(acc_dir / "test_attached.png"), timeout=5000)

            # ===== 点发送弹窗里的「发送」按钮 =====
            clicked = False
            try:
                sure_btn = page.locator('button.MsgInputSendFileModalbtnSure').first
                if sure_btn.count() == 0:
                    sure_btn = page.locator('button:has-text("发送")').last
                if sure_btn.count():
                    # 弹窗可能晚 1~2 秒才出现，最多等 6 秒
                    try:
                        sure_btn.wait_for(state="visible", timeout=6000)
                    except Exception:
                        logger.info("【图片测试】发送弹窗按钮未及时出现")
                    if sure_btn.is_visible():
                        sure_btn.click(force=True, timeout=5000)
                        clicked = True
                        logger.info("【图片测试】已点击发送弹窗「发送」按钮")
            except Exception as e:
                logger.info("【图片测试】弹窗发送按钮点击异常: %s", str(e)[:100])
            if not clicked:
                logger.info("【图片测试】未找到弹窗发送按钮，尝试 Enter")
                try:
                    ce = page.locator('[contenteditable="true"]').first
                    if ce.count():
                        ce.click()
                        page.keyboard.press("Enter")
                        clicked = True
                except Exception:
                    pass

            # ===== 验证真正送达（点发送后保持页面 15 秒，等气泡/状态变化）=====
            sent_ok = False
            last_big = f0.get("bigRightImgs") or 0
            if clicked:
                for i in range(15):
                    time.sleep(1)
                    txts = _leaf_texts()
                    st = page.evaluate(_UPLOAD_OBSERVE_JS) or {}
                    cur_big = st.get("bigRightImgs") or 0
                    state_txt = [t for t in txts if any(k in t for k in ("发送中", "已发送", "发送失败", "失败"))]
                    if state_txt and i in (0, 3, 8, 14):
                        logger.info("【图片测试】%ss 状态文本: %s", i + 1, state_txt[:6])
                    if traffic and i in (0, 3, 8, 14):
                        logger.info("【图片测试】%ss 网络: %s", i + 1, traffic[-3:])
                    if cur_big > last_big:
                        logger.info("【图片测试】%ss 右侧大图 %s→%s（图片气泡出现=已发出）", i + 1, last_big, cur_big)
                        sent_ok = True
                        last_big = cur_big
                    if any("发送失败" in t for t in txts):
                        logger.info("【图片测试】%ss 出现「发送失败」", i + 1)
                        break
                    if i in (2, 7, 13):
                        page.screenshot(path=str(acc_dir / f"test_send_{i + 1}s.png"), timeout=5000)
            # 最后再补一拍
            stZ = page.evaluate(_UPLOAD_OBSERVE_JS) or {}
            page.screenshot(path=str(acc_dir / "test_send_last.png"), timeout=5000)
            if not sent_ok and (stZ.get("bigRightImgs") or 0) > last_big:
                sent_ok = True
                logger.info("【图片测试】收尾拍：右侧大图增加（判定发出）")
            leaf_end = _leaf_texts()
            logger.info("【图片测试】结束时相关文本: %s",
                        [t for t in leaf_end if "发送" in t or fname_mark in t][:10])
            im_hits = [t for t in traffic if any(k in t for k in ("/im/", "send", "upload", "message", "msg"))]
            logger.info("【图片测试】IM/发送相关请求(尾12): %s", im_hits[-12:])
            logger.info("【图片测试】全部POST请求(尾20): %s", [t for t in traffic if "POST" in t][-20:])
            if js_errors:
                logger.info("【图片测试】控制台错误(尾5): %s", js_errors[-5:])
            logger.info("【图片测试】完成，送达判定=%s（请以对方是否收到为准）", sent_ok)
            result["sent"] = bool(sent_ok)
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            p.stop()
    except Exception as e:
        logger.error("图片测试异常: %s", e)
        result["error"] = f"图片测试异常: {e}"
    return result



# ============ 表情面板侦查（抖音原生表情/贴纸） ============

_EMOJI_TOOLBAR_JS = """
    () => {
        const out = [];
        const ce = document.querySelector('[contenteditable="true"]');
        if (!ce) return out;
        let node = ce;
        for (let k = 0; k < 7 && node && node.parentElement; k++) {
            node = node.parentElement;
            node.querySelectorAll('button,[role="button"],[class*="icon" i]').forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width < 2 || r.height < 2 || r.width > 90 || r.height > 90) return;
                const cls = String(el.className || '');
                const aria = el.getAttribute('aria-label') || '';
                const title = el.getAttribute('title') || '';
                const key = aria + '|' + title + '|' + cls.slice(0, 45);
                if (out.some(x => x.key === key)) return;
                if (!aria && !title && !cls) return;
                out.push({ key, tag: el.tagName, aria: aria.slice(0, 30), title: title.slice(0, 30), cls: cls.slice(0, 70), x: Math.round(r.x), y: Math.round(r.y) });
            });
        }
        return out.slice(0, 40);
    }
"""

_EMOJI_PANEL_JS = """
    () => {
        const out = { texts: [], imgsCount: 0, imgs: [], panels: [] };
        const leafs = [...document.querySelectorAll('div,span,li')].filter(e => {
            if (e.children.length !== 0) return false;
            const t = (e.textContent || '').trim();
            return t && t.length <= 8;
        });
        const set = new Set();
        leafs.forEach(e => set.add((e.textContent || '').trim()));
        out.texts = [...set].slice(0, 150);
        const ims = [...document.querySelectorAll('img')].filter(im => {
            const r = im.getBoundingClientRect();
            return r.width >= 16 && r.width <= 200 && r.height >= 16 && r.height <= 200;
        });
        out.imgsCount = ims.length;
        out.imgs = ims.slice(0, 15).map(im => ({
            cls: String(im.className || '').slice(0, 50),
            src: (im.src || '').slice(0, 95),
            w: Math.round(im.getBoundingClientRect().width),
            h: Math.round(im.getBoundingClientRect().height),
        }));
        [...document.querySelectorAll('[class*="emoji" i],[class*="expression" i],[class*="sticker" i],[class*="biaoqing" i],[class*="panel" i],[class*="popover" i],[class*="face" i]')].forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width > 150 && r.height > 60 && r.width < 760 && r.y > 0) {
                out.panels.push(String(el.className || '').slice(0, 90) + ' | ' + Math.round(r.width) + 'x' + Math.round(r.height) + ' | y=' + Math.round(r.y));
            }
        });
        return out;
    }
"""

def diagnose_emoji_panel(acc_dir: Path) -> dict:
    """侦查抖音网页私信输入框的表情/贴纸入口：找出打开面板的按钮、面板里是什么。"""
    result = {"error": None}
    state_path = acc_dir / "state.json"
    if not state_path.exists():
        result["error"] = "该账号尚未上传登录态 state.json"
        return result
    cfg = load_account_config(acc_dir)
    engine = cfg.get("browser", "chromium")
    headless = cfg.get("headless", True)

    browser = None
    try:
        p = sync_playwright().start()
        try:
            browser = _launch_browser(p, engine, headless)
            # 只注入 cookies（不注入 localStorage），个别账号降级整份 storage_state
            context = _new_context(browser, state_path, acc_dir)
            page = context.new_page()
            for attempt in range(3):
                try:
                    page.goto(CHAT_URL, timeout=60000, wait_until="domcontentloaded")
                    break
                except Exception as e:
                    logger.info("表情诊断：第 %s 次打开页面失败: %s", attempt + 1, str(e)[:80])
                    time.sleep(5)
            page.wait_for_timeout(10000)
            logged, why = check_login(page)
            if not logged:
                result["error"] = why
                return result
            # 打开第一个会话（表情面板只在会话输入框里出现）
            opened_name = ""
            try:
                page.wait_for_selector(".conversationConversationItemtitle", timeout=20000)
                first_names = [c.get("name", "") for c in (page.evaluate(_STREAK_EXTRACT_JS) or []) if c.get("name")]
                if first_names:
                    opened_name = first_names[0]
                    for attempt in range(4):
                        target = _find_contact(page, opened_name)
                        if target.count():
                            target.click(force=True, timeout=10000)
                            time.sleep(random.uniform(2, 3))
                            if verify_in_conversation(page, opened_name):
                                break
                        else:
                            try:
                                page.mouse.move(200, 350)
                                page.mouse.wheel(0, 600)
                            except Exception:
                                pass
                            time.sleep(1.5)
                    if not verify_in_conversation(page, opened_name):
                        search_and_open(page, opened_name)
            except Exception as e:
                logger.info("【表情诊断】点开会话异常: %s", str(e)[:80])
            logger.info("【表情诊断】已打开会话: %s", opened_name or "(无)")
            page.wait_for_timeout(4000)

            # 1) 工具栏按钮清单
            btns = page.evaluate(_EMOJI_TOOLBAR_JS) or []
            logger.info("【表情诊断】工具栏按钮 %s 个", len(btns))
            for b in btns:
                logger.info(
                    "【表情诊断】工具钮 tag=%s aria=%r title=%r cls=%s x=%s y=%s",
                    b.get("tag"), b.get("aria"), b.get("title"), b.get("cls"), b.get("x"), b.get("y"),
                )
            # 2) 逐个点候选找表情面板（特征命中优先；先保证输入框是空的，避免误触发送）
            try:
                ce = page.locator('[contenteditable="true"]').first
                if ce.count():
                    ce.click()
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Delete")
            except Exception:
                pass
            time.sleep(1)
            hinted = [
                b for b in btns
                if any(k in (str(b.get("aria")) + str(b.get("title")) + str(b.get("cls"))).lower()
                   for k in ("emoji", "expression", "sticker", "face", "smile", "表情", "biaoqing"))
            ]
            candidates = hinted or btns
            panel_found = None
            for i, b in enumerate(candidates[:10]):
                try:
                    r = page.evaluate(
                        """(key) => {
                            const cls = key.cls, aria = key.aria, title = key.title;
                            const cands = [...document.querySelectorAll('button,[role="button"],[class*="icon" i]')].filter(e => {
                                const c = String(e.className || ''); const a = e.getAttribute('aria-label') || ''; const t = e.getAttribute('title') || '';
                                return (cls && c.slice(0, 45) === cls.slice(0, 45)) || (aria && a === aria) || (title && t === title);
                            });
                            if (!cands.length) return 'none';
                            cands[cands.length - 1].click();
                            return 'clicked';
                        }""",
                        b,
                    )
                    label = (b.get("aria") or b.get("title") or b.get("cls"))[:26]
                    logger.info("【表情诊断】点候选#%s(%s) → %s", i + 1, label, r)
                    if r != "clicked":
                        continue
                    time.sleep(1.6)
                    snap = page.evaluate(_EMOJI_PANEL_JS) or {}
                    leaf_n = len(snap.get("texts") or [])
                    imgs_n = snap.get("imgsCount") or 0
                    panels = snap.get("panels") or []
                    texts = snap.get("texts") or []
                    emojiish = sum(
                        1 for t in texts
                        if t and any(0x1F000 <= ord(ch) <= 0x1FAFF or 0x2600 <= ord(ch) <= 0x27BF or ord(ch) > 0x3000 for ch in t)
                    )
                    logger.info(
                        "【表情诊断】候选#%s 后: 叶子文本=%s 网格图=%s emoji字符项=%s 面板=%s",
                        i + 1, leaf_n, imgs_n, emojiish, panels[:3],
                    )
                    if imgs_n >= 6 or panels or (emojiish >= 5 and leaf_n >= 10):
                        panel_found = b
                        logger.info("【表情诊断】✅ 命中面板（候选#%s: %s）", i + 1, label)
                        logger.info("【表情诊断】面板文本样本(前40): %s", texts[:40])
                        logger.info("【表情诊断】面板图片样本(前15): %s", snap.get("imgs"))
                        # ---- 深入：枚举左侧面板项目，点「表情/贴纸」入口 ----
                        try:
                            rows = page.evaluate(
                                """() => {
                                    const out = [];
                                    const roots = [...document.querySelectorAll('[class*="componentsLeftPanel" i]')];
                                    const root = roots[roots.length - 1] || document.body;
                                    root.querySelectorAll('div,span,li').forEach(el => {
                                        const r = el.getBoundingClientRect();
                                        if (r.width < 4 || r.height < 4) return;
                                        const t = (el.textContent || '').trim().replace(/\\u00a0/g, ' ');
                                        if (!t || t.length > 12) return;
                                        const icon = el.querySelector('img,svg');
                                        const ic = icon ? Math.round((icon.getBoundingClientRect().width || 0)) : 0;
                                        const cls = String(el.className || '').slice(0, 60);
                                        const leaf = el.children.length === 0;
                                        out.push({ t, leaf, ic, cls, x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width) });
                                    });
                                    return out.slice(0, 60);
                                }"""
                            ) or []
                            logger.info("【表情诊断】面板行项目: %s", rows)
                            # 找「表情」类入口文本
                            pick_t = None
                            for row in rows:
                                if row.get("leaf") and any(k in row.get("t", "") for k in ("表情", "贴纸", "斗图")):
                                    pick_t = row.get("t")
                                    break
                            if pick_t:
                                logger.info("【表情诊断】点面板入口: %s", pick_t)
                                page.get_by_text(pick_t, exact=True).first.click(force=True, timeout=5000)
                                time.sleep(1.8)
                                snap2 = page.evaluate(_EMOJI_PANEL_JS) or {}
                                logger.info(
                                    "【表情诊断】点后: 叶子文本=%s 网格图=%s emoji项=%s 面板=%s",
                                    len(snap2.get("texts") or []), snap2.get("imgsCount"),
                                    sum(1 for t in (snap2.get("texts") or [])
                                        if t and any(0x1F000 <= ord(ch) <= 0x1FAFF or 0x2600 <= ord(ch) <= 0x27BF for ch in t)),
                                    snap2.get("panels"),
                                )
                                logger.info("【表情诊断】点后文本样本(前60): %s", (snap2.get("texts") or [])[:60])
                                logger.info("【表情诊断】点后图片样本(前20): %s", (snap2.get("imgs") or [])[:20])
                                # 若出现表情网格：点第 1 个表情，观察输入框变化（不发送）
                                leaf2 = (snap2.get("texts") or [])
                                emo_cells = [t for t in leaf2 if t and (any(0x1F000 <= ord(c) <= 0x1FAFF for c in t) or len(t) <= 4 and any(0x2600 <= ord(c) <= 0x27BF for c in t))]
                                if emo_cells:
                                    logger.info("【表情诊断】表情候选(前20): %s", emo_cells[:20])
                                    try:
                                        page.get_by_text(emo_cells[0], exact=True).first.click(force=True, timeout=4000)
                                        time.sleep(1.2)
                                        ce_txt = ""
                                        try:
                                            ce_txt = page.locator('[contenteditable="true"]').first.inner_text() or ""
                                        except Exception:
                                            pass
                                        logger.info("【表情诊断】点表情后输入框：字符数=%s（是否变空=%s）",
                                                    len(ce_txt), not bool(ce_txt.strip()))
                                    except Exception as e:
                                        logger.info("【表情诊断】点表情单元异常: %s", str(e)[:90])
                            else:
                                logger.info("【表情诊断】面板行里没有文字型「表情/贴纸」入口，行文本=%s",
                                            [r.get("t") for r in rows if r.get("leaf")][:30])
                        except Exception as e:
                            logger.info("【表情诊断】深入面板异常: %s", str(e)[:120])
                        break
                    page.keyboard.press("Escape")
                    time.sleep(0.8)
                except Exception as e:
                    logger.info("【表情诊断】候选#%s 异常: %s", i + 1, str(e)[:90])
            if not panel_found:
                logger.info("【表情诊断】未找到表情面板（试过的候选 %s 个）", len(candidates))
            # 3) 专门阶段：空输入框点“+”，扫底部弹层（菜单/面板）
            try:
                add = page.locator('.semi-upload-add').first
                if add.count():
                    add.click(force=True, timeout=5000)
                    time.sleep(1.8)
                    pop = page.evaluate(
                        """() => {
                            const out = { leafs: [], imgs: [], cls: [] };
                            document.querySelectorAll('div,span,li,button').forEach(el => {
                                if (el.children.length !== 0) return;
                                const t = (el.textContent || '').trim();
                                const r = el.getBoundingClientRect();
                                if (!t || t.length > 10 || r.width < 2 || r.height < 2 || r.y < 520) return;
                                out.leafs.push(t);
                            });
                            out.leafs = [...new Set(out.leafs)].slice(0, 60);
                            document.querySelectorAll('img').forEach(im => {
                                const r = im.getBoundingClientRect();
                                if (r.width >= 24 && r.width <= 80 && r.y >= 520) {
                                    out.imgs.push({ src: (im.src || '').slice(0, 90), w: Math.round(r.width), h: Math.round(r.height), cls: String(im.className || '').slice(0, 40) });
                                }
                            });
                            out.imgs = out.imgs.slice(0, 25);
                            [...document.querySelectorAll('[class*="menu" i],[class*="panel" i],[class*="pop" i],[class*="upload" i],[class*="tooltip" i]')].forEach(el => {
                                const r = el.getBoundingClientRect();
                                if (r.width > 60 && r.width < 500 && r.height > 30 && r.height < 600 && r.y > 520) {
                                    out.cls.push(String(el.className || '').slice(0, 70) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height) + ' y=' + Math.round(r.y));
                                }
                            });
                            out.cls = [...new Set(out.cls)].slice(0, 15);
                            return out;
                        }"""
                    ) or {}
                    logger.info("【表情诊断】+后底部叶子文本: %s", pop.get("leafs"))
                    logger.info("【表情诊断】+后底部图标图: %s", pop.get("imgs"))
                    logger.info("【表情诊断】+后底部容器: %s", pop.get("cls"))
                    kw_rows = [t for t in (pop.get("leafs") or []) if any(k in t for k in ("表情", "贴纸", "斗图", "相册", "图片", "文件", "收藏", "更多", "拍摄", "视频"))]
                    logger.info("【表情诊断】可点关键词: %s", kw_rows)
                    # 点“表情/贴纸”若存在
                    pick_t = next((t for t in kw_rows if any(k in t for k in ("表情", "贴纸", "斗图"))), None)
                    if pick_t:
                        page.get_by_text(pick_t, exact=True).first.click(force=True, timeout=5000)
                        time.sleep(2)
                        snap3 = page.evaluate(_EMOJI_PANEL_JS) or {}
                        logger.info(
                            "【表情诊断】点「%s」后: 文本=%s 图=%s 面板=%s",
                            pick_t, len(snap3.get("texts") or []), snap3.get("imgsCount"), snap3.get("panels"),
                        )
                        logger.info("【表情诊断】点「%s」后文本(前50): %s", pick_t, (snap3.get("texts") or [])[:50])
                        logger.info("【表情诊断】点「%s」后图片(前20): %s", pick_t, (snap3.get("imgs") or [])[:20])
                        # 若有网格：点第一个单元看输入框是否出现内容/发送钮
                        texts3 = snap3.get("texts") or []
                        cell = None
                        if (snap3.get("imgsCount") or 0) > 0:
                            cell = "first-img"
                        elif texts3 and any(len(t) <= 2 for t in texts3):
                            cell = texts3[0]
                        if cell:
                            try:
                                if cell == "first-img":
                                    page.evaluate("""() => {
                                        const ims = [...document.querySelectorAll('img')].filter(im => { const r = im.getBoundingClientRect(); return r.width >= 24 && r.width <= 90 && r.height >= 24 && r.height <= 90 && r.y > 400; });
                                        if (ims.length) ims[0].click();
                                    }""")
                                else:
                                    page.get_by_text(cell, exact=True).first.click(force=True, timeout=4000)
                                time.sleep(1.5)
                                ce_txt = ""
                                try:
                                    ce_txt = page.locator('[contenteditable="true"]').first.inner_text() or ""
                                except Exception:
                                    pass
                                leaf4 = page.evaluate(_EMOJI_PANEL_JS) or {}
                                logger.info("【表情诊断】点单元后 输入框字符数=%s 出现发送?=%s", len(ce_txt),
                                            any(t.replace("\\u00a0", " ").startswith("发送给") or t == "发送"
                                                for t in (leaf4.get("texts") or [])))
                            except Exception as e:
                                logger.info("【表情诊断】点单元异常: %s", str(e)[:100])
                else:
                    logger.info("【表情诊断】没找到 + 按钮")
            except Exception as e:
                logger.info("【表情诊断】+按钮阶段异常: %s", str(e)[:120])
            result["panel_found"] = bool(panel_found)
            result["opened_name"] = opened_name
            if panel_found:
                result["entry"] = {
                    "aria": panel_found.get("aria"),
                    "title": panel_found.get("title"),
                    "cls": panel_found.get("cls"),
                }
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            p.stop()
    except Exception as e:
        logger.error("表情诊断异常: %s", e)
        result["error"] = f"表情诊断异常: {e}"
    return result


def emoji_ui_probe(acc_dir: Path, target_name: str = "", click_x: int | None = None, click_y: int | None = None) -> dict:
    """有头（真实窗口）打开会话，探测/点击表情 UI；可传屏幕坐标点击。给网页端排障用。"""
    result = {"error": None, "clicked": False}
    state_path = acc_dir / "state.json"
    if not state_path.exists():
        result["error"] = "该账号尚未上传登录态 state.json"
        return result
    cfg = load_account_config(acc_dir)
    engine = cfg.get("browser", "chromium")
    # 强制有头：让用户在窗口里看/验证坐标
    headless = False

    browser = None
    try:
        p = sync_playwright().start()
        try:
            browser = _launch_browser(p, engine, headless)
            # 只注入 cookies（不注入 localStorage），个别账号降级整份 storage_state
            context = _new_context(browser, state_path, acc_dir)
            page = context.new_page()
            for attempt in range(3):
                try:
                    page.goto(CHAT_URL, timeout=60000, wait_until="domcontentloaded")
                    break
                except Exception as e:
                    logger.info("表情UI：第 %s 次打开页面失败: %s", attempt + 1, str(e)[:80])
                    time.sleep(5)
            page.wait_for_timeout(10000)
            logged, why = check_login(page)
            if not logged:
                result["error"] = why
                return result
            # 打开目标会话（没有目标就用第一个）
            names = [c.get("name", "") for c in (page.evaluate(_STREAK_EXTRACT_JS) or []) if c.get("name")]
            opened = target_name or (names[0] if names else "")
            if opened:
                try:
                    page.wait_for_selector(".conversationConversationItemtitle", timeout=20000)
                    for attempt in range(4):
                        target = _find_contact(page, opened)
                        if target.count():
                            target.click(force=True, timeout=10000)
                            time.sleep(random.uniform(2, 3))
                            if verify_in_conversation(page, opened):
                                break
                        else:
                            try:
                                page.mouse.move(200, 350)
                                page.mouse.wheel(0, 600)
                            except Exception:
                                pass
                            time.sleep(1.5)
                    if not verify_in_conversation(page, opened):
                        search_and_open(page, opened)
                except Exception as e:
                    logger.info("表情UI：点开会话异常: %s", str(e)[:80])
            logger.info("【表情UI】已打开会话: %s（窗口已弹出，请观察输入框工具区）", opened or "(无)")
            page.wait_for_timeout(4000)

            # 底部工具区全景清单（右栏 y>560 的可见元素：图标/按钮/文字）
            inv = page.evaluate(
                """() => {
                    const out = [];
                    document.querySelectorAll('div,span,button,svg,img,[role="button"]').forEach(el => {
                        const r = el.getBoundingClientRect();
                        if (r.width < 3 || r.height < 3 || r.x < 750 || r.y < 560) return;
                        if (r.y + r.height > 780 || r.width > 400) return;
                        const cls = String(el.className || '').slice(0, 60);
                        const txt = (el.textContent || '').trim().slice(0, 8);
                        const aria = el.getAttribute('aria-label') || '';
                        const title = el.getAttribute('title') || '';
                        if (!cls && !txt && !aria && !title) return;
                        out.push({
                            tag: el.tagName, cls, txt, aria: aria.slice(0, 20), title: title.slice(0, 20),
                            x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
                            src: el.tagName === 'IMG' ? (el.src || '').slice(0, 70) : '',
                        });
                    });
                    // 去重
                    const seen = new Set(); const uniq = [];
                    out.forEach(o => { const k = o.tag + '|' + o.cls + '|' + o.txt + '|' + Math.round(o.x / 5) + '|' + Math.round(o.y / 5); if (!seen.has(k)) { seen.add(k); uniq.push(o); } });
                    return uniq;
                }"""
            ) or []
            logger.info("【表情UI】底部工具区元素(%s):", len(inv))
            for e in inv[:40]:
                logger.info(
                    "【表情UI】  <%s> cls=%s txt=%r aria=%s title=%s box=(%s,%s %sx%s) src=%s",
                    e.get("tag"), e.get("cls"), e.get("txt"), e.get("aria"), e.get("title"),
                    e.get("x"), e.get("y"), e.get("w"), e.get("h"), e.get("src"),
                )
            try:
                page.screenshot(path=str(acc_dir / "emoji_probe_bottom.png"), timeout=6000)
                logger.info("【表情UI】已存底部截图 emoji_probe_bottom.png")
            except Exception:
                pass
            # 若给坐标则点一下
            if click_x is not None and click_y is not None:
                page.mouse.click(click_x, click_y)
                result["clicked"] = True
                logger.info("【表情UI】已点击 (%s, %s)", click_x, click_y)
                time.sleep(2.5)
                snap = page.evaluate(_EMOJI_PANEL_JS) or {}
                leafs = snap.get("texts") or []
                logger.info(
                    "【表情UI】点击后 底部文本=%s 图=%s 面板=%s emoji项=%s",
                    [t for t in leafs if len(t) <= 6][:40], snap.get("imgsCount"),
                    snap.get("panels"), sum(1 for t in leafs if t and len(t) <= 4),
                )
                try:
                    page.screenshot(path=str(acc_dir / "emoji_probe_clicked.png"), timeout=6000)
                    logger.info("【表情UI】已存点击后截图 emoji_probe_clicked.png")
                except Exception:
                    pass
            logger.info("【表情UI】窗口将保持打开约 25 秒供你观察，随后自动关闭")
            page.wait_for_timeout(25000)
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            p.stop()
    except Exception as e:
        logger.error("表情UI探测异常: %s", e)
        result["error"] = f"表情UI探测异常: {e}"
    return result


def emoji_flow_probe(acc_dir: Path, target_name: str = "", max_step: int = 2, send: bool = False, emoji_text: str = "火花") -> dict:
    """原生表情三步流（有头窗口）：①点头像 ②点输入框右边笑脸 ③点表情(火花)。
    max_step: 1=只到点头像, 2=再点笑脸并展开面板, 3=再点表情并(可选)发送。"""
    result = {"error": None, "steps_done": 0}
    state_path = acc_dir / "state.json"
    if not state_path.exists():
        result["error"] = "该账号尚未上传登录态 state.json"
        return result
    cfg = load_account_config(acc_dir)
    engine = cfg.get("browser", "chromium")
    headless = False  # 有头，用户可见

    browser = None
    try:
        p = sync_playwright().start()
        try:
            browser = _launch_browser(p, engine, headless)
            # 只注入 cookies（不注入 localStorage），个别账号降级整份 storage_state
            context = _new_context(browser, state_path, acc_dir)
            page = context.new_page()
            for attempt in range(3):
                try:
                    page.goto(CHAT_URL, timeout=60000, wait_until="domcontentloaded")
                    break
                except Exception as e:
                    logger.info("表情流：第 %s 次打开页面失败: %s", attempt + 1, str(e)[:80])
                    time.sleep(5)
            page.wait_for_timeout(10000)
            logged, why = check_login(page)
            if not logged:
                result["error"] = why
                return result
            # 打开目标会话
            names = [c.get("name", "") for c in (page.evaluate(_STREAK_EXTRACT_JS) or []) if c.get("name")]
            opened = target_name or (names[0] if names else "")
            if opened:
                try:
                    page.wait_for_selector(".conversationConversationItemtitle", timeout=20000)
                    for attempt in range(4):
                        target = _find_contact(page, opened)
                        if target.count():
                            target.click(force=True, timeout=10000)
                            time.sleep(random.uniform(2, 3))
                            if verify_in_conversation(page, opened):
                                break
                        else:
                            try:
                                page.mouse.move(200, 350)
                                page.mouse.wheel(0, 600)
                            except Exception:
                                pass
                            time.sleep(1.5)
                    if not verify_in_conversation(page, opened):
                        search_and_open(page, opened)
                except Exception as e:
                    logger.info("表情流：点开会话异常: %s", str(e)[:80])
            logger.info("【表情流】会话已打开: %s", opened or "(无)")
            page.wait_for_timeout(3000)

            def _shot(name):
                try:
                    page.screenshot(path=str(acc_dir / name), timeout=6000)
                    logger.info("【表情流】截图 %s", name)
                except Exception:
                    pass

            def _avatar_center():
                """找右侧会话头部头像中心。"""
                return page.evaluate(
                    """() => {
                        const ims = [...document.querySelectorAll('img')].map(im => {
                            const r = im.getBoundingClientRect();
                            return { im, r };
                        }).filter(o => o.r.width >= 30 && o.r.width <= 68 && o.r.height >= 30 && o.r.height <= 68
                                      && o.r.x > 300 && o.r.x < 760 && o.r.y > 0 && o.r.y < 130);
                        if (!ims.length) return null;
                        // 优先取最靠近顶部左侧、可能带头像类名的
                        ims.sort((a, b) => (a.r.y - b.r.y) || (a.r.x - b.r.x));
                        const top = ims[0];
                        return { x: Math.round(top.r.x + top.r.width / 2), y: Math.round(top.r.y + top.r.height / 2), w: Math.round(top.r.width), cls: String(top.im.className || '').slice(0, 50) };
                    }"""
                )

            def _bottom_icons():
                """底部输入行右侧图标（排除文件上传 semi-upload）。"""
                return page.evaluate(
                    """() => {
                        const out = [];
                        document.querySelectorAll('svg,img,button,[role="button"],[class*="icon" i],[class*="action" i]').forEach(el => {
                            const r = el.getBoundingClientRect();
                            if (r.width < 16 || r.width > 64 || r.height < 16 || r.height > 64) return;
                            if (r.x < 900 || r.y < 640 || r.y > 780) return;
                            if (el.closest('.semi-upload') || el.closest('[class*="FileUpload"]')) return;
                            const cls = String(el.className || '');
                            if (cls === '[object SVGAnimatedString]') return;
                            out.push({ tag: el.tagName, cls: cls.slice(0, 55), x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) });
                        });
                        const seen = new Set(); const u = [];
                        out.forEach(o => { const k = o.tag + '|' + o.cls + '|' + Math.round(o.x / 8) + '|' + Math.round(o.y / 8); if (!seen.has(k)) { seen.add(k); u.push(o); } });
                        return u.sort((a, b) => a.x - b.x || a.y - b.y);
                    }"""
                )

            steps = 0
            # ---- 第 1 步：点头像 ----
            if max_step >= 1:
                av = _avatar_center()
                logger.info("【表情流】头部头像候选: %s", av)
                if not av:
                    result["error"] = "未找到会话头部头像"
                    _shot("flow_1_none.png")
                else:
                    page.mouse.click(av["x"], av["y"])
                    steps = 1
                    logger.info("【表情流】已点头像 (%s,%s) cls=%s", av["x"], av["y"], av.get("cls"))
                    time.sleep(2.5)
                    _shot("flow_1_avatar.png")
            result["steps_done"] = steps
            # ---- 第 2 步：点笑脸 ----
            if max_step >= 2 and steps >= 1:
                icons = _bottom_icons()
                logger.info("【表情流】底部图标: %s", icons)
                smiley = icons[0] if icons else None
                if not smiley:
                    logger.info("【表情流】没找到笑脸图标，尝试再列一次底部元素")
                else:
                    page.mouse.click(smiley["x"], smiley["y"])
                    steps = 2
                    logger.info("【表情流】已点笑脸/图标 (%s,%s) %s", smiley["x"], smiley["y"], smiley.get("cls"))
                    time.sleep(2.8)
                    _shot("flow_2_smiley.png")
                    snap = page.evaluate(_EMOJI_PANEL_JS) or {}
                    texts = snap.get("texts") or []
                    logger.info("【表情流】点击后 面板=%s 文本数=%s 图=%s", snap.get("panels")[:5], len(texts), snap.get("imgsCount"))
                    logger.info("【表情流】点击后 含关键字的文本: %s",
                                [t for t in texts if any(k in t for k in ("火花", "表情", "斗图", "爱心", "火焰", "🔥"))][:20])
                    logger.info("【表情流】点击后 文本样本(前45): %s", texts[:45])
                    logger.info("【表情流】点击后 图片样本(前20): %s", (snap.get("imgs") or [])[:20])
            result["steps_done"] = steps
            # ---- 第 3 步：点火花/表情并发送 ----
            if max_step >= 3 and steps >= 2:
                try:
                    cell = None
                    # 优先按文本找 火花
                    try:
                        hit = page.get_by_text(emoji_text, exact=False)
                        for i in range(hit.count()):
                            b = hit.nth(i).bounding_box()
                            if b and b["y"] > 400:
                                cell = (hit.nth(i), "text")
                                break
                    except Exception:
                        pass
                    if cell is None:
                        # 找面板区域第一个 60-160px 图片
                        got = page.evaluate(
                            """() => {
                                const ims = [...document.querySelectorAll('img')].filter(im => {
                                    const r = im.getBoundingClientRect();
                                    return r.width >= 50 && r.width <= 200 && r.height >= 50 && r.height <= 200 && r.y > 300;
                                });
                                if (!ims.length) return null;
                                const t = ims[0];
                                const r = t.getBoundingClientRect();
                                return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
                            }"""
                        )
                        if got:
                            cell = ("mouse", got)
                    if not cell:
                        logger.info("【表情流】未找到可点的表情格子（面板可能没开）")
                        _shot("flow_3_none.png")
                    else:
                        if cell[0] == "mouse":
                            page.mouse.click(cell[1]["x"], cell[1]["y"])
                            logger.info("【表情流】已点图片表情格 (%s,%s)", cell[1]["x"], cell[1]["y"])
                        else:
                            cell[0].click(force=True, timeout=5000)
                            logger.info("【表情流】已点文本表情「%s」", emoji_text)
                        time.sleep(2)
                        ce_txt = ""
                        try:
                            ce_txt = page.locator('[contenteditable="true"]').first.inner_text() or ""
                        except Exception:
                            pass
                        _shot("flow_3_picked.png")
                        logger.info("【表情流】点表情后 输入框字符数=%s", len(ce_txt))
                        if send:
                            # 尝试发送：点「发送给」按钮或 Enter
                            sent = False
                            try:
                                sure = page.locator('button.MsgInputSendFileModalbtnSure').first
                                if sure.count() and sure.is_visible():
                                    sure.click(force=True, timeout=5000)
                                    sent = True
                                    logger.info("【表情流】已点文件确认发送")
                            except Exception:
                                pass
                            if not sent:
                                try:
                                    b = page.evaluate(
                                        """() => {
                                            const els = [...document.querySelectorAll('button,[role="button"],div')].filter(e => {
                                                const t = (e.textContent || '').replace(/\\u00a0/g, ' ').trim();
                                                return (t.startsWith('发送给') || t === '发送') && t.length <= 12;
                                            });
                                            if (!els.length) return null;
                                            const el = els[els.length - 1];
                                            const r = el.getBoundingClientRect();
                                            return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
                                        }"""
                                    )
                                    if b:
                                        page.mouse.click(b["x"], b["y"])
                                        sent = True
                                        logger.info("【表情流】已点发送按钮 (%s,%s)", b["x"], b["y"])
                                except Exception as e:
                                    logger.info("【表情流】点发送异常: %s", str(e)[:90])
                            if not sent:
                                try:
                                    ce = page.locator('[contenteditable="true"]').first
                                    if ce.count():
                                        ce.click()
                                        page.keyboard.press("Enter")
                                        sent = True
                                        logger.info("【表情流】已按 Enter")
                                except Exception:
                                    pass
                            time.sleep(3)
                            _shot("flow_3_sent.png")
                            logger.info("【表情流】发送步骤完成 sent=%s", sent)
                except Exception as e:
                    logger.info("【表情流】第3步异常: %s", str(e)[:120])
            result["steps_done"] = steps
            logger.info("【表情流】窗口保持 20 秒供观察")
            page.wait_for_timeout(20000)
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            p.stop()
    except Exception as e:
        logger.error("表情流异常: %s", e)
        result["error"] = f"表情流异常: {e}"
    return result


# ============ 表情操作录制（用户手动演示，记录点位供复刻） ============
emoji_rec_stop: dict[str, bool] = {}


def request_emoji_rec_stop(acc_id: str) -> None:
    emoji_rec_stop[acc_id] = True


def emoji_record_session(acc_id: str, acc_dir: Path, target_name: str = "") -> dict:
    """有头窗口让用户手动操作并全程记录点击（坐标+元素）+ 截图，最多 5 分钟或收到停止信号。"""
    result = {"error": None, "events": 0}
    state_path = acc_dir / "state.json"
    if not state_path.exists():
        result["error"] = "该账号尚未上传登录态 state.json"
        return result
    cfg = load_account_config(acc_dir)
    engine = cfg.get("browser", "chromium")
    emoji_rec_stop[acc_id] = False

    browser = None
    events: list[dict] = []
    try:
        p = sync_playwright().start()
        try:
            browser = _launch_browser(p, engine, False)  # 有头
            # 只注入 cookies（不注入 localStorage），个别账号降级整份 storage_state
            context = _new_context(browser, state_path, acc_dir)
            page = context.new_page()
            for attempt in range(3):
                try:
                    page.goto(CHAT_URL, timeout=60000, wait_until="domcontentloaded")
                    break
                except Exception as e:
                    logger.info("录制：第 %s 次打开页面失败: %s", attempt + 1, str(e)[:80])
                    time.sleep(5)
            page.wait_for_timeout(10000)
            logged, why = check_login(page)
            if not logged:
                result["error"] = why
                return result
            # 打开目标会话
            names = [c.get("name", "") for c in (page.evaluate(_STREAK_EXTRACT_JS) or []) if c.get("name")]
            opened = target_name or (names[0] if names else "")
            if opened:
                try:
                    page.wait_for_selector(".conversationConversationItemtitle", timeout=20000)
                    for attempt in range(4):
                        target = _find_contact(page, opened)
                        if target.count():
                            target.click(force=True, timeout=10000)
                            time.sleep(random.uniform(2, 3))
                            if verify_in_conversation(page, opened):
                                break
                        else:
                            try:
                                page.mouse.move(200, 350)
                                page.mouse.wheel(0, 600)
                            except Exception:
                                pass
                            time.sleep(1.5)
                    if not verify_in_conversation(page, opened):
                        search_and_open(page, opened)
                except Exception as e:
                    logger.info("录制：点开会话异常: %s", str(e)[:80])
            logger.info("【录制】会话已打开: %s —— 请开始手动操作：①点头像 ②点输入框右侧笑脸 ③点面板里的火花（可再点发送）。我会记录每个点击。", opened or "(无)")

            # 注入录制脚本 + console 转发 + 网络监听
            netlog: list[str] = []

            def _on_req(req):
                try:
                    u = req.url
                    lu = u.lower()
                    if any(k in lu for k in ("message/send", "im/send", "/send", "sticker", "emoji", "resource")) and req.method in ("POST", "GET"):
                        body = ""
                        try:
                            if req.post_data:
                                body = " BODY:" + req.post_data[:600]
                        except Exception:
                            pass
                        netlog.append(f"{req.method} {u[:150]}{body}")
                except Exception:
                    pass

            page.on("request", _on_req)

            # 注入点击录制脚本（pointerdown 捕获坐标与元素信息）
            page.evaluate(
                """() => {
                    window.__drec = [];
                    window.__drecOn = true;
                    document.addEventListener('pointerdown', (e) => {
                        if (!window.__drecOn) return;
                        const t = e.target;
                        const rec = {
                            t: Date.now(),
                            x: Math.round(e.clientX), y: Math.round(e.clientY),
                            tag: t.tagName,
                            cls: String(t.className || '').slice(0, 70),
                            aria: (t.getAttribute && (t.getAttribute('aria-label') || '')) || '',
                            title: (t.getAttribute && (t.getAttribute('title') || '')) || '',
                            text: ((t.textContent || '').trim().slice(0, 14)),
                        };
                        window.__drec.push(rec);
                        console.log('DREC:' + JSON.stringify(rec));
                    }, true);
                }"""
            )

            last_shot = 0.0
            start = time.time()
            heartbeat = 0
            while time.time() - start < 300:
                if emoji_rec_stop.get(acc_id):
                    logger.info("【录制】收到停止信号，结束记录")
                    break
                time.sleep(0.25)
                # 取新事件
                try:
                    arr = page.evaluate("() => (window.__drec || []).splice(0, window.__drec.length)") or []
                except Exception:
                    arr = []
                for ev in arr:
                    events.append(ev)
                    logger.info(
                        "【录制】点击 (%s,%s) <%s> cls=%s aria=%s title=%s text=%r",
                        ev.get("x"), ev.get("y"), ev.get("tag"), ev.get("cls"),
                        ev.get("aria"), ev.get("title"), ev.get("text"),
                    )
                # 事件后截图 + 3 秒心跳截图
                now = time.time()
                if events and now - last_shot > 1.0:
                    last_shot = now
                    try:
                        page.screenshot(path=str(acc_dir / f"rec_{len(events):02d}.png"), timeout=5000)
                    except Exception:
                        pass
                if not events and now - last_shot > 3.0 and heartbeat < 30:
                    heartbeat += 1
                    last_shot = now
                    try:
                        page.screenshot(path=str(acc_dir / f"rec_wait{heartbeat:02d}.png"), timeout=5000)
                    except Exception:
                        pass
            result["events"] = len(events)
            logger.info("【录制】共记录 %s 次点击", len(events))
            if netlog:
                logger.info("【录制】网络请求(尾8): %s", netlog[-8:])
                try:
                    (acc_dir / "rec_net.log").write_text("\n".join(netlog), encoding="utf-8")
                    logger.info("【录制】网络记录已存 rec_net.log（%s 条）", len(netlog))
                except Exception:
                    pass
            # 结束前再来一张整页
            try:
                page.screenshot(path=str(acc_dir / "rec_final.png"), timeout=6000)
            except Exception:
                pass
            logger.info("【录制】窗口即将关闭")
            page.wait_for_timeout(2000)
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            p.stop()
    except Exception as e:
        logger.error("录制异常: %s", e)
        result["error"] = f"录制异常: {e}"
    return result


def _open_chat_page(browser, state_path: Path, acc_dir: Path, mode: str):
    """按指定注入方式新建 context 并打开私信页；打不开返回 (None, None)。"""
    context = _new_context(browser, state_path, acc_dir, mode)
    page = context.new_page()
    ok = False
    for attempt in range(3):
        try:
            page.goto(CHAT_URL, timeout=90000, wait_until="domcontentloaded")
            ok = True
            break
        except Exception as e:
            logger.info("打开私信页失败（第 %s 次，注入方式=%s）: %s", attempt + 1, mode, str(e)[:80])
            time.sleep(5)
    if not ok:
        try:
            context.close()
        except Exception:
            pass
        return None, None
    return context, page


def _open_chat_with_fallback(browser, state_path: Path, acc_dir: Path,
                             rounds: int = 2, each_timeout: int = 60,
                             pre_wait_ms: int = 8000):
    """打开私信页并等会话列表就绪，**带 cookie-only 降级兜底**。

    默认只注入 cookies（避免 storage_state 里过期的 localStorage 把列表接口顶成
    `word word` 占位页）；若连续 rounds 轮（默认 2 轮 × 60s）仍读不到任何会话行，
    自动改用整份 storage_state 再试一次，成功则记住该账号偏好（runtime.json storage_mode）。
    返回 (context, page, rows, mode)；彻底失败时仍返回最后一轮的 page，供截图诊断。
    """
    pref = _storage_mode(acc_dir)                       # cookie（默认）/ state（历史偏好）
    modes = [pref] + (["state"] if pref == "cookie" else ["cookie"])
    ctx = page = None
    mode_used, rows = pref, 0
    for i, mode in enumerate(modes):
        last = i == len(modes) - 1
        mode_used = mode
        ctx, page = _open_chat_page(browser, state_path, acc_dir, mode)
        if ctx is None:
            if last:
                return None, None, 0, mode_used
            continue
        try:
            page.wait_for_timeout(pre_wait_ms)
        except Exception:
            time.sleep(pre_wait_ms / 1000.0)
        logged, why = check_login(page)
        if not logged:
            logger.warning("登录态检查未通过（注入方式=%s）：%s", mode, why)
            if last:
                return ctx, page, 0, mode_used
            try:
                ctx.close()
            except Exception:
                pass
            ctx = page = None
            continue
        rows = _wait_conversation_list_ready(page, rounds=rounds, each_timeout=each_timeout)
        if rows:
            if mode != pref:
                logger.warning(
                    "cookie-only 连续 %s 轮读不到会话行 → 已降级为整份 storage_state 并成功，"
                    "记住该账号偏好 storage_mode=%s", rounds, mode,
                )
                _set_storage_mode(acc_dir, mode)
            return ctx, page, rows, mode_used
        logger.warning(
            "注入方式 %s 等 %s 轮（每轮 %s 秒）仍 0 行，改用另一种登录态注入方式再试一次",
            mode, rounds, each_timeout,
        )
        if last:
            return ctx, page, 0, mode_used
        try:
            ctx.close()
        except Exception:
            pass
        ctx = page = None
    return ctx, page, rows, mode_used


def fetch_chat_contacts(acc_dir: Path) -> dict:
    """从抖音私信页读取联系人；TRANSIENT 错误退避重试最多 3 次。

    AUTHENTICATION / RATE_LIMIT 属于「再试只会更糟」：立即停并写 paused_until；
    PERMANENT（没上传登录态、配置缺失）不重试。重试之间按 3.0 * 1.5^n 退避。
    """
    acc_id = acc_dir.name
    last: dict = {"at": _now(), "names": [], "error": "抓取失败"}
    for attempt in range(3):
        res = _fetch_chat_contacts_once(acc_dir)
        if not res.get("error"):
            return res
        err_class = res.get("error_class") or classify_error(None, res.get("error"))
        res["error_class"] = err_class
        last = res
        if err_class != TRANSIENT or attempt >= 2:
            return res
        delay = backoff(attempt)
        logger.warning(
            "抓取联系人遇到临时错误（第 %s/3 次，类别=%s），%.1f 秒后重试：%s",
            attempt + 1, err_class, delay, str(res.get("error"))[:120],
        )
        try:
            fetch_progress[acc_id] = "retry"
        except Exception:
            pass
        time.sleep(delay)
    return last


def _fetch_chat_contacts_once(acc_dir: Path) -> dict:
    """单次抓取（不含重试）；异常/登录失效会写 error_class 供外层决策。"""
    result = {"at": _now(), "names": [], "error": None, "error_class": None}
    state_path = acc_dir / "state.json"
    if not state_path.exists():
        result["error"] = "该账号尚未上传登录态 state.json"
        result["error_class"] = PERMANENT
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
            # ① cookie-only 注入 + 降级兜底：连续 2 轮读不到会话行 → 整份 storage_state 再试一次
            fetch_progress[acc_id] = "wait"
            context, page, rows, mode_used = _open_chat_with_fallback(
                browser, state_path, acc_dir, rounds=2, each_timeout=60, pre_wait_ms=10000
            )
            if context is None or page is None:
                result["error"] = "无法打开抖音私信页面"
                result["error_class"] = TRANSIENT
                return result
            logger.info("本次登录态注入方式：%s（会话行 %s）", mode_used, rows)

            fetch_progress[acc_id] = "login"
            logged, why = check_login(page)
            if not logged:
                result["error"] = why
                result["error_class"] = classify_error(None, why)
                if should_stop_all(result["error_class"]):
                    _pause_account(acc_dir, result["error_class"], f"抓取联系人：{why}")
                _screenshot(page, acc_dir)
                return result

            # 抓取前先确认页面上没有风控浮层（只认可见元素，避免隐藏节点误伤）
            kw = detect_rate_limit(page)
            if kw:
                result["error"] = f"页面出现风控提示「{kw}」"
                result["error_class"] = RATE_LIMIT
                _pause_account(acc_dir, RATE_LIMIT, f"抓取联系人命中风控：{kw}")
                _screenshot(page, acc_dir)
                return result

            fetch_progress[acc_id] = "scrape"
            # 会话列表实测 24~27 秒才渲染：轮询等就绪（就绪立刻继续），超时重载后再给一轮
            collected_by_name: dict[str, dict] = {}
            avatar_map: dict[str, str] = {}        # name -> 头像URL（边滚边记，不覆盖已有头像）
            slot_avatars: dict[str, tuple] = {}    # 行槽位 -> (name, src)：识别改名漂移 / 本屏补采
            avatar_seen: dict[str, int] = {}       # name -> 成功记到头像的次数（统计）

            def _merge_avatar_screen() -> int:
                """抓当前屏头像。只认「真的加载完」的图（img.complete && naturalWidth>0）。

                返回本屏仍未拿到头像的行数（供本屏重试）。同一行前后名字不同（改名漂移）时，
                以最新名字为准并把上一屏同一槽位的头像沿用过去，不丢头像。
                """
                try:
                    avatars = page.evaluate(_VISIBLE_AVATAR_JS) or []
                except Exception:
                    return 0
                cur_names = {(it.get("name") or "").strip() for it in avatars}
                pending = 0
                for it in avatars:
                    nm = (it.get("name") or "").strip()
                    if not nm:
                        continue
                    src = (it.get("src") or "").strip()
                    ok = bool(it.get("ok"))
                    slot = (it.get("slot") or "").strip()
                    prev = slot_avatars.get(slot)
                    if not avatar_map.get(nm):
                        if src and ok:
                            avatar_map[nm] = src
                        elif prev and prev[1]:
                            pname, psrc = prev
                            # ① 同一行本屏图片没加载好 → 用上一屏同槽位记到的地址
                            # ② 同一行前后名字不同（改名漂移）→ 最新名字为准，头像跟过去
                            if pname == nm or pname not in cur_names:
                                avatar_map[nm] = psrc
                    if src and ok:
                        slot_avatars[slot] = (nm, src)
                    elif slot not in slot_avatars:
                        slot_avatars[slot] = (nm, "")
                    if avatar_map.get(nm):
                        avatar_seen[nm] = avatar_seen.get(nm, 0) + 1
                    else:
                        pending += 1
                return pending

            def _collect_avatar_screen(tries: int = 3) -> int:
                """本屏头像采集（最多 3 次 = 首次 + 重试 2 次，等图片懒加载落位）。"""
                pending = 0
                for i in range(max(1, tries)):
                    pending = _merge_avatar_screen()
                    if pending <= 0 or i >= tries - 1:
                        break
                    try:
                        page.wait_for_timeout(400)
                    except Exception:
                        time.sleep(0.4)
                return pending

            def _scrape_visible_screen() -> int:
                """抓当前可见屏：名字/火花合并进 collected_by_name，头像合并进 avatar_map。"""
                try:
                    data = page.evaluate(_STREAK_EXTRACT_JS) or []
                except Exception:
                    data = []
                for item in data:
                    nm = (item.get("name") or "").strip()
                    if not nm:
                        continue
                    item["name"] = nm
                    cur = collected_by_name.get(nm)
                    if cur is None:
                        collected_by_name[nm] = item
                    else:
                        for key in ("streak", "time", "avatar"):
                            if not cur.get(key) and item.get(key):
                                cur[key] = item.get(key)
                # 虚拟滚动：头像只在当前屏的 DOM 里，必须每滚一屏就补记一次
                _collect_avatar_screen()
                return len(data)

            if rows:
                logger.info("会话列表已就绪：当前可见 %s 行", rows)
                page.wait_for_timeout(1500)  # 等首屏布局稳定
                # ② 主列表 / 折叠组 / 陌生人组是三个独立滚动容器：逐个滚到底（主 → 折叠 → 陌生人），
                #    每个容器各自用「真到底」判定，避免只滚主容器漏掉折叠/陌生会话里的好友。
                walked = _scan_all_containers(page, _scrape_visible_screen, max_steps=100)
                logger.info(
                    "滚动扫描完成：%s｜已收录联系人 %s 个",
                    "、".join(f"{lb} {n} 屏" for lb, n in walked) or "无可用容器",
                    len(collected_by_name),
                )
                _scrape_visible_screen()   # 最后一屏再补记一次（兜底）
                # 头像补采：图片懒加载/图床慢导致本屏没拿到头像的行，最多再整轮扫 2 次
                for rnd in range(2):
                    missing_now = [n for n in collected_by_name if not avatar_map.get(n)]
                    if not missing_now:
                        break
                    logger.info("头像补采第 %s 轮：%s 个联系人待补", rnd + 1, len(missing_now))
                    _scan_all_containers(page, _collect_avatar_screen, max_steps=100)
                # P4：整轮扫描结束后，再回到各容器顶部扫一遍可见区（懒加载/图床慢的漏网头像）。
                _rescan_avatars_from_top(page, collected_by_name, avatar_map, _collect_avatar_screen)
                missing_now = [n for n in collected_by_name if not avatar_map.get(n)]
                if missing_now:
                    logger.info(
                        "头像补采结束：仍有 %s 个未拿到（%s）",
                        len(missing_now), "、".join(missing_now[:8]),
                    )
            else:
                logger.warning(
                    "读取到 0 个会话：私信页未加载或该登录态未完成网页端授权，"
                    "建议重新扫码登录并在网页端确认能看到会话列表"
                )

            # 头像本地化：data/avatars/<账号>/<hash>.<ext>（去 host/query 后 hash，签名轮换也能命中）
            # 并发 4（2C2G 上别用 8）、超时 4s、失败静默 —— 失败时前端仍可用原始 URL 兜底
            local_map: dict[str, str] = {}
            todo_urls = {nm: u for nm, u in avatar_map.items() if u}
            if todo_urls:
                adir = avatars_dir(acc_dir)
                try:
                    with ThreadPoolExecutor(max_workers=4) as ex:
                        futs = {ex.submit(save_avatar_file, u, acc_id, adir): nm
                                for nm, u in todo_urls.items()}
                        for fut in as_completed(futs):
                            nm = futs[fut]
                            try:
                                rel = fut.result()
                            except Exception:
                                rel = None
                            if rel:
                                local_map[nm] = rel
                except Exception as e:
                    logger.info("头像本地缓存异常（忽略）: %s", str(e)[:100])
                logger.info("头像本地缓存：%s/%s 个已落盘 → %s",
                            len(local_map), len(todo_urls), adir)

            collected: list[dict] = []
            for item in collected_by_name.values():
                nm = item.get("name") or ""
                item["avatar"] = avatar_map.get(nm) or item.get("avatar") or ""
                item["avatar_local"] = local_map.get(nm, "")   # 本地文件（长期有效，签名过期也不怕）
                collected.append(item)
            # 把联系人昵称登记进日志脱敏表：后续任何日志里出现这些名字都会变成「张*」
            _remember_names([c.get("name") for c in collected])
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
        err_class = classify_error(e, str(e))
        logger.error("获取联系人异常（类别=%s）: %s", err_class, str(e)[:200])
        result["error"] = f"获取联系人异常: {str(e)[:150]}"
        result["error_class"] = err_class
        if should_stop_all(err_class):
            _pause_account(acc_dir, err_class, f"抓取联系人异常：{str(e)[:100]}")
    finally:
        fetch_progress.pop(acc_id, None)
    return result


_SAFE_SEND_RETRY_MARKERS = (
    "未能进入会话", "等待会话列表", "未就绪", "未加载", "尚未渲染",
    "找不到聊天输入框", "未能切到", "未能输入到输入框", "未能输入",
    "超时", "timeout", "网络", "连接", "not ready", "未找到元素",
)


def _should_retry_send(why: str) -> bool:
    """TRANSIENT 重试的安全闸门。

    对照表痛点 4 ③：`uncertain`（点过发送但拿不到证据）**绝不自动重试** —— 重复发送
    比漏发更糟（刷屏 + 触发风控）。这里只对「明确发生在发送动作之前」的失败重试。
    """
    text = str(why or "")
    if any(k in text for k in ("输入框未清空", "可能未发出", "未确认", "uncertain", "发送后")):
        return False
    if "发送异常" in text or "文字段发送异常" in text:
        return False
    # 服务端回执已经给出结论（陌生人/审核/拉黑/未知码）：重发不会变好，甚至可能重复打扰
    if any(k in text for k in ("回执", "审核", "陌生人", "拉黑", "注销", "不在单聊", "未知服务端")):
        return False
    # 含【图】【贴纸】的多段模板：前面几段可能已经发出，整条重发会重复
    if "段类型=" in text:
        return False
    low = text.lower()
    return any(k.lower() in low for k in _SAFE_SEND_RETRY_MARKERS)


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
    # 好友昵称登记进日志脱敏表（发送日志里经常带名字）
    _remember_names(friends)

    result = {
        "at": _now(),
        "dry_run": bool(dry_run),
        "ok": [],
        "uncertain": [],
        "failed": [],
        "logged_out": False,
        "rate_limited": False,
        "error_class": None,
    }

    state_path = acc_dir / "state.json"
    if not state_path.exists():
        result["failed"].append({"name": "_system", "reason": "该账号尚未上传登录态 state.json"})
        result["error_class"] = PERMANENT
        return result

    targets = friends[:max_n] if max_n > 0 else friends
    if not targets:
        # 明确给出原因，避免网页端显示“成功 0 失败 0”让人摸不着头脑
        if cfg.get("friends"):
            reason = "名单里有好友但全部处于停用状态（请到「好友与消息」开启）"
        else:
            reason = "未配置启用的好友（请到「好友与消息」页添加并保存）"
        result["failed"].append({"name": "_system", "reason": reason})
        result["error_class"] = PERMANENT
        return result

    browser = None
    tracker = None
    try:
        p = sync_playwright().start()
        try:
            browser = _launch_browser(p, engine, headless)
            # 只注入 cookies（不注入 localStorage）+ 连续 2 轮读不到会话行时降级整份 storage_state
            context, page, rows, mode_used = _open_chat_with_fallback(
                browser, state_path, acc_dir, rounds=2, each_timeout=60, pre_wait_ms=8000
            )
            if context is None or page is None:
                result["failed"].append({"name": "_system", "reason": "无法打开抖音私信页面"})
                result["error_class"] = TRANSIENT
                return result
            logger.info("本次登录态注入方式：%s（会话行 %s）", mode_used, rows)

            logged, why = check_login(page)
            if not logged:
                result["logged_out"] = True
                result["error_class"] = classify_error(None, why) or AUTHENTICATION
                result["failed"].append({"name": "_system", "reason": why})
                _pause_account(acc_dir, AUTHENTICATION, f"发送前登录态检查失败：{why}")
                _screenshot(page, acc_dir)
                return result

            logger.info("待发送好友 %s 人，dry_run=%s，浏览器=%s", len(targets), dry_run, engine)
            # 私信页实测 24~37 秒才渲染会话列表：_open_chat_with_fallback 内已等就绪（就绪立即继续），
            # 未就绪 reload 再给一轮；两轮都 0 行才报明确的登录态错误。
            if rows <= 0:
                logger.warning(
                    "未能进入会话：等待会话列表 2 轮（约 2 分钟）仍为 0 行，列表状态=%s",
                    _list_state(page),
                )
                result["failed"].append({"name": "_system", "reason": _LIST_NOT_READY_MSG})
                result["error_class"] = classify_error(None, _LIST_NOT_READY_MSG)
                if should_stop_all(result["error_class"]):
                    _pause_account(acc_dir, result["error_class"], "发送前会话列表未就绪")
                _screenshot(page, acc_dir)
                return result
            # 顺手刷新好友名单的火花天数（页面已在私信列表，轻量抓取，不额外开浏览器）
            try:
                _refresh_streaks_from_page(acc_dir, cfg, page)
            except Exception as e:
                logger.info("刷新火花天数失败: %s", str(e)[:80])
            # P1：发送回执监听必须在本轮第一次发送之前挂好（只认 path 结尾锚定的 message/send）
            tracker = MessageSendTracker(page)
            logger.info("发送回执监听已挂载：/v1/message/send（只认 reset() 之后的本轮回包）")
            for name in targets:
                msg = random.choice(messages)
                attempts = 0
                state, why = _NOT_SENT, "未知原因"
                while True:
                    state, why = send_to_contact(page, name, msg, dry_run, tracker=tracker)
                    if state == _CONFIRMED:
                        break
                    if state == _UNCERTAIN:
                        # ① P1 铁律：点过发送但拿不到证据 → 绝不自动重试，也绝不当成功
                        break
                    err_class = classify_error(None, why)
                    # ② AUTH / RATE_LIMIT：立即停本轮 + 暂停账号（不再尝试下一个人）
                    if should_stop_all(err_class):
                        result["error_class"] = err_class
                        if err_class == RATE_LIMIT:
                            result["rate_limited"] = True
                        else:
                            result["logged_out"] = True
                        _pause_account(acc_dir, err_class, f"发送给好友失败：{why[:100]}")
                        break
                    # ③ TRANSIENT：退避重试，最多 3 次，且只在「发送前就失败」时重试
                    if err_class == TRANSIENT and attempts < 3 and _should_retry_send(why):
                        delay = backoff(attempts)
                        logger.warning(
                            "发送给好友遇到临时错误（第 %s/3 次，类别=%s），%.1f 秒后重试：%s",
                            attempts + 1, err_class, delay, str(why)[:100],
                        )
                        time.sleep(delay)
                        attempts += 1
                        continue
                    # ④ PERMANENT / not_sent（回执结论）：不重试
                    break
                if state == _CONFIRMED:
                    result["ok"].append(name)
                    logger.info("已发送给 %s（confirmed）：%s", name,
                                msg if not dry_run else "(干跑，未真实发送)")
                elif state == _UNCERTAIN:
                    result["uncertain"].append({"name": name, "reason": why, "state": _UNCERTAIN})
                    logger.warning(
                        "发送给 %s 结果无法确认（uncertain，不自动重试）：%s", name, why,
                    )
                else:
                    result["failed"].append({"name": name, "reason": why, "state": _NOT_SENT})
                    logger.warning("发送给 %s 失败（类别=%s，not_sent）：%s", name,
                                   result.get("error_class") or classify_error(None, why), why)
                    if result.get("error_class") in (AUTHENTICATION, RATE_LIMIT):
                        logger.warning("已在发送路径判定为 %s，停止本轮（下一个人不再尝试）",
                                       result["error_class"])
                        break
                    kw = detect_rate_limit(page)
                    if kw:
                        result["rate_limited"] = True
                        result["error_class"] = RATE_LIMIT
                        _pause_account(acc_dir, RATE_LIMIT, f"页面出现风控提示：{kw}")
                        logger.warning("页面出现风控提示「%s」，停止本轮", kw)
                        break
                time.sleep(random.uniform(gap_min, gap_max))
            logger.info(
                "本次发送完成（三态）：confirmed %s 人、uncertain %s 人（待人工确认，不自动重试）、"
                "failed %s 人，dry=%s",
                len(result["ok"]), len(result["uncertain"]),
                len([f for f in result["failed"] if isinstance(f, dict) and f.get("name") != "_system"]),
                dry_run,
            )
        finally:
            if tracker:
                try:
                    tracker.close()
                except Exception:
                    pass
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            p.stop()
    except Exception as e:
        msg = str(e)
        err_class = classify_error(e, msg)
        result["error_class"] = err_class
        logger.error("运行异常（类别=%s）: %s", err_class, msg)
        if should_stop_all(err_class):
            _pause_account(acc_dir, err_class, f"发送运行异常：{msg[:100]}")
        # 引擎未安装/路径无效时给出明确的中文指引（而不是一串英文报错）
        if "Executable doesn't exist" in msg or "executable doesn't exist" in msg.lower():
            hint = (
                f"浏览器引擎不可用（未安装或路径无效，当前引擎: {engine}）。"
                "请在网页「定时与浏览器」页把该账号的浏览器引擎改为 Chromium，"
                "或运行 python -m playwright install firefox webkit 安装对应浏览器。"
            )
            result["failed"].append({"name": "_system", "reason": hint})
            result["error_class"] = PERMANENT
        else:
            result["failed"].append({"name": "_system", "reason": f"运行异常: {msg[:150]}"})
    return result
