"""工作区（多令牌）支持：令牌表 + 数据路径解析。

设计见同目录《多令牌改造设计.md》，两条铁律：

1. **零迁移**：默认工作区（default）沿用现有的 `data/accounts`、`data/config.json`、
   `data/emojis`、`data/logs` 路径 —— 老部署换上新代码后，数据一个字节都不用动。
2. **纯增量**：`data/tokens.json` 不存在时，行为与 v0.6.6 完全一致
   （`.env` 里的 `AUTH_TOKEN` 就是默认工作区的令牌）。
   本模块只提供能力，不改动任何既有调用路径，等 P2 再逐步接入 app.py。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

from . import config as _config

DEFAULT_WS = "default"
WS_ROOT_NAME = "ws"                    # data/ws/<id>/...
WS_ID_RE = re.compile(r"^ws_[a-z0-9]{6,32}$")
_NAME_MAX = 32

# 令牌会放进 HTTP 请求头，而 HTTP 头只能是 latin-1：
# **必须限制为可见 ASCII**，否则像中文这样字符会直接报 'latin-1' codec can't encode
# （和 ntfy 中文标题踩的是同一个坑）。8~64 位、不含空格。
TOKEN_RE = re.compile(r"^[\x21-\x7e]{8,64}$")

_lock = threading.Lock()


# ---------------------------------------------------------------- 基础路径

def tokens_path() -> Path:
    return _config.DATA_DIR / "tokens.json"


def ws_base_dir() -> Path:
    """非默认工作区的存放目录：data/ws"""
    return _config.DATA_DIR / WS_ROOT_NAME


def ws_paths(ws: str = DEFAULT_WS) -> dict:
    """返回某个工作区的各类路径。

    默认工作区**必须**映射到老路径（回归关键点）：
        root    -> data/
        accounts-> data/accounts
        config  -> data/config.json
        emojis  -> data/emojis
        logs    -> data/logs
    其它工作区：
        root    -> data/ws/<id>/
        ...（同上相对结构）
    """
    data = _config.DATA_DIR
    if ws == DEFAULT_WS:
        root = data
    else:
        root = data / WS_ROOT_NAME / ws
    return {
        "ws": ws,
        "root": root,
        "accounts": root / "accounts",
        "config": root / "config.json",
        "emojis": root / "emojis",
        "logs": root / "logs",
    }


def ensure_ws_dirs(ws: str) -> dict:
    paths = ws_paths(ws)
    for key in ("root", "accounts", "emojis", "logs"):
        try:
            paths[key].mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    return paths


# ---------------------------------------------------------------- 令牌表

def _read_env_token() -> str:
    """读 .env 里的 AUTH_TOKEN（老部署的唯一令牌）。"""
    env = _config.BASE_DIR / ".env"
    if not env.exists():
        return ""
    try:
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("AUTH_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _read_tokens() -> dict:
    p = tokens_path()
    if not p.exists():
        return {"version": 1, "tokens": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("tokens"), dict):
            data.setdefault("version", 1)
            return data
    except Exception:
        pass
    return {"version": 1, "tokens": {}}


def _write_tokens(data: dict) -> None:
    p = tokens_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)                      # 原子替换，避免半截文件
    try:
        os.chmod(p, 0o600)                  # 与 .env 同级敏感度
    except Exception:
        pass


def has_token_table() -> bool:
    return tokens_path().exists()


def _seed_default(data: dict) -> dict:
    """确保老令牌被登记为默认工作区（首次建表时自动迁入，老令牌继续可用）。"""
    tokens = data.setdefault("tokens", {})
    if not any(v.get("ws") == DEFAULT_WS for v in tokens.values() if isinstance(v, dict)):
        legacy = _read_env_token()
        if legacy:
            tokens[legacy] = {"name": "我（默认）", "ws": DEFAULT_WS,
                              "created": _now(), "legacy": True}
    return data


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------- 对外 API

def resolve_token(token: str) -> str | None:
    """令牌 → 工作区 id；不认识返回 None。

    * 有 tokens.json：查表
    * 没有 tokens.json（老部署）：只认 .env 里的令牌 → default
    * 兜底：任何情况下，.env 里的令牌永远能用（防止建表时漏掉把自己锁在门外）
    """
    token = (token or "").strip()
    if not token:
        return None
    data = _read_tokens()
    info = data.get("tokens", {}).get(token)
    if isinstance(info, dict):
        ws = str(info.get("ws") or DEFAULT_WS)
        return ws if ws == DEFAULT_WS or WS_ID_RE.match(ws) else None
    legacy = _read_env_token()
    if legacy and token == legacy:
        return DEFAULT_WS
    return None


def current_ws() -> str:
    """从请求/任务上下文推断"现在是哪个工作区"（没有上下文 → default）。

    调度器与接口都用它来决定该读写谁的数据，避免到处传参。
    """
    try:
        from . import ctx
        root = ctx.current_root()
    except Exception:
        return DEFAULT_WS
    if root is None:
        return DEFAULT_WS
    try:
        if Path(root) == _config.DATA_DIR:
            return DEFAULT_WS
    except Exception:
        pass
    return Path(root).name or DEFAULT_WS


def token_hint(token: str) -> str:
    """只用于界面辨认：…abcd"""
    token = (token or "").strip()
    return ("…" + token[-4:]) if len(token) >= 8 else "…"


def list_tokens() -> list[dict]:
    data = _read_tokens()
    out = []
    for token, info in (data.get("tokens") or {}).items():
        if not isinstance(info, dict):
            continue
        out.append({
            "hint": token_hint(token),
            "name": str(info.get("name") or ""),
            "ws": str(info.get("ws") or DEFAULT_WS),
            "created": str(info.get("created") or ""),
            "legacy": bool(info.get("legacy")),
            "token": token,          # 仅本机管理接口使用；对外接口请只给 hint
        })
    out.sort(key=lambda x: (x["ws"] != DEFAULT_WS, x["created"]))
    return out


def list_workspaces() -> list[dict]:
    """按令牌表汇总出工作区列表（含账号数，账号数由调用方补充或这里直接算）。"""
    seen: dict[str, dict] = {}
    for item in list_tokens():
        ws = item["ws"]
        info = seen.setdefault(ws, {"ws": ws, "names": [], "tokens": 0, "created": item["created"]})
        info["tokens"] += 1
        if item["name"]:
            info["names"].append(item["name"])
        if item["created"] and (not info["created"] or item["created"] < info["created"]):
            info["created"] = item["created"]
    if not seen:                                  # 老部署：至少有一个默认工作区
        seen[DEFAULT_WS] = {"ws": DEFAULT_WS, "names": ["我（默认）"], "tokens": 1, "created": ""}
    out = []
    for ws, info in seen.items():
        paths = ws_paths(ws)
        accs = 0
        try:
            if paths["accounts"].is_dir():
                accs = sum(1 for d in paths["accounts"].iterdir() if d.is_dir())
        except Exception:
            pass
        out.append({
            "ws": ws,
            "name": info["names"][0] if info["names"] else ws,
            "is_default": ws == DEFAULT_WS,
            "tokens": info["tokens"],
            "accounts": accs,
            "created": info["created"],
            "exists": ws == DEFAULT_WS or paths["root"].exists(),
        })
    out.sort(key=lambda x: (not x["is_default"], x["created"]))
    return out


def create_workspace(name: str) -> tuple[str, str]:
    """新建工作区，返回 (ws_id, 一次性明文令牌)。"""
    clean = (name or "").strip()[:_NAME_MAX] or "新工作区"
    with _lock:
        data = _seed_default(_read_tokens())
        ws = "ws_" + secrets.token_hex(4)
        while ws in {v.get("ws") for v in data["tokens"].values() if isinstance(v, dict)}:
            ws = "ws_" + secrets.token_hex(4)
        token = secrets.token_hex(16)
        data["tokens"][token] = {"name": clean, "ws": ws, "created": _now()}
        _write_tokens(data)
    ensure_ws_dirs(ws)
    return ws, token


def rotate_token(ws: str, name: str = "") -> str:
    """给某个工作区换一把新令牌（旧令牌立即失效），返回新明文令牌。"""
    with _lock:
        data = _seed_default(_read_tokens())
        tokens = data["tokens"]
        keep_name = name
        for token, info in list(tokens.items()):
            if isinstance(info, dict) and str(info.get("ws")) == ws:
                keep_name = keep_name or str(info.get("name") or "")
                del tokens[token]
        token = secrets.token_hex(16)
        tokens[token] = {"name": keep_name or ws, "ws": ws, "created": _now()}
        _write_tokens(data)
    return token


def rename_workspace(ws: str, name: str) -> None:
    clean = (name or "").strip()[:_NAME_MAX]
    if not clean:
        raise ValueError("名称不能为空")
    with _lock:
        data = _seed_default(_read_tokens())
        for info in data["tokens"].values():
            if isinstance(info, dict) and str(info.get("ws")) == ws:
                info["name"] = clean
        _write_tokens(data)


def add_token(ws: str, name: str = "") -> str:
    """给已有工作区再加一把令牌（过渡期用）。"""
    with _lock:
        data = _seed_default(_read_tokens())
        token = secrets.token_hex(16)
        data["tokens"][token] = {"name": (name or "").strip()[:_NAME_MAX] or ws,
                                 "ws": ws, "created": _now()}
        _write_tokens(data)
    return token


def valid_token(token: str) -> bool:
    """令牌必须是 8~64 位可见 ASCII（HTTP 头只能 latin-1，中文会直接报错）。"""
    return bool(TOKEN_RE.match((token or "").strip()))


def set_token(ws: str, token: str, name: str = "") -> None:
    """把某个工作区的令牌**换成调用方指定的字符串**（移除旧令牌）。

    用于「修改访问令牌」：默认工作区改令牌时也走这里，保证令牌表与 .env 一致。
    """
    token = (token or "").strip()
    if not valid_token(token):
        raise ValueError("令牌需为 8~64 位可见 ASCII 字符（不要用中文或空格）")
    with _lock:
        data = _seed_default(_read_tokens())
        tokens = data["tokens"]
        keep = name
        for tk, info in list(tokens.items()):
            if isinstance(info, dict) and str(info.get("ws")) == ws:
                keep = keep or str(info.get("name") or "")
                del tokens[tk]
        tokens[token] = {"name": keep or ws, "ws": ws, "created": _now()}
        _write_tokens(data)


def delete_workspace(ws: str) -> Path | None:
    """删除工作区：目录**改名归档**（可反悔），令牌一并移除。default 不允许删。"""
    if ws == DEFAULT_WS:
        raise ValueError("默认工作区不能删除")
    if not WS_ID_RE.match(ws):
        raise ValueError("工作区 id 不合法")
    with _lock:
        data = _seed_default(_read_tokens())
        data["tokens"] = {t: i for t, i in data["tokens"].items()
                          if not (isinstance(i, dict) and str(i.get("ws")) == ws)}
        _write_tokens(data)
    src = ws_paths(ws)["root"]
    if not src.exists():
        return None
    dst = ws_base_dir() / ("_deleted_%s_%s" % (time.strftime("%Y%m%d-%H%M%S"), ws))
    try:
        shutil.move(str(src), str(dst))
        return dst
    except Exception:
        return None
