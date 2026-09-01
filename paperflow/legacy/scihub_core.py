#!/usr/bin/env python3
"""altcha proof-of-work 求解器 + Sci-Hub 页面解析（支持验证码 & 直链 & 广告假冒站检测）。"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

import requests
from bs4 import BeautifulSoup

# legacy 默认只尝试本机 Clash 代理
DEFAULT_PROXIES = [
    "http://127.0.0.1:7890",
]

TIMEOUT = 12


def build_session(proxy: str | None = None) -> requests.Session:
    """创建会话并强制走给定代理（requests 在 macOS 上默认读系统代理）。"""
    session = requests.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def solve_challenge(salt: str, target: str, max_number: int) -> int | None:
    """altcha 实际算法：找 number 使 sha256(salt+number) 的 hex == target（完整哈希匹配，非前导零）。"""
    for number in range(max_number + 1):
        if sha256_hex(f"{salt}{number}".encode("utf-8")) == target:
            return number
    return None


class SciHubPage:
    kind: str            # pdf | framepdf | article | captcha | cloudflare | fake | notfound | error
    pdf_url: str = ""
    notice: str = ""

    def __init__(self, kind: str, pdf_url: str = "", notice: str = "") -> None:
        self.kind = kind
        self.pdf_url = pdf_url
        self.notice = notice


def is_fake_mirror(status: int, text: str) -> bool:
    """检测广告/停靠伪装站（.de/.cc/.it/.nl/.pl/.si 那一类）。"""
    low = text[:8000].lower()
    markers = ["ad-overlay", "google-ad-bottom", "prebid-wrapper", "dfp-ad-container"]
    return any(m in low for m in markers) and "<title>redirecting..." in low


def parse_scihub_page(session: requests.Session, url: str) -> SciHubPage:
    """GET url 并按响应类型分类。"""
    try:
        r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException as exc:
        return SciHubPage("error", notice=f"{type(exc).__name__}: {str(exc)[:160]}")

    if r.content[:5] == b"%PDF-":
        return SciHubPage("pdf", pdf_url=str(r.url))
    low = r.text[:12000].lower()
    if is_fake_mirror(r.status_code, r.text):
        return SciHubPage("fake", notice="广告跳转伪装站")
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
    obj = soup.find("object")
    if obj and obj.attrs.get("data"):
        return SciHubPage("framepdf", pdf_url=str(obj.attrs["data"]))
    for script in soup.find_all("script"):
        st = script.string or ""
        for mm in re.finditer(r"['\"](https?://[^'\"]+\.pdf[^'\"]*)['\"]", st):
            return SciHubPage("framepdf", pdf_url=mm.group(1))
    if r.status_code == 403 or "challenge-platform" in r.text or "cf-chl-" in r.text.lower():
        return SciHubPage("cloudflare", notice=f"HTTP {r.status_code}")
    if "altcha" in r.text and "challengeurl" in r.text:
        return SciHubPage("captcha")

    txt = re.sub(r"<[^>]+>", " ", r.text)
    txt = re.sub(r"\s+", " ", txt).strip()
    if any(k in txt[:400] for k in ("未找到", "не найдена", "not found", "article not found",
                                      "見つかりません", "存在しません", "見つからなかった", "존재하지 않습니다", "논문을 찾을 수 없")):
        return SciHubPage("notfound", notice="未收录该 DOI")
    return SciHubPage("article", notice=f"HTTP {r.status_code}, 无法识别: {txt[:100]}")


def solve_captcha(session: requests.Session, page_url: str) -> SciHubPage | None:
    """处理 altcha 验证码: GET 页面→取 challenge→求解→POST solution→重取页面。"""
    try:
        r = session.get(page_url, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return SciHubPage("error", notice=f"GET 页失败: {str(exc)[:160]}")
    page = r.text
    m = re.search(r'challengeurl\s*=\s*"([^"]+)"', page)
    m2 = re.search(r"/captcha/solution/(\d+)", page)
    if not m or not m2:
        return None
    base = re.match(r"^(https?://[^/]+)", page_url).group(1)
    try:
        cr = session.get(base + m.group(1), timeout=20)
        cj: dict[str, Any] = cr.json()
    except Exception as exc:
        return SciHubPage("error", notice=f"取 challenge 失败: {str(exc)[:120]}")
    number = solve_challenge(cj.get("salt", ""), cj.get("challenge", ""), cj.get("maxNumber", 100000))
    if number is None:
        return SciHubPage("error", notice="求解失败")
    payload = base64.b64encode(json.dumps({
        "algorithm": cj["algorithm"], "challenge": cj["challenge"],
        "number": number, "salt": cj["salt"], "signature": cj["signature"],
        "took": 1,
    }).encode()).decode("ascii")
    try:
        pr = session.post(
            f"{base}/captcha/solution/{m2.group(1)}",
            json={"captcha": payload}, headers={"Content-Type": "application/json"}, timeout=30,
        )
        ct = pr.headers.get("Content-Type", "")
        try:
            data = pr.json() if ct.startswith("application/json") else {}
        except Exception:
            data = {}
        if not data.get("success"):
            return SciHubPage("error", notice=f"验证未通过: {pr.status_code} {str(data)[:120]} {pr.text[:80]}")
    except Exception as exc:
        return SciHubPage("error", notice=f"提交失败: {str(exc)[:120]}")
    # 验证通过后重取文章页（此时应返回论文页或 PDF）
    return parse_scihub_page(session, page_url)
