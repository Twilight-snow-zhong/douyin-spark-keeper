"""检测本机已安装的 Chromium 系浏览器，供扫码登录选择。

注意：这里检测到的是真实安装的浏览器（Edge/Chrome/夸克/360/QQ 等），
Playwright 的 chromium 引擎可通过 executable_path 直接驱动它们。
系统版 Firefox 与 Playwright 协议不兼容，不在检测之列（请用内置 firefox 引擎）。
"""

from __future__ import annotations

import os
from pathlib import Path

# name -> 候选 exe 路径（按常见安装位置排列）
CANDIDATES: dict[str, list[str]] = {
    "Microsoft Edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "Google Chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "夸克浏览器": [
        r"C:\Program Files\Quark\Application\Quark.exe",
        r"C:\Program Files (x86)\Quark\Application\Quark.exe",
    ],
    "360安全浏览器": [
        r"C:\Program Files (x86)\360\360se6\Application\360se.exe",
        r"C:\Program Files\360\360se6\Application\360se.exe",
    ],
    "QQ浏览器": [
        r"C:\Program Files (x86)\Tencent\QQBrowser\QQBrowser.exe",
        r"C:\Program Files\Tencent\QQBrowser\QQBrowser.exe",
    ],
}

_local = Path(os.environ.get("LOCALAPPDATA", ""))
if _local.exists():
    CANDIDATES.setdefault("Google Chrome", []).append(str(_local / "Google/Chrome/Application/chrome.exe"))
    CANDIDATES.setdefault("夸克浏览器", []).append(str(_local / "Quark/Application/Quark.exe"))


def detect_windows_browsers() -> list[dict]:
    """返回本机已安装的 Chromium 系浏览器列表：[{name, path}, ...]。"""
    out: list[dict] = []
    for name, paths in CANDIDATES.items():
        for p in paths:
            if Path(p).exists():
                out.append({"name": name, "path": p})
                break
    return out


def looks_like_browser_path(value: str) -> bool:
    """判断引擎值是否为浏览器可执行文件路径（而非内置引擎名）。"""
    v = (value or "").strip()
    if not v:
        return False
    return ":" in v or v.startswith("/") or v.lower().endswith(".exe")


def detect_playwright_engines() -> dict[str, bool]:
    """检测 Playwright 内置浏览器引擎是否已安装（chromium/firefox/webkit）。

    注意：这是 Playwright 专用构建（与系统安装的 Firefox 等无关），
    未安装时需运行 `python -m playwright install firefox webkit`。
    同时检查：程序自带 browsers\\ 目录（PLAYWRIGHT_BROWSERS_PATH）和系统默认 ms-playwright。
    """
    def _bases() -> list[Path]:
        """浏览器可能存放的位置（顺序：自定义 → Windows → Linux → macOS）。

        曾经只查 Windows 的 LOCALAPPDATA 与自带的 browsers\，导致 **Linux 服务器上
        永远显示"全部未安装"**（其实 Chromium 装好且能用）——这里补上 Linux/macOS 默认路径。
        """
        out: list[Path] = []
        env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
        if env:
            out.append(Path(env))
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        if local_appdata:
            out.append(Path(local_appdata) / "ms-playwright")
        home = os.environ.get("HOME", "").strip()
        if not home:
            try:
                home = str(Path.home())
            except Exception:
                home = ""
        if home:
            out.append(Path(home) / ".cache" / "ms-playwright")            # Linux 默认
            out.append(Path(home) / "Library" / "Caches" / "ms-playwright")  # macOS 默认
        return out

    bases = _bases()
    out: dict[str, bool] = {}
    for eng, prefix in (("chromium", "chromium-"), ("firefox", "firefox-"), ("webkit", "webkit-")):
        found = False
        for base in bases:
            if base.exists():
                try:
                    if any(p.is_dir() and p.name.startswith(prefix) for p in base.iterdir()):
                        found = True
                        break
                except Exception:
                    continue
        out[eng] = found
    return out
