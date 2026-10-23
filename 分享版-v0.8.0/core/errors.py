"""把发送失败的原因归纳成「人话分类」——推送和界面共用。

使用者不都是技术人：原始报错（Playwright 异常、抖音接口返回、HTTP 码）
必须归纳成几类「看得懂 + 能照做」的原因，而不是把 stack trace 甩给人看。
"""

from __future__ import annotations

# (key, 人话分类, 建议动作, 关键字)
CATEGORIES: list[tuple[str, str, str, tuple[str, ...]]] = [
    ("login", "登录过期", "需要重新扫码登录（点「网页内扫码登录」）",
     ("登录", "login", "cookie", "未登录", "扫码", "expired", "会话失效", "state")),
    ("limit", "被限流/风控", "隔天再试，或减少同时发送的好友数量",
     ("限流", "风控", "频繁", "rate", "too many", "验证", "captcha", "risk", "blocked", "拒绝")),
    ("notfound", "找不到好友", "到「好友与消息」重新刷新名单",
     ("找不到", "未找到", "没找到", "不存在", "not found", "404", "无此人", "已删除", "无匹配", "该好友")),
    ("network", "网络不通", "服务器网络问题，会自动重试",
     ("timeout", "timed_out", "timed out", "err_timed", "err_connection", "err_internet", "err_name_not_resolved", "超时", "网络", "connection", "connect", "econn", "dns", "socket", "proxy")),
    ("emoji", "表情/图片失效", "换一个表情或图片",
     ("表情", "图片", "emoji", "sticker", "素材", "upload")),
    ("browser", "浏览器异常", "重试一次；仍失败就换浏览器",
     ("browser", "chromium", "浏览器", "crash", "target closed", "page closed", "executable", "net::")),
]

UNKNOWN = ("unknown", "其他失败", "把这条发给维护者看看")

_REASON_KEYS = ("reason", "error", "detail", "msg", "message", "why")


def classify(text: str) -> tuple[str, str, str]:
    """返回 (key, 人话分类, 建议动作)。"""
    low = (text or "").lower()
    for key, label, advice, kws in CATEGORIES:
        for kw in kws:
            if kw.lower() in low:
                return key, label, advice
    return UNKNOWN


def label_of(text: str) -> str:
    return classify(text)[1]


def reason_of(item) -> str:
    """从一条失败记录里尽量取出原始原因文本。"""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for k in _REASON_KEYS:
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return ""


def name_of(item) -> str:
    if isinstance(item, dict):
        v = item.get("name")
        if isinstance(v, str):
            return v
    return ""


def group_failures(failed) -> list:
    """把失败列表归成 [(key, 分类, 建议, [名字...]), ...]，按人数多的排前面。"""
    buckets = {}
    for item in failed or []:
        name = name_of(item)
        if name == "_system":
            continue
        key, label, advice = classify(reason_of(item))
        buckets.setdefault(key, [label, advice, []])[2].append(name or "未知好友")
    out = [(k, v[0], v[1], v[2]) for k, v in buckets.items()]
    out.sort(key=lambda x: -len(x[3]))
    return out


def summary_line(failed, max_names: int = 3) -> str:
    """一行人的总结，例如：
    失败分类：登录过期 2 人（夏桃分、潍殷）；找不到好友 1 人（老王）
    建议：需要重新扫码登录（点「网页内扫码登录」）
    """
    groups = group_failures(failed)
    if not groups:
        return ""
    parts = []
    for _key, label, _advice, names in groups:
        shown = "、".join(names[:max_names])
        if len(names) > max_names:
            shown += " 等 %d 人" % len(names)
        parts.append("%s %d 人（%s）" % (label, len(names), shown))
    line = "失败分类：" + "；".join(parts)
    top = groups[0]
    if top[2]:
        line += "\n建议：" + top[2]
    return line


# ======================================================================
# 运行级错误分类（可靠性层）
# ======================================================================
#
# 依据《借鉴/对照表.md》§4「Top3」与痛点 4/5：
#   AUTHENTICATION 登录态失效（二维码/登录文案/sessionid 缺失）→ 立即停本轮 + 暂停 2h
#   RATE_LIMIT     命中风控关键词（操作频繁/安全验证…）        → 立即停本轮 + 暂停 2h
#   TRANSIENT      超时/网络/元素未就绪                        → 退避重试（最多 3 次）
#   PERMANENT      找不到好友/同名/配置缺失                    → 不重试
#
# classify_error 用「异常类型 + 中文关键词」双判据，判不准时按 TRANSIENT
# （宁可多试一次，也不要因为误判 PERMANENT 漏发）。

AUTHENTICATION = "AUTHENTICATION"
RATE_LIMIT = "RATE_LIMIT"
TRANSIENT = "TRANSIENT"
PERMANENT = "PERMANENT"

ERROR_CLASSES = (AUTHENTICATION, RATE_LIMIT, TRANSIENT, PERMANENT)
STOP_ALL_CLASSES = frozenset({AUTHENTICATION, RATE_LIMIT})

# 限流关键词：cloud-streak guard.py 的 13 条 + 「请稍后」。
# 注意：**必须要求元素可见（bounding_box 非空）才算命中**，可见性判断在
# automation.detect_rate_limit() 内完成（隐藏节点同样含这些文案，直接扫 body 会误伤）。
RATE_LIMIT_KEYWORDS = [
    "操作频繁",
    "操作太频繁",
    "发送过于频繁",
    "请稍后再试",
    "稍后再试",
    "请稍后",
    "安全验证",
    "滑动验证",
    "验证码",
    "验证中心",
    "人机验证",
    "网络异常",
    "请勿频繁",
]

