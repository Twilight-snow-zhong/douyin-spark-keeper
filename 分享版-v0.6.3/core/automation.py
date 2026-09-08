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

from .config import DATA_DIR, load_account_config, save_account_config

logger = logging.getLogger("douyin-spark")

CHAT_URL = "https://www.douyin.com/chat"

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

_IMAGE_DIAG_JS = """
    () => {
        const out = { fileInputs: [], ceditables: [], inputs: [], buttons: [], inputAreaCls: '', note: '' };
        document.querySelectorAll('input[type="file"]').forEach(el => {
            out.fileInputs.push({
                accept: (el.accept || '').slice(0, 60),
                cls: String(el.className || '').slice(0, 60),
                visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                parentCls: String((el.parentElement && el.parentElement.className) || '').slice(0, 60)
            });
        });
        // 所有 contenteditable（不限制大小/类名长度，逐个报告）
        document.querySelectorAll('[contenteditable="true"]').forEach(el => {
            const r = el.getBoundingClientRect();
            out.ceditables.push({
                cls: String(el.className || '').slice(0, 110),
                w: Math.round(r.width), h: Math.round(r.height),
                vis: r.width > 5 && r.height > 5,
                txt: (el.textContent || '').slice(0, 20)
            });
        });
        // 同时保留宽泛的输入区查找
        document.querySelectorAll('[contenteditable="true"], textarea, [class*="composer" i]').forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width > 120 && r.height > 18) {
                out.inputs.push({
                    tag: el.tagName, ce: !!el.isContentEditable,
                    cls: String(el.className || '').slice(0, 90),
                    ph: ((el.getAttribute && el.getAttribute('placeholder')) || '').slice(0, 30),
                    w: Math.round(r.width), h: Math.round(r.height)
                });
            }
        });
        const pick = document.querySelector('[contenteditable="true"]');
        if (!pick) { out.note = '仍未找到 contenteditable 输入框'; return out; }
        // 从输入框向上收集工具栏按钮
        const seen = new Set();
        let node = pick;
        for (let k = 0; k < 6 && node && node.parentElement; k++) {
            node = node.parentElement;
            if (!out.inputAreaCls) out.inputAreaCls = String(node.className || '').slice(0, 90);
            node.querySelectorAll('button, [role="button"], div[class*="icon" i]').forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width < 2 || r.height < 2) return;
                const cls = String(el.className || '');
                const aria = el.getAttribute('aria-label') || '';
                const title = el.getAttribute('title') || '';
                const key = aria + '|' + title + '|' + cls.slice(0, 40);
                if (seen.has(key)) return;
                seen.add(key);
                const hasIcon = el.querySelector('svg, img') !== null;
                if (r.width < 80 && r.height < 80 && (aria || title || hasIcon)) {
                    out.buttons.push({ tag: el.tagName, aria: aria.slice(0, 30), title: title.slice(0, 30), cls: cls.slice(0, 60), img: hasIcon, w: Math.round(r.width) });
                }
            });
            if (out.buttons.length > 8) break;
        }
        out.buttons = out.buttons.slice(0, 40);
        return out;
    }
"""

_DIAG_CLICK_CONV_JS = """
    () => {
        const items = document.querySelectorAll('.conversationConversationItemtitle, [data-e2e="conversation-item"]');
        if (items.length) { items[0].click(); return true; }
        return false;
    }
"""

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


def _send_text_segment(page, seg_text: str) -> tuple[bool, str]:
    """发送一段纯文字（输入→Enter→确认清空；失败重试一次）。"""
    try:
        input_box = page.locator('div[contenteditable="true"]').first
        if input_box.count() == 0 or input_box.bounding_box() is None:
            return False, "找不到聊天输入框"
        if detect_rate_limit(page):
            return False, "检测到验证提示"
        if not _type_and_send(page, input_box, seg_text):
            return False, "文字未能输入到输入框"
        if _wait_input_cleared(input_box, seg_text, wait=8):
            return True, "ok"
        logger.warning("未检测到文字段发出，重试一次")
        if detect_rate_limit(page):
            return False, "重试时检测到验证提示"
        if not _type_and_send(page, input_box, seg_text):
            return False, "重试时文字未能输入"
        if _wait_input_cleared(input_box, seg_text, wait=8):
            return True, "ok"
        return False, "发送后输入框未清空，文字可能未发出"
    except Exception as e:
        logger.info("文字段发送异常: %s", e)
        return False, f"文字段发送异常: {str(e)[:80]}"


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


