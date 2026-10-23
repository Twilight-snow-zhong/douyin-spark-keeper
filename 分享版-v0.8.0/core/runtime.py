"""运行状态与日志。

- 账号级状态：data/accounts/<id>/runtime.json（运行结果、历史、联系人）
- 账号级日志：data/accounts/<id>/logs/account.log
- 全局日志：data/logs/app.log + 内存环形缓冲（供网页端 /api/logs 使用）
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from .config import DATA_DIR, data_dir

RUNTIME_PATH = DATA_DIR / "runtime.json"
LOG_DIR = DATA_DIR / "logs"                                # 老路径（兼容保留）

def logs_dir() -> Path:
    """当前工作区的日志目录（未设置上下文时 = 老路径 data/logs）。"""
    return data_dir() / "logs"

_lock = threading.Lock()
_ring: deque = deque(maxlen=600)          # 存 (工作区, 日志行) 二元组
_tls = threading.local()
_account_handlers: dict[str, logging.Handler] = {}


def _default() -> dict:
    return {"session_status": "unknown", "running": False, "last_run": None, "history": []}


def load_runtime(acc_dir: Path) -> dict:
    rt = _default()
    if (acc_dir / "runtime.json").exists():
        try:
            data = json.loads((acc_dir / "runtime.json").read_text(encoding="utf-8"))
            if isinstance(data, dict):
                rt.update(data)
        except Exception:
            pass
    return rt


def _save(acc_dir: Path, rt: dict) -> None:
    with _lock:
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "runtime.json").write_text(
            json.dumps(rt, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def set_running(acc_dir: Path, value: bool) -> None:
    rt = load_runtime(acc_dir)
    rt["running"] = bool(value)
    _save(acc_dir, rt)


def record_run(acc_dir: Path, result: dict, update_session: bool = True) -> None:
    """记录一次运行结果。

    update_session=False（干跑）时只写 last_run/history，**不改 session_status**：
    干跑失败往往只是目标好友不存在/环境问题，把它写成 session_status=failed 会让前端
    误报「登录态/发送异常」。
    """
    rt = load_runtime(acc_dir)
    rt["last_run"] = result
    history = rt.get("history", [])
    history.insert(0, result)
    rt["history"] = history[:30]

    if update_session:
        if result.get("logged_out"):
            rt["session_status"] = "expired"
        elif result.get("ok") and not result.get("failed"):
            # 有 confirmed 成功；若还有 uncertain，则只能算「部分成功」（不能当全部成功）
            rt["session_status"] = "partial" if result.get("uncertain") else "ok"
        elif result.get("ok"):
            rt["session_status"] = "partial"
        elif not result.get("failed"):
            rt["session_status"] = "partial" if result.get("uncertain") else "ok"
        else:
            rt["session_status"] = "failed"
    _save(acc_dir, rt)


def _contact_key(item: dict) -> str:
    """联系人主键：优先 convId（能拿到时更稳定），否则退回昵称。"""
    cid = str(item.get("conv_id") or item.get("conversation_id") or "").strip()
    if cid:
        return "conv:" + cid
    return "name:" + str(item.get("name") or "").strip()


def _find_old_match(item: dict, old: list[dict], used: set[int]) -> tuple[int, dict] | None:
    """在旧名单里找与 item 同一个人：先按 convId，再按昵称（改名漂移时两者互补）。"""
    cid = str(item.get("conv_id") or item.get("conversation_id") or "").strip()
    name = str(item.get("name") or "").strip()
    if cid:
        for i, o in enumerate(old):
            if i in used:
                continue
            ocid = str(o.get("conv_id") or o.get("conversation_id") or "").strip()
            if ocid and ocid == cid:
                return i, o
    if name:
        for i, o in enumerate(old):
            if i in used:
                continue
            if str(o.get("name") or "").strip() == name:
                return i, o
    return None


def _merge_contacts(old: list, new: list, old_first: bool = False) -> tuple[list[dict], dict]:
    """P2：联系人增量合并（绝不整表覆盖）。

    - 命中旧联系人：保留旧字段（头像/本地缓存等），用新抓到的非空字段更新，stale=False；
    - 本次没出现的老联系人：**保留**并标 stale=true（不删），计入「缺失」；
    - 新出现的：追加，计入「新增」。
    old_first=True（数量骤降时）：以旧列表顺序为主，把新结果并入，避免整表被劣化结果替换。
    返回 (合并后的列表, 统计)。
    """
    old_items = [dict(x) for x in (old or []) if isinstance(x, dict) and str(x.get("name") or "").strip()]
    new_items = [dict(x) for x in (new or []) if isinstance(x, dict) and str(x.get("name") or "").strip()]
    used: set[int] = set()

    def _merge_one(item: dict) -> dict:
        m = _find_old_match(item, old_items, used)
        if m is None:
            base = dict(item)
            base["stale"] = False
            return base
        i, o = m
        used.add(i)
        base = dict(o)
        for k, v in item.items():
            if v not in (None, "", [], {}):
                base[k] = v
        base["stale"] = False
        return base

    if old_first:
        merged_map = []
        used_new: set[int] = set()
        for o in old_items:
            new_match = None
            cid_o = str(o.get("conv_id") or o.get("conversation_id") or "").strip()
            name_o = str(o.get("name") or "").strip()
            for j, n in enumerate(new_items):
                if j in used_new:
                    continue
                cid_n = str(n.get("conv_id") or n.get("conversation_id") or "").strip()
                name_n = str(n.get("name") or "").strip()
                if (cid_o and cid_n and cid_o == cid_n) or (name_o and name_o == name_n):
                    new_match = (j, n)
                    break
            if new_match:
                j, n = new_match
                used_new.add(j)
                base = dict(o)
                for k, v in n.items():
                    if v not in (None, "", [], {}):
                        base[k] = v
                base["stale"] = False
                merged_map.append(base)
            else:
                base = dict(o)
                base["stale"] = True
                merged_map.append(base)
        for j, n in enumerate(new_items):
            if j in used_new:
                continue
            base = dict(n)
            base["stale"] = False
            merged_map.append(base)
        new_n, kept_n = len(new_items), len(used_new)
        added = len(new_items) - kept_n
        missing = len(old_items) - kept_n
        merged = merged_map
    else:
        merged = [_merge_one(n) for n in new_items]
        for i, o in enumerate(old_items):
            if i in used:
                continue
            base = dict(o)
            base["stale"] = True
            merged.append(base)
        new_n, kept_n = len(new_items), len(used)
        added = new_n - kept_n
        missing = len(old_items) - kept_n

    stats = {
        "old": len(old_items),
        "new": new_n,
        "added": max(0, added),
        "kept": max(0, kept_n),
        "missing": max(0, missing),
        "merged": len(merged),
        "stale": sum(1 for c in merged if c.get("stale")),
    }
    return merged, stats


def record_contacts(acc_dir: Path, data: dict) -> None:
    """写入联系人名单：按 convId/昵称与旧名单**增量合并**，绝不整表覆盖。"""
    rt = load_runtime(acc_dir)
    old = rt.get("contacts") or []
    new = data.get("names") or []
    logger = logging.getLogger("douyin-spark")
    err = data.get("error")

    # 抓取失败且没有任何新数据：保留原名单（旧实现会直接写成 []，等于把名单清空）
    if err and not new:
        rt["contacts_error"] = err
        logger.warning("联系人抓取失败，保留原有 %s 个联系人不变（错误：%s）",
                       len(old) if isinstance(old, list) else 0, str(err)[:160])
        _save(acc_dir, rt)
        return

    old_n = len(old) if isinstance(old, list) else 0
    new_n = len(new)
    # P2 骤降告警：本次 < 上次的 70% 且上次 >= 20 → 可能是采集不全/私信页未就绪
    drop = old_n >= 20 and new_n < old_n * 0.7
    if drop:
        logger.warning(
            "联系人数量骤降：上次 %s 个 → 本次 %s 个（低于上次的 70%%）。"
            "可能是采集不全/私信页未就绪；本次不替换旧列表，保留旧名单并把新结果并入"
            "（缺失的旧联系人标 stale=true）",
            old_n, new_n,
        )
    merged, stats = _merge_contacts(old, new, old_first=drop)
    rt["contacts"] = merged
    rt["contacts_at"] = data.get("at")
    rt["contacts_error"] = err
    rt["contacts_merge"] = stats
    logger.info(
        "联系人增量合并：新增 %s、保留(命中) %s、缺失(保留为 stale) %s｜合并后 %s 个（上次 %s / 本次 %s）",
        stats["added"], stats["kept"], stats["missing"], stats["merged"], old_n, new_n,
    )
    _save(acc_dir, rt)


def update_runtime(acc_dir: Path, **fields) -> None:
    rt = load_runtime(acc_dir)
    rt.update(fields)
    _save(acc_dir, rt)


def read_account_log_tail(acc_dir: Path, n: int = 300) -> str:
    """读账号级日志文件末尾 n 行。"""
    path = acc_dir / "logs" / "account.log"
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


# ---------------- 敏感信息脱敏（只作用于日志，不改返回给前端的数据） ----------------
#
# 参考《借鉴》douyin-auto-fire 的 privacy.RedactingFormatter：用「白名单式替换」
# 做日志最后一道闸门，而不是要求每个 log 调用点自觉打码（迟早会漏一处）。
#
# 规则：
#   · 好友昵称 → 保留首字 + *（如「张三」→「张*」）
#   · 手机号 / 纯数字抖音号 → 保留前 3 位 + 后 2 位（如 13812345678 → 138******78）
#   · token / auth_token / X-Auth-Token → 整体打码
#
# 昵称是任意字符串，没法用正则识别 → 由业务侧调用 register_sensitive_names()
# 把「确实是人名」的字符串登记进来（抓联系人、加载好友名单、启动时读配置）。
# 抖音号也没有统一长度：≥7 位纯数字按号处理，更短的要显式调用 mask_number()。

_SENSITIVE_NAMES: set[str] = set()
# 纯数字的「抖音号 / 用户 ID」单独一张表：只做精确替换。
# 不能用「≥6 位数字一律打码」的正则 —— 会把 URL 里的文件 hash（01719046…）、
# 签名过期时间戳（1792717954）一起打掉，日志里的图片地址/时间就没法看了。
_SENSITIVE_IDS: set[str] = set()
_sensitive_lock = threading.Lock()

_TOKEN_RE = re.compile(
    r"(?i)\b(x-auth-token|auth_token|token|sig)\b(\s*[=:]\s*)([^\s&\"',;]+)"
)
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


def mask_name(name: str) -> str:
    """昵称脱敏：保留首字 + *。"""
    s = (name or "").strip()
    return (s[0] + "*") if s else ""


def mask_number(value) -> str:
    """数字脱敏：保留前 3 位 + 后 2 位（不足 6 位原样返回，避免把计数打废）。"""
    s = str(value if value is not None else "")
    digits = re.sub(r"\D", "", s)
    if len(digits) <= 5:
        return s
    return digits[:3] + "*" * (len(digits) - 5) + digits[-2:]


def register_sensitive_names(names) -> None:
    """登记需要脱敏的昵称 / 纯数字抖音号。

    · 长度 <2 的跳过（单字姓名替换成 * 会到处误伤）
    · 纯数字且 ≥6 位 → 当作抖音号/用户 ID，精确替换（保留前3后2）
    · 纯数字但很短（如群名「19」）→ 不登记，否则「191 个联系人」会变成「1*1 个」
    """
    with _sensitive_lock:
        for n in names or []:
            s = str(n or "").strip()
            if len(s) < 2:
                continue
            if s.isdigit():
                if len(s) >= 6:
                    _SENSITIVE_IDS.add(s)
            else:
                _SENSITIVE_NAMES.add(s)


def clear_sensitive_names() -> None:
    with _sensitive_lock:
        _SENSITIVE_NAMES.clear()
        _SENSITIVE_IDS.clear()


def redact_text(text: str) -> str:
    """对一段日志文本做脱敏（token → 手机号 → 已登记的抖音号 → 已登记的昵称）。"""
    if not text:
        return text
    try:
        text = _TOKEN_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
        text = _PHONE_RE.sub(lambda m: mask_number(m.group(0)), text)
        with _sensitive_lock:
            ids = sorted(_SENSITIVE_IDS, key=len, reverse=True)
            names = sorted(_SENSITIVE_NAMES, key=len, reverse=True)
        for sid in ids:
            if sid in text:
                text = text.replace(sid, mask_number(sid))
        for name in names:
            if name in text:
                text = text.replace(name, mask_name(name))
    except Exception:
        pass
    return text


class RedactFilter(logging.Filter):
    """logging.Filter 版脱敏：直接改 msg/args 里的字符串，不破坏 %-格式化。

    注意不能使用 `record.getMessage()` 后清空 args —— uvicorn 的 AccessFormatter
    依赖 args 里的 client_addr/request_line/status_code，清掉会把访问日志变成
    `Message: '...'`。这里逐个把 args 中的字符串脱敏，格式链完全不变。
    """

    @staticmethod
    def _value(v):
        """递归处理 list/dict 参数（诊断日志常把文本样本放进列表里打）。"""
        if isinstance(v, str):
            return redact_text(v)
        if isinstance(v, list):
            return [RedactFilter._value(x) for x in v]
        if isinstance(v, tuple):
            return tuple(RedactFilter._value(x) for x in v)
        if isinstance(v, dict):
            return {k: RedactFilter._value(x) for k, x in v.items()}
        return v

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact_text(record.msg)
            args = record.args
            if isinstance(args, tuple):
                record.args = tuple(self._value(a) for a in args)
            elif isinstance(args, dict):
                record.args = {k: self._value(v) for k, v in args.items()}
        except Exception:
            pass
        return True


class RedactingFormatter(logging.Formatter):
    """Formatter 版脱敏：覆盖 traceback/异常文本（Filter 只处理 message）。"""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


_REDACT_FILTER: RedactFilter | None = None


def _redact_filter() -> RedactFilter:
    global _REDACT_FILTER
    if _REDACT_FILTER is None:
        _REDACT_FILTER = RedactFilter()
    return _REDACT_FILTER


def install_log_redaction() -> None:
    """把脱敏 Filter 挂到根/应用/uvicorn 的所有 handler 上（可重复调用，幂等）。

    uvicorn 的 access log 会原样打 `GET /api/avatar?...&token=xxx`，必须在
    它自己的 handler 上也挂 Filter，否则只改我们的 logger 是漏的。
    """
    flt = _redact_filter()
    for name in ("", "douyin-spark", "uvicorn", "uvicorn.access", "uvicorn.error"):
        try:
            lg = logging.getLogger(name)
        except Exception:
            continue
        try:
            lg.addFilter(flt)
        except Exception:
            pass
        for h in list(getattr(lg, "handlers", []) or []):
            try:
                h.addFilter(flt)
            except Exception:
                pass


# ---------------- 运行暂停（限流/登录失效）与连败熔断 ----------------

PAUSE_MINUTES_DEFAULT = 120          # 命中风控/掉登录后默认停 2 小时


def _now_dt() -> datetime:
    return datetime.now().astimezone()


def _now_iso() -> str:
    return _now_dt().isoformat(timespec="seconds")


def _parse_dt(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(raw))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()          # 老数据可能没带时区，按本机时区解释
    return dt


def pause_account(acc_dir: Path, minutes: int = PAUSE_MINUTES_DEFAULT, reason: str = "") -> str:
    """把账号暂停到 now+minutes（限流/掉登录用），返回 paused_until。"""
    until = (_now_dt() + timedelta(minutes=max(1, int(minutes)))).isoformat(timespec="seconds")
    update_runtime(
        acc_dir,
        paused_until=until,
        paused_at=_now_iso(),
        pause_reason=str(reason or "")[:200],
    )
    return until


def clear_pause(acc_dir: Path) -> None:
    update_runtime(acc_dir, paused_until=None, paused_at=None, pause_reason=None)


def pause_state(acc_dir: Path) -> dict:
    """返回 {paused, until, reason}；已过期视为未暂停。"""
    rt = load_runtime(acc_dir)
    raw = rt.get("paused_until")
    until = _parse_dt(raw) if raw else None
    if not until or until <= _now_dt():
        return {"paused": False, "until": "", "reason": ""}
    return {"paused": True, "until": str(raw), "reason": str(rt.get("pause_reason") or "")}


def is_skipped_today(acc_dir: Path) -> bool:
    """连败熔断：当天是否已标记跳过该账号。"""
    rt = load_runtime(acc_dir)
    return str(rt.get("skip_date") or "") == _now_dt().date().isoformat()


def record_run_outcome(acc_dir: Path, ok: bool, threshold: int = 3) -> dict:
    """记录一次「发送运行」的成败，维护 consecutive_failures / skip_date。

    ok=True  → 连败清零、解除当天跳过；
    ok=False → 连败 +1；达到 threshold（默认 3）→ 当天跳过并标记需要告警一次。
    返回 {consecutive_failures, alert, skip_date}。
    """
    rt = load_runtime(acc_dir)
    today = _now_dt().date().isoformat()
    if ok:
        update_runtime(acc_dir, consecutive_failures=0, skip_date=None, last_success_at=_now_iso())
        return {"consecutive_failures": 0, "alert": False, "skip_date": ""}

    n = int(rt.get("consecutive_failures") or 0) + 1
    fields: dict = {"consecutive_failures": n, "last_failure_at": _now_iso()}
    alert = False
    skip = ""
    if n >= max(1, int(threshold)):
        skip = today
        fields["skip_date"] = today
        if str(rt.get("consecutive_alert_date") or "") != today:
            fields["consecutive_alert_date"] = today
            alert = True
    update_runtime(acc_dir, **fields)
    return {"consecutive_failures": n, "alert": alert, "skip_date": skip}


# ---------------- 日志 ----------------

def _current_ws_name() -> str:
    """这条日志属于哪个工作区（按请求级上下文判断；没有上下文时算 default）。

    多令牌下日志必须能分账，否则朋友的「日志」页会看到主账号的运行记录。
    """
    try:
        from . import ctx
        from .config import DATA_DIR
        root = ctx.current_root()
        if root is None:
            return "default"
        if Path(root) == Path(DATA_DIR):
            return "default"
        return Path(root).name or "default"
    except Exception:
        return "default"


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _ring.append((_current_ws_name(), self.format(record)))
        except Exception:
            pass


class AccountFilter(logging.Filter):
    def __init__(self, acc_id: str):
        super().__init__()
        self.acc_id = acc_id

    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(_tls, "account", None) == self.acc_id


def set_log_account(acc_id: str | None) -> None:
    """当前线程属于哪个账号；由运行 worker 在开始/结束时设置。"""
    _tls.account = acc_id


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("douyin-spark")
    if logger.handlers:
        install_log_redaction()
        return logger
    logger.setLevel(logging.INFO)
    fmt = RedactingFormatter("%(asctime)s [%(levelname)s] %(message)s")

    logs_dir().mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(logs_dir() / "app.log", encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(_redact_filter())
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.addFilter(_redact_filter())
    logger.addHandler(sh)

    rh = RingHandler()
    rh.setFormatter(fmt)
    rh.addFilter(_redact_filter())
    logger.addHandler(rh)
    install_log_redaction()
    return logger


def setup_account_log(acc_id: str) -> None:
    """为指定账号挂一个按账号过滤的日志文件 handler（幂等）。"""
    if acc_id in _account_handlers:
        return
    logger = logging.getLogger("douyin-spark")
    # 账号目录不存在就不建：防止"已删除的账号"被日志顺手复活成幽灵账号（2026-10-13）
    # 必须写进"当前工作区"：用与 logs_dir() 完全相同的解析方式，保证路径一致
    try:
        _acc_dir = logs_dir().parent / "accounts" / acc_id
    except Exception:
        _acc_dir = DATA_DIR / "accounts" / acc_id
    if not _acc_dir.exists():
        return
    log_path = _acc_dir / "logs" / "account.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(RedactingFormatter("%(asctime)s [%(levelname)s] %(message)s"))
        fh.addFilter(AccountFilter(acc_id))
        fh.addFilter(_redact_filter())
        logger.addHandler(fh)
        _account_handlers[acc_id] = fh
    except Exception:
        pass


def close_account_log(acc_id: str) -> None:
    """关闭并移除某账号的日志 handler（删除账号前必须调用，否则 Windows 下文件被占用删不掉）。"""
    handler = _account_handlers.pop(acc_id, None)
    if handler is None:
        return
    try:
        logging.getLogger("douyin-spark").removeHandler(handler)
        handler.close()
    except Exception:
        pass


def recent_logs(n: int = 300, ws: str = "default") -> list[str]:
    """返回某个工作区的最近 n 行日志（默认只看 default 的）。"""
    want = (ws or "default").strip() or "default"
    picked = [line for w, line in list(_ring) if w == want]
    return picked[-n:]
