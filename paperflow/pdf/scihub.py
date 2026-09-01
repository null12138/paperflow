"""Sci-Hub 下载引擎：altcha proof-of-work 自动求解 + DDoS-Guard cookie 复用/刷新。"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from .. import net

TIMEOUT = 12

# 优先镜像；其余为备选
MIRRORS = [
    "https://sci-hub.jp/",
    "https://sci-hub.su/",
    "https://sci-hub.ru/",
]
BLACKLISTED = {"sci-hub.de", "sci-hub.cc", "sci-hub.it", "sci-hub.nl", "sci-hub.pl",
               "sci-hub.si", "sci.hubg.org"}


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def solve_challenge(salt: str, target: str, max_number: int) -> int | None:
    for number in range(max_number + 1):
        if _sha256_hex(f"{salt}{number}".encode("utf-8")) == target:
            return number
    return None


class SciHubPage:
    def __init__(self, kind: str, pdf_url: str = "", notice: str = "") -> None:
        self.kind = kind
        self.pdf_url = pdf_url
        self.notice = notice


def _is_fake(text: str) -> bool:
    low = text[:8000].lower()
    return any(m in low for m in ("ad-overlay", "google-ad-bottom", "prebid-wrapper", "dfp-ad-container")) \
        and "<title>redirecting..." in low


def _parse_page(session: requests.Session, url: str) -> SciHubPage:
    try:
        r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException as exc:
        return SciHubPage("error", notice=f"{type(exc).__name__}: {str(exc)[:140]}")
    if r.content[:5] == b"%PDF-":
        return SciHubPage("pdf", pdf_url=str(r.url))
    if _is_fake(r.text):
        return SciHubPage("fake", notice="广告伪装站")
    soup = BeautifulSoup(r.text, "html.parser")
    meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
    if meta and meta.attrs.get("content"):
        pdf = str(meta.attrs["content"])
        if pdf.startswith("//"):
            pdf = "https:" + pdf
        if not pdf.startswith("http"):
            base_m = re.match(r"^(https?://[^/]+)", str(r.url))
            pdf = (base_m.group(1) if base_m else "https://sci-hub.jp") + pdf
        return SciHubPage("framepdf", pdf_url=pdf)
    for tag in (soup.iframe, soup.embed):
        if tag and tag.attrs.get("src"):
            src = str(tag.attrs["src"])
            if src.startswith("//"):
                src = "https:" + src
            if "accep" in src.split("?")[0].lower() or "adserver" in src.lower():
                continue
            return SciHubPage("framepdf", pdf_url=src)
    if "altcha" in r.text and "challengeurl" in r.text:
        return SciHubPage("captcha")
    low = r.text[:2000].lower()
    if "ddos-guard" in low:
        return SciHubPage("cloudflare", notice="DDoS-Guard 拦截")
    if r.status_code == 403:
        return SciHubPage("cloudflare", notice=f"HTTP {r.status_code}")
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", " ", r.text, flags=re.S)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", txt)
    if any(k in txt[:400] for k in ("未找到", "не найдена", "not found", "article not found",
                                    "見つかりません", "存在しません", "利用できません")):
        return SciHubPage("notfound", notice="Sci-Hub 未收录")
    return SciHubPage("article", notice=f"无法识别页面: {txt[:80]}")


def _solve_captcha(session: requests.Session, page_url: str) -> SciHubPage | None:
    try:
        r = session.get(page_url, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return SciHubPage("error", notice=f"GET 页失败: {str(exc)[:140]}")
    m = re.search(r'challengeurl\s*=\s*"([^"]+)"', r.text)
    m2 = re.search(r"/captcha/solution/(\d+)", r.text)
    if not m or not m2:
        return None
    base = re.match(r"^(https?://[^/]+)", page_url).group(1)
    try:
        cj = session.get(base + m.group(1), timeout=15).json()
    except Exception as exc:
        return SciHubPage("error", notice=f"challenge获取失败: {str(exc)[:100]}")
    number = solve_challenge(cj.get("salt", ""), cj.get("challenge", ""), cj.get("maxNumber", 100000))
    if number is None:
        return SciHubPage("error", notice="altcha 求解失败")
    payload = base64.b64encode(json.dumps({
        "algorithm": cj["algorithm"], "challenge": cj["challenge"], "number": number,
        "salt": cj["salt"], "signature": cj["signature"], "took": 1,
    }).encode()).decode()
    try:
        pr = session.post(f"{base}/captcha/solution/{m2.group(1)}",
                          json={"captcha": payload},
                          headers={"Content-Type": "application/json"}, timeout=20)
        data = pr.json() if pr.headers.get("Content-Type", "").startswith("application/json") else {}
        if not data.get("success"):
            return SciHubPage("error", notice=f"验证未通过: {pr.status_code}")
    except Exception as exc:
        return SciHubPage("error", notice=f"提交失败: {str(exc)[:100]}")
    return _parse_page(session, page_url)


class SciHubEngine:
    def __init__(self, proxies: list[str | None] | None = None, cookie_file: Path | None = None) -> None:
        self.proxies = proxies or net.DEFAULT_PROXIES
        self.cookie_file = cookie_file
        self._cookies: list[dict] = []
        self._load()

    def _load(self) -> None:
        # 优先 auth 登录态 sessions/scihub.json，兼容旧 scihub_cookies.json
        try:
            from ..auth import load_site_cookies
            self._cookies = load_site_cookies("scihub")
            if self._cookies:
                return
        except Exception:
            pass
        if not self.cookie_file or not self.cookie_file.exists():
            return
        try:
            self._cookies = json.loads(self.cookie_file.read_text())
        except Exception:
            self._cookies = []

    def _session(self, proxy: str | None) -> requests.Session:
        s = net.make_session(proxy)
        for c in self._cookies:
            s.cookies.set(c["name"], c["value"], domain=c["domain"].lstrip("."), path=c.get("path", "/"))
        return s

    def fetch(self, doi: str, target: Path) -> tuple[bool, str]:
        mirrors = [m for m in MIRRORS if m.split("//")[1].rstrip("/") not in BLACKLISTED]
        for proxy in self.proxies:
            session = self._session(proxy)
            for mirror in mirrors:
                url = mirror.rstrip("/") + "/" + doi.rstrip("/")
                page = _parse_page(session, url)
                if page.kind == "captcha":
                    page = _solve_captcha(session, url) or page
                if page.kind in ("pdf", "framepdf"):
                    try:
                        r = session.get(page.pdf_url, timeout=TIMEOUT + 15)
                    except requests.RequestException as exc:
                        continue
                    if r.content[:5] == b"%PDF-":
                        target.write_bytes(r.content)
                        return True, f"scihub → {r.url[:80]}"
        return False, "scihub 各镜像失败/未收录"