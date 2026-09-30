"""配置读写。

- 全局配置：data/config.json（通知渠道等）
- 账号级配置：data/accounts/<id>/config.json（好友、消息、定时、浏览器等）
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path


def _resolve_base_dir() -> Path:
    """解析程序基准目录（开发运行 / PyInstaller 打包运行通用）。

    - 开发运行：项目根目录（本文件的上一级）
    - 打包运行：exe 所在目录（数据、令牌、日志、browsers 全部在 exe 旁，拷走即用）
    - 单文件 exe 版（exe 名带 onefile）：再收进 exe 旁的 DouyinSparkData 子目录，
      避免自解压临时目录与用户文件混在一起
    同步代码到打包工作区后无需再手工补补丁，两边行为一致。
    """
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent.parent
    base = Path(sys.executable).resolve().parent
    if "onefile" in Path(sys.executable).name.lower():
        base = base / "DouyinSparkData"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return base


BASE_DIR = _resolve_base_dir()
DATA_DIR = BASE_DIR / "data"
GLOBAL_CONFIG_PATH = DATA_DIR / "config.json"   # 老路径（兼容保留）

def data_dir() -> Path:
    """当前工作区的数据根目录。

    未设置工作区上下文时 → 老路径 DATA_DIR（v0.6.6 行为不变）。
    """
    from . import ctx
    return ctx.current_root() or DATA_DIR


def global_config_path() -> Path:
    return data_dir() / "config.json"

_lock = threading.Lock()

DEFAULT_GLOBAL_CONFIG = {
    "notify": {
        "desktop": True,           # Windows 桌面系统通知（右下角气泡）
        "webhook_enabled": False,  # 是否启用 webhook 推送（ntfy / Server酱 / PushPlus）
        "webhook_type": "ntfy",    # ntfy | serverchan | pushplus
        "webhook_url": "",         # ntfy 填完整地址；Server酱 填 SendKey；PushPlus 填 token
    },
}

# 认得的 webhook 渠道；配置里出现其它值（旧版渠道）时回退到 ntfy
_WEBHOOK_TYPES = ("ntfy", "serverchan", "pushplus")

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


def _norm_notify(raw) -> dict:
    """规范化 notify 配置：保留认得的字段，丢弃旧版/未知渠道。

    注意：这里必须把 webhook_* 三项一起带走 —— 曾经只搬 desktop，
    导致界面里配好的 webhook 一读一写就被清空（通知永远发不出去）。
    """
    out = dict(DEFAULT_GLOBAL_CONFIG["notify"])
    if isinstance(raw, dict):
        out["desktop"] = _truthy(raw.get("desktop", True))
        out["webhook_enabled"] = _truthy(raw.get("webhook_enabled", False))
        wtype = str(raw.get("webhook_type") or out["webhook_type"]).strip().lower()
        out["webhook_type"] = wtype if wtype in _WEBHOOK_TYPES else "ntfy"
        out["webhook_url"] = str(raw.get("webhook_url") or "").strip()
    return out


# ---------------- 全局配置 ----------------

def load_global_config() -> dict:
    cfg = _read_json(global_config_path(), DEFAULT_GLOBAL_CONFIG)
    merged = dict(DEFAULT_GLOBAL_CONFIG)
    merged.update(cfg)
    merged["notify"] = _norm_notify(merged.get("notify"))
    return merged


def save_global_config(cfg: dict | None) -> dict:
    merged = dict(DEFAULT_GLOBAL_CONFIG)
    if isinstance(cfg, dict):
        merged["notify"] = _norm_notify(cfg.get("notify"))
    with _lock:
        root = data_dir()                       # 按当前工作区落盘（未设置上下文时 = 老路径）
        root.mkdir(parents=True, exist_ok=True)
        global_config_path().write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
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
                "rekindled": _truthy(x.get("rekindled", False)),
            })
        else:
            name = str(x).strip()
            if name:
                out.append({"name": name, "enabled": True, "streak": "", "note": "", "rekindled": False})
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
