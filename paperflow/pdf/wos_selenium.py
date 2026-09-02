"""Visible Selenium WOS downloader using the user's own institutional login."""

from __future__ import annotations

import os
import time
from pathlib import Path

import requests

from .wos_browser import WOS_API_URL


class WosSeleniumEngine:
    """Keep one headed Chrome session alive and let the user complete login."""

    def __init__(self, downloads_dir: Path | None = None, timeout: float = 90) -> None:
        self.downloads_dir = Path(downloads_dir or (Path.home() / "Downloads"))
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.driver = None

    @staticmethod
    def _resolve_uid(doi: str) -> str:
        key = os.getenv("WOS_API_KEY", "").strip()
        if not key:
            return ""
        try:
            r = requests.get(WOS_API_URL, headers={"X-ApiKey": key, "Accept": "application/json"},
                             params={"db": "WOS", "q": f"DO=({doi})", "limit": 1, "page": 1},
                             timeout=(6, 18))
            r.raise_for_status()
            hits = r.json().get("hits") or []
            uid = str(hits[0].get("uid") or "") if hits else ""
            return uid if uid.startswith("WOS:") else ""
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            return ""

    def _ensure_driver(self):
        if self.driver:
            return self.driver
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        options = Options()
        options.add_argument("--start-maximized")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        prefs = {"download.default_directory": str(self.downloads_dir),
                 "download.prompt_for_download": False, "plugins.always_open_pdf_externally": True}
        options.add_experimental_option("prefs", prefs)
        self.driver = webdriver.Chrome(options=options)
        self.driver.get("https://webofscience.clarivate.cn/wos/woscc/basic-search")
        return self.driver

    def _wait_login(self) -> None:
        from selenium.webdriver.common.by import By
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            text = self.driver.find_element(By.TAG_NAME, "body").text
            # WOS may display a harmless account “Sign In” link even when an
            # institution/IP session is usable.  The search box is the reliable
            # readiness signal; only block when the page is actually replaced
            # by an SSO/login screen.
            ready = self.driver.find_elements(By.CSS_SELECTOR, 'input[aria-label*="Search box"]')
            if ready or not any(x in text for x in ("Sign In", "登录")):
                return
            time.sleep(2)
        raise RuntimeError("WOS 登录超时，请在弹出的浏览器中完成机构登录")

    def fetch(self, doi: str, target: Path) -> tuple[bool, str]:
        if not doi.strip():
            return False, "WOS Selenium 下载需要 DOI"
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            driver = self._ensure_driver()
            self._wait_login()
            uid = self._resolve_uid(doi.strip())
            if not uid:
                return False, "WOS API 未找到该 DOI"
            before = {p for p in self.downloads_dir.glob("*.pdf") if p.is_file()}
            driver.get(f"https://webofscience.clarivate.cn/wos/woscc/full-record/{uid}")
            wait = WebDriverWait(driver, self.timeout)
            link = wait.until(EC.element_to_be_clickable((By.XPATH,
                "//*[self::a or self::button][contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'full text') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'publisher') ]")))
            driver.execute_script("arguments[0].click();", link)
            wait.until(lambda d: "webofscience." not in d.current_url)
            pdf = wait.until(EC.element_to_be_clickable((By.XPATH,
                "//*[self::a or self::button][contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'pdf') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download')]")))
            driver.execute_script("arguments[0].click();", pdf)
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                files = [p for p in self.downloads_dir.glob("*.pdf") if p.is_file() and p not in before and p.stat().st_size > 0]
                if files:
                    source = max(files, key=lambda p: p.stat().st_mtime)
                    if source.read_bytes()[:5] != b"%PDF-":
                        return False, "浏览器下载结果不是有效 PDF"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
                    return True, f"授权下载模式（WOS Selenium）→ {doi}"
                time.sleep(1)
            return False, "出版社页面点击后未发现 PDF 下载"
        except Exception as exc:
            return False, f"WOS Selenium 失败：{str(exc)[:180]}"

    def close(self) -> None:
        if self.driver:
            self.driver.quit()
            self.driver = None
