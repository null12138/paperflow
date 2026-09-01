"""常驻会话浏览器：登录后不关闭，所有付费墙下载经同一浏览器（含 localStorage 登录态）。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from . import auth

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


class SessionBrowser:
    """启动一个带站点登录态的浏览器，提供 authorized_get() 供下载使用。"""

    def __init__(self, sites: list[str] | None = None) -> None:
        self.sites = sites or ["publisher", "sciencedirect", "springer", "wiley", "oxford"]
        self._p = None
        self._browser = None
        self._ctx = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._p = sync_playwright().start()
        self._browser = self._p.chromium.launch(
            channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
        )
        self._ctx = self._browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900})
        Stealth().apply_stealth_sync(self._ctx)
        # 装载各站点 cookie + localStorage
        for site in self.sites:
            data = self._load_site(site)
            if data.get("cookies"):
                self._ctx.add_cookies(data["cookies"])
            if data.get("storage"):
                try:
                    page = self._ctx.new_page()
                    page.goto(f"https://{data['host']}/", timeout=30000, wait_until="domcontentloaded")
                    page.evaluate(
                        "ls => { for (const [k,v] of Object.entries(ls)) localStorage.setItem(k,v); }",
                        data["storage"])
                    page.close()
                except Exception:
                    pass

    def _load_site(self, site: str) -> dict:
        path = auth.SESSIONS_DIR / f"{site}.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def authorized_get(self, url: str, timeout: float = 40) -> tuple[int, bytes, str]:
        with self._lock:
            resp = self._ctx.request.get(url, timeout=timeout)
            return resp.status, resp.body(), resp.headers.get("Content-Type", "")

    def page_download(self, url: str, target: Path, timeout: float = 60) -> bool:
        """用真实页面导航下载（对 JS 加密/重定向型 PDF 端点更稳）。"""
        with self._lock:
            page = self._ctx.new_page()
            try:
                with page.expect_download(timeout=timeout) as dl_info:
                    try:
                        page.goto(url, timeout=timeout)
                    except Exception:
                        pass
                dl = dl_info.value
                target.write_bytes(dl.path().read_bytes() if dl.path() else dl.url().encode())
                return target.read_bytes()[:5] == b"%PDF-"
            except Exception:
                try:
                    page.goto(url, timeout=timeout, wait_until="load")
                    target.write_bytes(page.content().encode())
                except Exception:
                    pass
                return False
            finally:
                try:
                    page.close()
                except Exception:
                    pass

    def stop(self) -> None:
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._p:
                self._p.stop()
        except Exception:
            pass