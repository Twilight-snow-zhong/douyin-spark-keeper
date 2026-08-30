"""通知：Windows 桌面系统通知 + webhook 推送（ntfy / Server酱 / PushPlus）。

- Windows 个人版：默认用系统通知气泡（零配置）。
- 服务器版（Linux 无桌面）：桌面通知不可用，改用 webhook 推送到手机。
  支持三种免费服务：
    ntfy       —— 地址形如 https://ntfy.sh/你的话题名，无需注册
    Server酱   —— 填 SendKey（sctp...），在 sct.ftqq.com 获取
    PushPlus   —— 填 token，在 pushplus.plus 获取

注意：Windows 的 Toast .Show() 在系统屏蔽（专注助手/通知权限）时也会静默返回成功，
所以另提供 check_notification_health() 读取系统状态，检测是否在屏蔽通知。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.parse
import urllib.request

logger = logging.getLogger("douyin-spark")

_IS_WIN = os.name == "nt"


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on", "是")


def check_notification_health() -> list[str]:
    """读取 Windows 通知相关设置，返回需要提醒用户的问题列表（没有则为空）。"""
    warnings: list[str] = []
    try:
        import winreg

        # 专注助手（Focus Assist）状态：0=关闭 1=仅优先 2=仅闹钟
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings",
            ) as k:
                status, _ = winreg.QueryValueEx(k, "QuietHoursStatus")
                if status == 1:
                    warnings.append("系统「专注助手」开着（仅优先），通知气泡会被屏蔽：设置 → 系统 → 专注助手 → 关闭")
                elif status == 2:
                    warnings.append("系统「专注助手」开着（仅闹钟），通知气泡会被屏蔽：设置 → 系统 → 专注助手 → 关闭")
        except Exception:
            pass

        # 系统全局通知是否被禁用
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\PushNotifications",
            ) as k:
                enabled, _ = winreg.QueryValueEx(k, "ToastEnabled")
                if enabled == 0:
                    warnings.append("系统通知总开关被关闭：设置 → 系统 → 通知 → 打开「获取来自应用和其他发送者的通知」")
        except Exception:
            pass
    except Exception:
        pass
    return warnings


def _windows_toast(title: str, message: str) -> tuple[bool, str]:
    """通过 PowerShell 调用 Windows 原生通知（Win10/11 气泡）。

    返回 (是否成功, 错误信息)；真正执行并捕获 PowerShell 的退出码与报错。
    """
    def esc(s: str) -> str:
        return str(s).replace("'", "''")

    script = (
        "$t='%s'; $m='%s'; " % (esc(title), esc(message))
        + "$ErrorActionPreference='Stop'; "
        + "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
        + "$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
        + "$textNodes = $template.GetElementsByTagName('text'); "
        + "$textNodes.Item(0).AppendChild($template.CreateTextNode($t)) > $null; "
        + "$textNodes.Item(1).AppendChild($template.CreateTextNode($m)) > $null; "
        + "$toast = [Windows.UI.Notifications.ToastNotification]::new($template); "
        + "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('DouyinSparkKeeper').Show($toast)"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode == 0:
            return True, ""
        detail = (proc.stderr or "").strip().splitlines()
        err = detail[-1] if detail else f"退出码 {proc.returncode}"
        return False, err[:150]
    except Exception as e:
        return False, str(e)[:150]


def _send_ntfy(url: str, title: str, content: str) -> None:
    req = urllib.request.Request(url, data=content.encode("utf-8"), method="POST")
    req.add_header("Title", title)
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    with urllib.request.urlopen(req, timeout=10) as r:
        if r.status >= 400:
            raise RuntimeError(f"HTTP {r.status}")


def _send_serverchan(sendkey: str, title: str, content: str) -> None:
    data = urllib.parse.urlencode({"title": title, "desp": content}).encode("utf-8")
    req = urllib.request.Request(f"https://sctapi.ftqq.com/{sendkey}.send", data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read().decode("utf-8", "replace")
        if '"code":0' not in body and r.status >= 400:
            raise RuntimeError(f"HTTP {r.status}")


def _send_pushplus(token: str, title: str, content: str) -> None:
    payload = json.dumps({"token": token, "title": title, "content": content}).encode("utf-8")
    req = urllib.request.Request("https://www.pushplus.plus/send", data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read().decode("utf-8", "replace")
        if '"code":200' not in body and r.status >= 400:
            raise RuntimeError(f"HTTP {r.status}")


def _send_webhook(notify: dict, title: str, content: str) -> str:
    """按 webhook 类型推送，返回结果描述。"""
    wtype = str(notify.get("webhook_type") or "ntfy").strip().lower()
    url = str(notify.get("webhook_url") or "").strip()
    if not url:
        return "webhook 未配置地址"
    try:
        if wtype == "serverchan":
            _send_serverchan(url, title, content)
        elif wtype == "pushplus":
            _send_pushplus(url, title, content)
        else:
            _send_ntfy(url, title, content)
        return f"webhook（{wtype}）✓"
    except Exception as e:
        return f"webhook（{wtype}）✗（{str(e)[:80]}）"


def send_notification(cfg: dict, title: str, content: str) -> list[str]:
    """按配置发送通知，返回渠道结果描述。"""
    notify = (cfg or {}).get("notify") or {}
    results: list[str] = []
    desktop = _truthy(notify.get("desktop", True))
    if _IS_WIN:
        if desktop:
            ok, err = _windows_toast(title, content)
            if ok:
                results.append("Windows 桌面通知 ✓")
            else:
                results.append(f"Windows 桌面通知 ✗（{err or '发送失败'}）")
        else:
            results.append("桌面通知未开启")
    elif desktop:
        results.append("桌面通知不可用（服务器无桌面，请配置 webhook 通知）")
    if _truthy(notify.get("webhook_enabled", False)):
        results.append(_send_webhook(notify, title, content))
    return results


def run_summary(acc_name: str, result: dict) -> tuple[str, str]:
    """把一次运行结果组织成 (标题, 内容)。"""
    ok_n = len(result.get("ok") or [])
    failed = result.get("failed") or []
    failed_n = len([f for f in failed if isinstance(f, dict) and f.get("name") != "_system"])
    dry = bool(result.get("dry_run"))
    tag = "（干跑测试）" if dry else ""

    if result.get("logged_out"):
        title = f"⚠️ 抖音续火花：{acc_name} 登录态已过期"
        content = "请在本机重新运行 python extract_cookie.py 扫码，然后在网页重新上传登录态。"
        return title, content

    if not failed:
        title = f"🔥 抖音续火花：{acc_name} 发送成功{tag}"
        content = f"已给 {ok_n} 位好友发送消息。"
    else:
        title = f"⚠️ 抖音续火花：{acc_name} 部分失败{tag}"
        lines = [f"成功 {ok_n} 人，失败 {failed_n} 人。"]
        for f in failed:
            if isinstance(f, dict):
                lines.append(f"· {f.get('name')}: {f.get('reason')}")
        content = "\n".join(lines)
        if result.get("rate_limited"):
            content += "\n疑似触发限流，本轮已停止。"
    return title, content
