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