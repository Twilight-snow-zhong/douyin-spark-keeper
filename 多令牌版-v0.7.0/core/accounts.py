"""多账号注册表：账号目录、增删改查，以及旧版单账号数据的自动迁移。

目录结构：
    data/accounts/<id>/
        state.json       登录态（Playwright storage_state）
        config.json      账号级配置
        runtime.json     运行状态 / 历史 / 联系人
        last_error.png   出错截图
        logs/account.log 账号级日志
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
from pathlib import Path

from .config import DATA_DIR, DEFAULT_ACCOUNT_CONFIG, data_dir, save_account_config

ACCOUNTS_DIR = DATA_DIR / "accounts"                  # 老路径（兼容保留，勿直接使用）
ORDER_FILE = DATA_DIR / "accounts_order.json"         # 老路径（兼容保留，勿直接使用）
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
LEGACY_FILES = ("state.json", "config.json", "runtime.json")


def accounts_dir() -> Path:
    """当前工作区的账号目录（未设置工作区上下文时 = 老路径 data/accounts）。"""
    return data_dir() / "accounts"


def order_file() -> Path:
    """当前工作区的账号排序文件。"""
    return data_dir() / "accounts_order.json"


def _load_order() -> list[str]:
    """读取用户自定义的账号显示顺序（不存在的 ID 会被忽略）。"""
    try:
        data = json.loads(order_file().read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x) for x in data if _ID_RE.match(str(x))]
    except Exception:
        pass
    return []


def save_order(ids: list[str]) -> None:
    """保存账号显示顺序（只保留真实存在的账号 ID）。"""
    valid = [x for x in ids if _ID_RE.match(str(x)) and account_dir(str(x)).exists()]
    order_file().write_text(json.dumps(valid, ensure_ascii=False, indent=2), encoding="utf-8")


def account_dir(acc_id: str) -> Path:
    """校验账号 ID 并返回其目录（防路径穿越）。"""
    if not _ID_RE.match(acc_id or ""):
        raise ValueError("非法账号 ID")
    return accounts_dir() / acc_id


def _gen_id() -> str:
    return secrets.token_hex(4)


def _read_json(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _migrate_legacy() -> None:
    """旧版单账号数据（data/state.json 等）迁移为 accounts/default/。"""
    if accounts_dir().exists() and any(accounts_dir().iterdir()):
        return
    legacy = {f: DATA_DIR / f for f in LEGACY_FILES if (DATA_DIR / f).exists()}
    if not legacy:
        return
    acc = accounts_dir() / "default"
    acc.mkdir(parents=True, exist_ok=True)
    for f, src in legacy.items():
        shutil.move(str(src), str(acc / f))
    cfg_path = acc / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg.setdefault("name", "默认账号")
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass


def ensure_accounts() -> list[dict]:
    """确保账号体系就绪：迁移旧数据；一个账号都没有时创建默认账号。"""
    accounts_dir().mkdir(parents=True, exist_ok=True)
    _migrate_legacy()
    accs = list_accounts()
    if not accs:
        create_account("默认账号")
        accs = list_accounts()
    return accs


def list_accounts() -> list[dict]:
    if not accounts_dir().exists():
        return []
    out: list[dict] = []
    for d in sorted(accounts_dir().iterdir()):
        if not d.is_dir() or not _ID_RE.match(d.name):
            continue
        cfg = _read_json(d / "config.json")
        rt = _read_json(d / "runtime.json")
        last_run = rt.get("last_run")
        out.append({
            "id": d.name,
            "name": (cfg.get("name") or d.name).strip() or d.name,
            "enabled": str(cfg.get("enabled", True)).lower() in ("1", "true", "yes", "on"),
            "browser": cfg.get("browser", "chromium"),
            "state_exists": (d / "state.json").exists(),
            "session_status": rt.get("session_status", "unknown"),
            "running": bool(rt.get("running", False)),
            "last_run_at": last_run.get("at") if isinstance(last_run, dict) else None,
        })
    # 按用户自定义顺序排序：排过的在前，没排过的按名字跟在后面
    order = _load_order()
    rank = {acc_id: i for i, acc_id in enumerate(order)}
    missing = sorted((a for a in out if a["id"] not in rank), key=lambda a: a["name"])
    ranked = sorted((a for a in out if a["id"] in rank), key=lambda a: rank[a["id"]])
    return ranked + missing


def create_account(name: str = "新账号") -> str:
    acc_id = _gen_id()
    d = account_dir(acc_id)
    d.mkdir(parents=True, exist_ok=True)
    save_account_config(d, {"name": (name or "新账号").strip() or "新账号"})
    return acc_id


def rename_account(acc_id: str, name: str) -> dict:
    d = account_dir(acc_id)
    if not d.exists():
        raise ValueError("账号不存在")
    name = (name or "").strip()
    if not name:
        raise ValueError("账号名不能为空")
    return save_account_config(d, {"name": name})


def delete_account(acc_id: str) -> None:
    if len(list_accounts()) <= 1:
        raise ValueError("至少保留一个账号，无法删除")
    d = account_dir(acc_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
