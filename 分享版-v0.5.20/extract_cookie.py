"""在本地电脑（有界面的 Windows/macOS）运行：打开浏览器扫码登录抖音，导出登录态。

用法：
    pip install -r requirements.txt
    python -m playwright install chromium
    python extract_cookie.py                 # 导出到 data/state.json（默认暂存位置）
    python extract_cookie.py --out "data/accounts/<账号ID>/state.json"   # 直接导出到某账号

网页端也可以「概览 -> 上传登录态」上传默认位置的 state.json。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

DEFAULT_OUT = Path(__file__).resolve().parent / "data" / "state.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="扫码导出抖音登录态")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="导出路径（默认 data/state.json）")
    args = parser.parse_args()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print("正在打开浏览器，请在弹出的窗口里用手机抖音 App 扫码登录…")
    print("（5 分钟内检测到登录会自动保存并关闭浏览器）")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)

        deadline = time.time() + 300
        while time.time() < deadline:
            cookies = context.cookies()
            if any(c["name"].startswith("sessionid") for c in cookies):
                time.sleep(2)
                context.storage_state(path=str(out_path))
                print(f"\n登录态已保存到: {out_path}")
                browser.close()
                return
            time.sleep(2)

        print("\n超时：5 分钟内未完成扫码登录，请重新运行。")
        browser.close()
        sys.exit(1)


if __name__ == "__main__":
    main()
