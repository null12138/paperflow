"""通用出版社 PDF 解析器：任何 DOI → doi.org 重定向 → 落地页 HTML 提取 PDF 链接 → 下载。

覆盖全部出版社（不依赖手工模板）；成功路径自动学习并持久化模板，
下次同前缀直接走学到的模板。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

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
    """从落地页提取可能的 PDF 链接（按优先级排序）。"""
    cands: list[str] = []
    soup = BeautifulSoup(text, "html.parser")
    meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
    if meta and meta.attrs.get("content"):
        cands.append(str(meta.attrs["content"]))
    for a in soup.find_all("a", href=True):
        href = a["href"]
        low = href.lower()
        if low.endswith(".pdf") or "pdf" in low and ("download" in low or "pdf" in low):
            cands.append(href)
        elif a.get_text(" ", strip=True).lower() in ("pdf", "download pdf", "full text pdf", "pdf (1"):
            cands.append(href)
    for tag in soup.find_all(["iframe", "embed", "object"]):
        for attr in ("src", "data"):
            url = tag.attrs.get(attr)
            if url and ".pdf" in str(url).lower():
                cands.append(str(url))
    # 去重+规范化
    out: list[str] = []
    seen = set()
    scheme = re.match(r"^(https?://[^/]+)", page_url)
    base = scheme.group(1) if scheme else ""
    for u in cands:
        u = u.strip().lstrip("\"'")
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/"):
            if base:
                u = base + u
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