def send_media_message(page, msg_text: str, emojis_dir: Path | None = None) -> tuple[bool, str]:
    """按模板段依次发送：文字走 Enter，图片走原生图片通道，火花/贴纸走原生贴纸面板。返回 (成功?, 说明)。"""
    segments = split_message_segments(msg_text, emojis_dir)
    sent_any = False
    for kind, payload in segments:
        if kind == "text":
            ok, why = _send_text_segment(page, payload)
        elif kind == "image":
            ok, why = send_image_to_page(page, payload)
        elif kind == "sticker":
            ok, why = send_native_sticker(page, payload)
        else:
            ok, why = send_native_flame(page)
        if not ok:
            return False, why or f"发送「{kind}」段失败"
        sent_any = True
    return sent_any, "ok"


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

    # 含【图:】【火花】【贴纸:】标记的模板走分段发送（文字 + 原生图片 + 原生贴纸）
    if _EMOJI_MARKER_RE.search(msg_text) or _FLAME_MARKER_RE.search(msg_text) or _STICKER_MARKER_RE.search(msg_text):
        return send_media_message(page, msg_text)

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


_UPLOAD_OBSERVE_JS = """
    () => {
        const out = {
            imgs: document.images.length,
            bigRightImgs: 0,
            inputs: [],
            editorHtml: '',
            composerText: '',
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
        const ed = document.querySelector('.messageEditorimChatEditorContainer');
        if (ed) out.editorHtml = ed.outerHTML.slice(0, 160);
        const ce = document.querySelector('[contenteditable="true"]');
        if (ce) out.composerText = (ce.textContent || '');
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
            context = browser.new_context(
                storage_state=str(state_path),
                viewport={"width": 1366, "height": 768},
            )
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
                titles = (
                    page.evaluate(
                        "() => [...document.querySelectorAll('.conversationConversationItemtitle')]"
                        ".map(e => (e.textContent || '').trim()).slice(0, 8)"
                    )
                    or []
                )
                logger.info("【图片测试】侧栏会话(%s个): %s", len(titles), titles)
                logger.info("【图片测试】目标「%s」在侧栏=%s", target_name, target_name in titles)
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
                f0.get("bigRightImgs"), f0.get("imgs"), bool(f0.get("editorHtml")),
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


def diagnose_image_entry(acc_dir: Path) -> dict:
    """诊断抖音聊天页的图片/表情上传入口结构（为原生图片发送功能做准备）。"""
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
            context = browser.new_context(
                storage_state=str(state_path),
                viewport={"width": 1366, "height": 768},
            )
            page = context.new_page()
            for attempt in range(3):
                try:
                    page.goto(CHAT_URL, timeout=60000, wait_until="domcontentloaded")
                    break
                except Exception as e:
                    logger.info("图片诊断：第 %s 次打开页面失败: %s", attempt + 1, str(e)[:80])
                    time.sleep(5)
            page.wait_for_timeout(10000)
            logged, why = check_login(page)
            if not logged:
                result["error"] = why
                return result
            # 复用真实发送的点开流程：读第一个联系人 → 点击标题 → 校验切换 → 失败搜索兜底
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
                logger.info("【图片诊断】点开会话异常: %s", str(e)[:80])
            logger.info("【图片诊断】尝试点开: %s", opened_name or "(无联系人)")
            page.wait_for_timeout(4000)
            diag = page.evaluate(_IMAGE_DIAG_JS) or {}
            result.update(diag)
            result["opened_name"] = opened_name
            logger.info("【图片诊断】fileInputs=%s", diag.get("fileInputs"))
            logger.info("【图片诊断】ceditables=%s", diag.get("ceditables"))
            logger.info("【图片诊断】inputs=%s", diag.get("inputs"))
            if diag.get("note"):
                logger.info("【图片诊断】%s", diag.get("note"))
            btns = diag.get("buttons") or []
            logger.info("【图片诊断】inputAreaCls=%s | 按钮数=%s", diag.get("inputAreaCls"), len(btns))
            for b in btns[:30]:
                logger.info(
                    "【图片诊断】按钮 tag=%s aria=%s title=%s cls=%s 含图标=%s",
                    b.get("tag"), b.get("aria"), b.get("title"), b.get("cls"), b.get("img"),
                )
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            p.stop()
    except Exception as e:
        logger.error("图片诊断异常: %s", e)
        result["error"] = f"图片诊断异常: {e}"
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
            context = browser.new_context(
                storage_state=str(state_path),
                viewport={"width": 1366, "height": 768},
            )
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
                                        logger.info("【表情诊断】点表情后输入框=%r", ce_txt[:40])
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
                                logger.info("【表情诊断】点单元后 输入框=%r 出现发送?=%s", ce_txt[:40],
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
            context = browser.new_context(
                storage_state=str(state_path),
                viewport={"width": 1366, "height": 768},
            )
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
            context = browser.new_context(
                storage_state=str(state_path),
                viewport={"width": 1366, "height": 768},
            )
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
                        logger.info("【表情流】点表情后 输入框=%r", ce_txt[:30])
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
            context = browser.new_context(
                storage_state=str(state_path),
                viewport={"width": 1366, "height": 768},
            )
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
