"""检查某个登录态文件对应的是哪个抖音账号。

用法：
    python check_account.py                                # 检查 data/state.json
    python check_account.py --state data/accounts/abc123/state.json   # 检查指定账号

原理：从登录态 cookie 里读出用户 UID，再打开该用户的主页读取昵称。
（页面结构变化时可能解析不到昵称，此时以手机扫码时登录的账号为准。）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

DEFAULT_STATE = Path(__file__).resolve().parent / "data" / "state.json"

UID_COOKIES = ("uid_tt", "passport_uid", "uid", "sid_tt")


def extract_uid(state_path: Path) -> str:
    data = json.loads(state_path.read_text(encoding="utf-8"))
    for c in data.get("cookies", []):
        if c.get("name") in UID_COOKIES and c.get("value"):
            return str(c["value"])
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description="检查登录态对应的抖音账号")
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="登录态文件路径（默认 data/state.json）")
    args = parser.parse_args()
    state_path = Path(args.state)

    if not state_path.exists():
        print(f"[错误] 未找到登录态文件: {state_path}")
        sys.exit(1)

    uid = extract_uid(state_path)
    print(f"[1/3] 从登录态 cookie 中识别到用户 UID: {uid or '未找到'}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                storage_state=str(state_path),
                viewport={"width": 1366, "height": 768},
            )
            page = context.new_page()
            url = f"https://www.douyin.com/user/{uid}" if uid else "https://www.douyin.com/user/self"
            print(f"[2/3] 正在打开个人主页: {url}")
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(12000)

            title = (page.title() or "").strip()
            print(f"[3/3] 页面标题: {title or '(空)'}")

            nickname = ""
            for sel in ("h1", "div[class*='nickname']", "span[class*='name']"):
                try:
                    el = page.locator(sel).first
                    if el.count():
                        txt = (el.inner_text() or "").strip()
                        if txt:
                            nickname = txt
                            break
                except Exception:
                    continue

            if nickname:
                print(f"检测到账号昵称: {nickname}")
                print(f"=> 确认：{state_path} 属于昵称【{nickname}】（UID {uid or '未知'}）")
            else:
                print("未能从页面解析出昵称（抖音页面结构可能已变化）。")
                print("替代确认方法：运行 extract_cookie.py 扫码时，手机抖音 App 当前登录的是哪个账号，")
                print("state.json 就是哪个账号。若你手机上有多个账号，请先切到目标账号再扫码。")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