# 登录/授权类：先判「验证码登录/扫码登录」这类**入口文案**，再判风控。
AUTH_KEYWORDS = [
    "扫码登录",
    "验证码登录",
    "登录已过期",
    "登录态已过期",
    "登录态失效",
    "登录失效",
    "未登录",
    "请先登录",
    "需要重新登录",
    "重新扫码",
    "sessionid",
    "passport",
    "login required",
    "需要登录",
]

# 配置/身份类：这些如果同时含「登录」等字样，必须先判 PERMANENT，
# 否则「尚未上传登录态」会被误判成 AUTHENTICATION → 把账号无谓暂停 2 小时。
PERMANENT_KEYWORDS = [
    "尚未上传",
    "请先上传",
    "未上传登录态",
    "没有启用的好友",
    "未配置启用的好友",
    "配置错误",
    "名单里",
    "找不到该好友",
    "未能找到该好友",
    "未找到该好友",
    "未能在会话列表里找到",
    "未能切到该好友的会话",
    "好友不存在",
    "该好友不存在",
    "同名会话",
    "同名好友",
    "不在单聊",
    "对方不在单聊",
    "已被拉黑",
    "已注销",
    # P3：搜索兜底后重载仍 0 行 = 私信页未就绪（目标状态明确，不重试，避免反复空耗）
    "私信页未就绪",
    "会话列表未就绪",
    "会话列表未加载",
]

TRANSIENT_KEYWORDS = [
    "timeout",
    "timed out",
    "time out",
    "超时",
    "网络",
    "connection",
    "connect error",
    "net::",
    "econn",
    "err_",
    "socket",
    "target closed",
    "target page, context or browser has been closed",
    "browser has been closed",
    "crash",
    "not visible",
    "element is not attached",
    "detached",
    "未加载",
    "未就绪",
    "未能进入会话",
    "尚未渲染",
    "等待会话列表",
    "找不到聊天输入框",
    "找不到页面",
    "元素不可见",
    "元素未出现",
    "执行超时",
    "页面加载",
    "重新尝试",
]

# Playwright 常见异常类型名（不 import playwright，保持本模块可在无浏览器环境跑单测）
_TRANSIENT_EXC_NAMES = {
    "TimeoutError",
    "TargetClosedError",
    "BrowserError",
    "ConnectionError",
    "ConnectionClosedError",
    "OSError",
    "NetError",
}


def classify_error(exc: BaseException | None = None, text: str | None = None) -> str:
    """把异常/文本归到 AUTHENTICATION / RATE_LIMIT / TRANSIENT / PERMANENT。

    · 先看异常类型名（Playwright TimeoutError / TargetClosedError → TRANSIENT）
    · 再看关键词：PERMANENT（配置/身份）> AUTH（登录入口）> RATE_LIMIT > TRANSIENT
    · 都判不出来 → TRANSIENT（安全默认：允许少量重试，而不是直接放弃）
    """
    msg = str(text if text is not None else (exc if exc is not None else "") or "")
    low = msg.lower()

    # 1) 明确是配置/身份问题 → PERMANENT（必须在 AUTH 之前，见 PERMANENT_KEYWORDS 注释）
    if any(k.lower() in low for k in PERMANENT_KEYWORDS):
        return PERMANENT

    # 2) 登录入口/登录态 → AUTHENTICATION（含「验证码登录」这种会被限流词误吃的入口文案）
    if any(k.lower() in low for k in AUTH_KEYWORDS):
        return AUTHENTICATION

    # 3) 风控/限流关键词
    if any(k.lower() in low for k in RATE_LIMIT_KEYWORDS):
        return RATE_LIMIT

    # 4) 异常类型：超时/浏览器断开/网络等 → TRANSIENT
    name = type(exc).__name__ if exc is not None else ""
    if name in _TRANSIENT_EXC_NAMES:
        return TRANSIENT

    # 5) 临时性文案
    if any(k.lower() in low for k in TRANSIENT_KEYWORDS):
        return TRANSIENT

    # 6) 兜底：泛「登录」字样（排除上面已命中的入口文案）
    if "登录" in msg:
        return AUTHENTICATION

    return TRANSIENT


def should_stop_all(err_class: str) -> bool:
    """AUTHENTICATION / RATE_LIMIT 必须立刻停本轮（并暂停账号）。"""
    return str(err_class or "").upper() in STOP_ALL_CLASSES


def backoff(attempt: int) -> float:
    """指数退避：3.0 * 1.5**attempt（attempt 从 0 开始：3s / 4.5s / 6.75s）。"""
    try:
        n = max(0, int(attempt))
    except Exception:
        n = 0
    return 3.0 * (1.5 ** n)


def max_retries(err_class: str) -> int:
    """各类错误的最大重试次数：TRANSIENT=3，其余=0。"""
    return 3 if str(err_class or "").upper() == TRANSIENT else 0


def error_text(exc: BaseException | None = None) -> str:
    """取可读错误文本（异常 + 原因），给 classify_error / 日志用。"""
    if exc is None:
        return ""
    parts = [str(exc)]
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        parts.append(str(cause))
    return " | ".join(p for p in parts if p)