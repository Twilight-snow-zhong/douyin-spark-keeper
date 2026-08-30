"""Windows 开机自启动：通过在「启动」文件夹里放一个 VBS 启动器实现。

VBS 启动项目根目录的 start.bat，Windows 登录时自动运行。
支持两种窗口模式：最小化（窗口样式 7）或正常窗口（窗口样式 1）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
VBS_NAME = "DouyinSparkKeeper.vbs"
# WshShell.Run 窗口样式：7=最小化 1=正常
STYLE_MINIMIZED = "7"
STYLE_NORMAL = "1"


def _startup_dir() -> Path:
    # 测试时可设置 DOUYIN_STARTUP_DIR 覆盖真实启动目录（避免测试污染用户系统）
    override = os.environ.get("DOUYIN_STARTUP_DIR", "").strip()
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _vbs_path() -> Path:
    return _startup_dir() / VBS_NAME


def _build_content(start_bat: Path, minimized: bool) -> str:
    style = STYLE_MINIMIZED if minimized else STYLE_NORMAL
    # VBS 内容保持纯 ASCII，避免编码问题
    return (
        'Set WshShell = CreateObject("WScript.Shell")\r\n'
        'WshShell.Run """%s""", %s, False\r\n' % (str(start_bat), style)
    )


def is_enabled() -> bool:
    """是否已启用开机自启动。"""
    return _vbs_path().exists()


def get_mode() -> bool | None:
    """读取当前自启动的窗口模式：True=最小化，False=正常窗口；未启用返回 None。"""
    p = _vbs_path()
    if not p.exists():
        return None
    try:
        content = p.read_text(encoding="ascii", errors="ignore")
        m = re.search(r'WshShell\.Run """[^"]+""",\s*(\d+),', content)
        if m:
            return m.group(1) != STYLE_NORMAL  # 不是正常窗口则视为最小化
    except Exception:
        pass
    return None


def set_enabled(flag: bool, minimized: bool = True) -> bool:
    """开启/关闭开机自启动。返回是否成功。"""
    p = _vbs_path()
    if not flag:
        if p.exists():
            try:
                p.unlink()
            except Exception:
                return False
        return True
    start_bat = BASE_DIR / "start.bat"
    if not start_bat.exists():
        return False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_build_content(start_bat, minimized), encoding="ascii")
        return True
    except Exception:
        return False
