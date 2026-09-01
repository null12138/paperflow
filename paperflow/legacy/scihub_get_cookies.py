#!/usr/bin/env python3
"""用 Playwright（无头 Chromium）获取 sci-hub.jp 的 DDoS-Guard 放行 Cookie。"""

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

COOKIE_FILE = str(Path(__file__).resolve().parents[2] / "scihub_cookies.json")
TARGET = "10.1038/nature12373"


def fetch_cookies(proxy: str = "http://127.0.0.1:7890") -> list[dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, proxy={"server": proxy})
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        )
        page = ctx.new_page()
        page.goto(f"https://sci-hub.jp/{TARGET}", timeout=45000, wait_until="domcontentloaded")
        for _ in range(25):  # 等 DDoS-Guard 自动挑战完成
            time.sleep(2)
            title = page.title() or ""
            if "DDoS" not in title and title.strip():
                break
        cookies = [c for c in ctx.cookies() if "sci-hub.jp" in c["domain"]]
        browser.close()
        return cookies


def main() -> int:
    cookies = fetch_cookies()
    with open(COOKIE_FILE, "w") as f:
        json.dump(cookies, f, indent=1)
    print(f"保存 {len(cookies)} 个 cookie -> {COOKIE_FILE}")
    print("names:", [c["name"] for c in cookies])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
