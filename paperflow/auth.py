"""浏览器授权模块：弹出浏览器 → 用户登录 → 捕获登录态（cookie + localStorage）→ 本地持久化。

设计目标（可分发）：
- 不依赖 Kimi WebBridge 等外部扩展，只用 Playwright 驱动的本地浏览器
- 两种模式：
  * auto   —— 无头自动化（如 Sci-Hub 的 DDoS-Guard 挑战页自动等待）
  * manual —— 弹出可见浏览器窗口，用户手动登录（WOS/CNKI/出版社订阅），登录后回车确认
- 登录态按站点保存在 sessions/<site>.json，供 PdfEngine/Publisher 复用
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SESSIONS_DIR = Path(__file__).resolve().parent.parent / "sessions"

# 站点定义：url=登录起始页；hint=登录成功的 URL 特征（可为空）
AUTH_SITES: dict[str, dict] = {
    "scihub": {"url": "https://sci-hub.jp/10.1038/nature12373", "auto": True, "domain": "sci-hub.jp"},
    "cnki": {"url": "https://oversea.cnki.net/kns8s/defaultresult/index", "auto": False,
             "hint": "oversea.cnki.net", "domain": ".cnki.net"},
    "wos": {"url": "https://webofscience.clarivate.cn/wos/woscc/basic-search", "auto": False,
            "hint": "webofscience", "domain": "webofscience.clarivate.cn"},
    "sciencedirect": {"url": "https://www.sciencedirect.com/user/institution/login", "auto": False,
                      "hint": "sciencedirect.com", "domain": ".sciencedirect.com"},
    "springer": {"url": "https://link.springer.com", "auto": False,
                 "hint": "link.springer.com", "domain": ".springer.com"},
    "wiley": {"url": "https://onlinelibrary.wiley.com", "auto": False,
              "hint": "onlinelibrary.wiley.com", "domain": ".wiley.com"},
    "publisher": {"url": "https://www.sciencedirect.com", "auto": False,
                  "hint": "", "domain": ""},
}


def load_site_cookies(site: str) -> list[dict]:
    return load_site_session(site).get("cookies", [])


def load_site_session(site: str) -> dict:
    """读取用户主动保存的站点会话；无效或不存在时返回空字典。"""
    path = SESSIONS_DIR / f"{site}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("cookies"), list):
            return data
        if isinstance(data, list):
            return {"cookies": data, "storage": {}, "host": ""}
        return {}
    except Exception:
        return {}


def save_site_cookies(site: str, cookies: list[dict]) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    (SESSIONS_DIR / f"{site}.json").write_text(
        json.dumps(cookies, indent=1, ensure_ascii=False), encoding="utf-8")


def save_site_session(site: str, cookies: list[dict], storage: dict | None = None,
                      host: str = "") -> None:
    """保存完整会话：cookie + localStorage（SPA 站点登录态在 localStorage）。"""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    data = {"cookies": cookies, "storage": storage or {}, "host": host}
    (SESSIONS_DIR / f"{site}.json").write_text(
        json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def apply_cookies(session, cookies: list[dict]) -> None:
    for c in cookies:
        try:
            session.cookies.set(c["name"], c["value"], domain=c["domain"].lstrip("."),
                                path=c.get("path", "/"))
        except Exception:
            continue


def _launch_browser(headful: bool):
    """启动真实指纹浏览器：优先系统 Chrome（channel=chrome）+ stealth 反检测。"""
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    # 尽量避开自动化特征，模拟真人
    launch_kwargs = {
        "headless": (not headful),
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run", "--no-default-browser-check",
        ],
    }
    try:
        p = sync_playwright().start()
        browser = p.chromium.launch(channel="chrome", **launch_kwargs)  # 系统 Chrome
    except Exception:
        p.stop()
        p = sync_playwright().start()
        browser = p.chromium.launch(**launch_kwargs)  # 回退 chromium
    ctx = browser.new_context(
        user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
        viewport={"width": 1440, "height": 900},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
    )
    Stealth().apply_stealth_sync(ctx)  # 注入指纹伪装脚本
    return p, browser, ctx


def login(site: str, headful: bool = True, timeout: int = 300) -> list[dict]:
    """弹出浏览器完成登录，自动检测登录完成并捕获 cookie。

    - auto 站点（scihub）：headless 等待 DDoS-Guard 自动挑战完成后抓 cookie
    - manual 站点：headful 弹出浏览器，自动轮询检测以下完成信号：
        * URL 包含站点 hint；或
        * 浏览器窗口被手动关闭；或
        * 捕捉到疑似登录态 cookie（非初始值）；或
        * 超时
    """
    from playwright.sync_api import sync_playwright

    meta = AUTH_SITES[site]
    p, browser, ctx = _launch_browser(headful)
    try:
        page = ctx.new_page()
        page.goto(meta["url"], timeout=60000, wait_until="domcontentloaded")
        if meta.get("auto"):
            import time
            for _ in range(int(timeout)):
                title = page.title() or ""
                if "DDoS" not in title and title.strip():
                    break
                time.sleep(2)
        else:
            print(f"\n已在浏览器打开: {meta['url']}", flush=True)
            print("请在弹出窗口中登录（校园账号/机构 SSO）。", flush=True)
            print("登录并回到站点正文页后，请回到终端按回车保存会话。", flush=True)
            try:
                input()
            except EOFError:
                raise RuntimeError("当前终端无法确认登录；请在交互式终端运行 auth login")
            if page.is_closed():
                raise RuntimeError("浏览器页面已关闭，无法捕获会话；请保持窗口打开并重新登录")
            print("\n正在捕获会话...", flush=True)
        cookies = [c for c in ctx.cookies() if c["name"] and c.get("value")]
        storage: dict = {}
        try:
            storage = page.evaluate(
                "() => { const o={}; for (let i=0;i<localStorage.length;i++){"
                "const k=localStorage.key(i); o[k]=localStorage.getItem(k);} return o; }")
        except Exception:
            pass
        host = re.sub(r"^https?://(www\.)?", "", meta["url"]).split("/")[0]
        save_site_session(site, cookies, storage, host)
        try:
            browser.close()
        except Exception:
            pass
        print(f"已保存 {len(cookies)} cookies + {len(storage)} localStorage -> sessions/{site}.json",
              flush=True)
        return {"cookies": cookies, "storage": storage, "host": host}
    finally:
        try:
            p.stop()
        except Exception:
            pass


def status(site: str | None = None) -> int:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    target = [site] if site else sorted(AUTH_SITES)
    for name in target:
        cookies = load_site_cookies(name)
        fresh = len(cookies)
        detail = " ".join(c["name"] for c in cookies[:6])
        print(f"{name:14s} {'✓ 已授权' if fresh else '✗ 未授权'} ({fresh} cookies) {detail}")
    return 0
