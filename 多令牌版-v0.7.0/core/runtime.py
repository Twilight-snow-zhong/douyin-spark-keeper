"""运行状态与日志。

- 账号级状态：data/accounts/<id>/runtime.json（运行结果、历史、联系人）
- 账号级日志：data/accounts/<id>/logs/account.log
- 全局日志：data/logs/app.log + 内存环形缓冲（供网页端 /api/logs 使用）
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
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


def record_run(acc_dir: Path, result: dict) -> None:
    rt = load_runtime(acc_dir)
    rt["last_run"] = result
    history = rt.get("history", [])
    history.insert(0, result)
    rt["history"] = history[:30]

    if result.get("logged_out"):
        rt["session_status"] = "expired"
    elif result.get("ok") and not result.get("failed"):
        rt["session_status"] = "ok"
    elif result.get("ok"):
        rt["session_status"] = "partial"
    elif not result.get("failed"):
        rt["session_status"] = "ok"
    else:
        rt["session_status"] = "failed"
    _save(acc_dir, rt)


def record_contacts(acc_dir: Path, data: dict) -> None:
    rt = load_runtime(acc_dir)
    rt["contacts"] = data.get("names", [])
    rt["contacts_at"] = data.get("at")
    rt["contacts_error"] = data.get("error")
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
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    logs_dir().mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(logs_dir() / "app.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    rh = RingHandler()
    rh.setFormatter(fmt)
    logger.addHandler(rh)
    return logger


def setup_account_log(acc_id: str) -> None:
    """为指定账号挂一个按账号过滤的日志文件 handler（幂等）。"""
    if acc_id in _account_handlers:
        return
    logger = logging.getLogger("douyin-spark")
    log_path = DATA_DIR / "accounts" / acc_id / "logs" / "account.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        fh.addFilter(AccountFilter(acc_id))
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
