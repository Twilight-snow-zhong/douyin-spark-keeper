"""请求级「当前工作区」上下文。

为什么用 contextvars 而不是给每个函数加参数：
`app.py` 有 49 个接口、上百处路径引用，逐个加参数既慢又容易漏一处 → 串数据。
这里用一个请求级上下文：由 ASGI 中间件在请求开头按令牌设置好，
之后所有解析路径的辅助函数（accounts_dir / logs_dir / global_config_path …）自动生效。

铁律：**未设置时一律回落到老路径（data/）**，所以 v0.6.6 的行为完全不变。

实现注意：值必须在**异步上下文**（中间件）里设置。线程池 worker 只会拿到
设置那一刻的上下文快照 —— 对每个请求而言正好正确，不会互相串。
"""

from __future__ import annotations

import contextvars
from pathlib import Path

_current_root: contextvars.ContextVar = contextvars.ContextVar("ws_data_root", default=None)


def set_root(root: Path | None):
    """设置当前请求的工作区数据根目录，返回可用于 reset 的 token。"""
    return _current_root.set(root)


def reset_root(token) -> None:
    try:
        _current_root.reset(token)
    except Exception:
        pass


def current_root() -> Path | None:
    return _current_root.get()