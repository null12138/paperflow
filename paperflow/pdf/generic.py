"""通用出版社 PDF 解析器：任何 DOI → doi.org 重定向 → 落地页 HTML 提取 PDF 链接 → 下载。

覆盖全部出版社（不依赖手工模板）；成功路径自动学习并持久化模板，
下次同前缀直接走学到的模板。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .. import net

_PATTERN_FILE = Path(__file__).resolve().parent / "learned_patterns.json"


def load_learned() -> dict:
    if _PATTERN_FILE.exists():
        try:
            return json.loads(_PATTERN_FILE.read_text())
        except Exception:
            pass
    return {}


def save_learned(data: dict) -> None:
    _PATTERN_FILE.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def _find_pdf_links(session: requests.Session, page_url: str, text: str) -> list[str]:
    """从落地页提取可能的 PDF 链接（按优先级排序）。

    The broader metadata/embedded-viewer scan is adapted from the MIT-licensed
    InstSci ``publisher_pdf_router`` (Rimagination/instsci).  It remains a
    candidate discovery step only; every candidate is still downloaded and
    accepted only when its bytes begin with ``%PDF-``.
    """
    cands: list[str] = []
    soup = BeautifulSoup(text, "html.parser")
    for meta in soup.find_all("meta"):
        key = str(meta.get("name") or meta.get("property") or meta.get("itemprop") or "").lower()
        value = str(meta.get("content") or "").strip()
        if value and ("citation_pdf_url" in key or key.endswith("pdf_url") or key == "pdf_url"):
            cands.append(value)
    for node in soup.find_all(["a", "link", "iframe", "embed", "object"]):
        target = str(node.get("href") or node.get("src") or node.get("data") or "").strip()
        if not target:
            continue
        label = " ".join(filter(None, [node.get_text(" ", strip=True), node.get("title"), node.get("aria-label")])).lower()
        low = target.lower()
        if low.endswith(".pdf") or any(x in low for x in ("/pdf", "/epdf", "/pdfdirect", "/pdfft", "download=true")) or "pdf" in label:
            cands.append(target)
        # Publishers often hide the real asset in a query parameter.
        for key, values in parse_qs(urlparse(urljoin(page_url, target)).query).items():
            if key.lower() in {"file", "pdf", "src", "url"}:
                cands.extend(v for v in values if "pdf" in v.lower())
    # Firefox/Chrome PDF.js viewers expose a defaultUrl in an inline script.
    for script in soup.find_all("script"):
        body = script.string or script.get_text(" ", strip=False)
        for match in re.finditer(r"defaultUrl['\"]?\s*,\s*['\"]([^'\"]+)", body or "", re.I):
            if "pdf" in match.group(1).lower():
                cands.append(match.group(1))
    # 去重+规范化
    out: list[str] = []
    seen = set()
    for u in cands:
        u = u.strip().lstrip("\"'")
        u = urljoin(page_url, u)
        if u not in seen and u:
            seen.add(u)
            out.append(u)
    return out


def resolve_landing(session: requests.Session, doi: str) -> tuple[str, str]:
    """GET doi.org/{doi} 跟踪重定向，返回 (最终URL, HTML)。"""
    r = session.get(f"https://doi.org/{doi}", timeout=25, allow_redirects=True)
    return r.url, r.text


def generic_fetch(session: requests.Session, doi: str) -> list[str]:
    """通用解析：返回候选 PDF URL（空列表=无法解析）。"""
    try:
        final_url, html = resolve_landing(session, doi)
    except requests.RequestException:
        return []
    return _find_pdf_links(session, final_url, html)


def try_download(session: requests.Session, url: str, timeout: float = 30) -> bytes | None:
    try:
        r = session.get(url, timeout=timeout, allow_redirects=True)
        if r.content[:5] == b"%PDF-":
            return r.content
    except requests.RequestException:
        pass
    return None
