"""配置读写。

- 全局配置：data/config.json（通知渠道等）
- 账号级配置：data/accounts/<id>/config.json（好友、消息、定时、浏览器等）
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
GLOBAL_CONFIG_PATH = DATA_DIR / "config.json"

_lock = threading.Lock()

DEFAULT_GLOBAL_CONFIG = {
    "notify": {
        "desktop": True,  # Windows 桌面系统通知（右下角气泡）
    },
}

DEFAULT_ACCOUNT_CONFIG = {
    "name": "未命名账号",      # 显示名
    "enabled": True,          # 是否参与每日定时发送
    "schedule_time": "21:00",  # 每天发送时间 HH:MM
    "jitter_minutes": 30,     # 时间抖动窗口（分钟）
    "send_gap_min": 6,        # 相邻好友最小间隔（秒）
    "send_gap_max": 12,       # 相邻好友最大间隔（秒）
    "max_friends_per_run": 20,  # 每次最多发送人数（0 不限制）
    "retry_minutes": 45,      # 失败后自动补发等待分钟数
    "browser": "chromium",    # 浏览器引擎：chromium | firefox | webkit
    "headless": True,         # 无头模式
    "friends": [],            # [{name, enabled, streak, note}, ...]
    "messages": ["🔥 续火花", "晚安，明天见", "今天也要开心哦"],
}

_BROWSERS = ("chromium", "firefox", "webkit")


def _read_json(path: Path, default: dict) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return dict(default)


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on", "是")


# ---------------- 全局配置 ----------------

def load_global_config() -> dict:
    cfg = _read_json(GLOBAL_CONFIG_PATH, DEFAULT_GLOBAL_CONFIG)
    merged = dict(DEFAULT_GLOBAL_CONFIG)
    merged.update(cfg)
    notify = dict(DEFAULT_GLOBAL_CONFIG["notify"])  # 只保留认识的字段，旧版渠道自动丢弃
    if isinstance(merged.get("notify"), dict):
        notify["desktop"] = _truthy(merged["notify"].get("desktop", True))
    merged["notify"] = notify
    return merged


def save_global_config(cfg: dict | None) -> dict:
    merged = dict(DEFAULT_GLOBAL_CONFIG)
    if isinstance(cfg, dict):
        notify = dict(DEFAULT_GLOBAL_CONFIG["notify"])
        if isinstance(cfg.get("notify"), dict):
            notify["desktop"] = _truthy(cfg["notify"].get("desktop", True))
        merged["notify"] = notify
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        GLOBAL_CONFIG_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


# ---------------- 账号级配置 ----------------

def load_account_config(acc_dir: Path) -> dict:
    cfg = _read_json(acc_dir / "config.json", DEFAULT_ACCOUNT_CONFIG)
    merged = dict(DEFAULT_ACCOUNT_CONFIG)
    merged.update(cfg)
    merged["friends"] = _normalize_friends(merged.get("friends", []))
    merged["messages"] = [str(x) for x in merged.get("messages", []) if str(x).strip()]
    if not merged["messages"]:
        merged["messages"] = ["🔥"]
    merged["browser"] = str(merged.get("browser", "chromium")) if str(merged.get("browser", "chromium")) in _BROWSERS else "chromium"
    merged["headless"] = _truthy(merged.get("headless", True))
    merged["enabled"] = _truthy(merged.get("enabled", True))
    try:
        merged["retry_minutes"] = max(5, int(merged.get("retry_minutes", 45)))
    except (TypeError, ValueError):
        merged["retry_minutes"] = 45
    # 防御性归一化：即使配置文件被手工改坏，调度与发送也不崩溃
    schedule = str(merged.get("schedule_time", "21:00"))
    try:
        hh, mm = schedule.split(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise ValueError
        merged["schedule_time"] = f"{int(hh):02d}:{int(mm):02d}"
    except Exception:
        merged["schedule_time"] = "21:00"
    for key in ("jitter_minutes", "send_gap_min", "send_gap_max", "max_friends_per_run"):
        try:
            # 不能用 `or 默认值`：jitter=0 / max_friends=0 是合法值，0 or 30 会错误地变成 30
            merged[key] = max(0, int(merged.get(key, DEFAULT_ACCOUNT_CONFIG[key])))
        except (TypeError, ValueError):
            merged[key] = DEFAULT_ACCOUNT_CONFIG[key]
    if merged["send_gap_max"] < merged["send_gap_min"]:
        merged["send_gap_max"] = merged["send_gap_min"]
    return merged


def _normalize_friends(raw) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for x in raw:
        if isinstance(x, dict):
            name = str(x.get("name", "")).strip()
            if not name:
                continue
            out.append({
                "name": name,
                "enabled": _truthy(x.get("enabled", True)),
                "streak": str(x.get("streak", "") or "").strip(),
                "note": str(x.get("note", "") or "").strip(),
            })
        else:
            name = str(x).strip()
            if name:
                out.append({"name": name, "enabled": True, "streak": "", "note": ""})
    return out


def save_account_config(acc_dir: Path, cfg: dict | None) -> dict:
    # 先合并现有配置，避免部分更新（如只改定时）时把 name 等字段重置
    merged = dict(DEFAULT_ACCOUNT_CONFIG)
    merged.update(_read_json(acc_dir / "config.json", DEFAULT_ACCOUNT_CONFIG))
    if isinstance(cfg, dict):
        merged.update(cfg)

    if isinstance(merged.get("name"), str) and merged["name"].strip():
        merged["name"] = merged["name"].strip()
    else:
        merged["name"] = DEFAULT_ACCOUNT_CONFIG["name"]

    merged["friends"] = _normalize_friends(merged.get("friends", []))
    merged["messages"] = [str(x) for x in merged.get("messages", []) if str(x).strip()]
    if not merged["messages"]:
        merged["messages"] = ["🔥"]

    schedule = str(merged.get("schedule_time", "21:00"))
    try:
        hh, mm = schedule.split(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise ValueError
        merged["schedule_time"] = f"{int(hh):02d}:{int(mm):02d}"
    except Exception:
        raise ValueError("schedule_time 必须是 HH:MM 格式")

    for key in ("jitter_minutes", "send_gap_min", "send_gap_max", "max_friends_per_run", "retry_minutes"):
        try:
            merged[key] = max(0, int(merged.get(key, DEFAULT_ACCOUNT_CONFIG[key])))
        except (TypeError, ValueError):
            raise ValueError(f"{key} 必须是整数")
    if merged["send_gap_max"] < merged["send_gap_min"]:
        merged["send_gap_max"] = merged["send_gap_min"]
    merged["retry_minutes"] = max(5, merged["retry_minutes"])

    browser = str(merged.get("browser", "chromium"))
    merged["browser"] = browser if browser in _BROWSERS else "chromium"
    merged["headless"] = _truthy(merged.get("headless", True))
    merged["enabled"] = _truthy(merged.get("enabled", True))

    with _lock:
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "config.json").write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return merged
