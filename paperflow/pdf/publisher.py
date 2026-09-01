"""出版社订阅适配器（付费墙）：学校 IP / CARSI 会话下直连出版社 PDF 端点。

每个适配器需知道：
- DOI 前缀判定（publisher_of(doi)）
- 出版社 PDF URL 模板（resolve_pdf_url）
- 会话来源（校园网直连 / 学校代理 / 浏览器注入 cookie）

当前内置最常用的五个出版社模板；学校订阅可用时直接命中。
"""

from __future__ import annotations

import re
from pathlib import Path

import requests

from .. import net

PUBLISHERS = {
    "10.1016": {"name": "Elsevier/ScienceDirect", "special": "sciencedirect"},
    "10.1007": {"name": "Springer", "pdf": "https://link.springer.com/content/pdf/{doi}.pdf"},
    "10.1002": {"name": "Wiley", "pdf": "https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}"},
    "10.1093": {"name": "Oxford", "pdf": "https://academic.oup.com/{doi}/article-pdf"},
    "10.1080": {"name": "Taylor&Francis", "pdf": "https://www.tandfonline.com/doi/pdf/{doi}"},
    "10.1038": {"name": "Nature", "pdf": "https://www.nature.com/articles/{arti}.pdf"},
    "10.1111": {"name": "Wiley/Blackwell", "pdf": "https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}"},
    "10.1042": {"name": "Portland Press", "pdf": "https://portlandpress.com/docserver/fulltext/{doi}"},
    "10.1126": {"name": "Science/AAAS", "pdf": "https://www.science.org/doi/pdf/{doi}"},
    "10.1136": {"name": "BMJ", "pdf": "https://heart.bmj.com/content/{doi}.full.pdf"},
    "10.1103": {"name": "APS", "pdf": "https://journals.aps.org/{j}/abstract/{doi}"},
    "10.1371": {"name": "PLOS", "special": "plos"},
}

_PLOS_JOURNALS = {
    "journal.pbio": "plosbiology", "journal.pcbi": "ploscompbiol", "journal.pgen": "plosgenetics",
    "journal.pmed": "plosmedicine", "journal.pntd": "plosntds", "journal.pone": "plosone",
    "journal.ppat": "plospathogens", "journal.pclm": "plosclimate", "journal.pdig": "plosdigitalhealth",
    "journal.pwat": "ploswater", "journal.pstr": "plossustain", "journal.pn2": "plosnitrogen",
}


def _plos_url(doi: str) -> str:
    stem = doi.split("/", 1)[1] if "/" in doi else doi
    journal_key = next((k for k in _PLOS_JOURNALS if stem.lower().startswith(k)), None)
    jname = _PLOS_JOURNALS[journal_key] if journal_key else "plosone"
    return f"https://journals.plos.org/{jname}/article/file?id={doi}&type=printable"


def _sciencedirect_url(doi: str, session=None) -> tuple[str, str]:
    """解析 ScienceDirect 的 pii，返回 (pdf端点, 说明)。无会话时 pdf 获取会 403。"""
    try:
        s = session or requests.Session()
        r = s.get(f"https://doi.org/{doi}", timeout=20, allow_redirects=True)
        m = re.search(r"/pii/([A-Z0-9]+)", r.url)
        if m:
            pii = m.group(1)
            return f"https://www.sciencedirect.com/science/article/pii/{pii}/pdfft?download=true", f"pii={pii}"
        return f"https://doi.org/{doi}", "未见 pii，退回 doi 重定向"
    except Exception as exc:
        return f"https://doi.org/{doi}", f"解析失败:{type(exc).__name__}"


def publisher_of(doi: str) -> tuple[str, str] | None:
    prefix = doi.split("/")[0] if "/" in doi else doi
    meta = PUBLISHERS.get(prefix)
    if not meta:
        return None
    if meta.get("special") == "plos":
        return meta["name"], _plos_url(doi)
    if meta.get("special") == "sciencedirect":
        url, _ = _sciencedirect_url(doi)
        return meta["name"], url
    arti = doi.split("/")[-1]
    try:
        url = meta["pdf"].format(doi=doi, arti=arti)
    except Exception:
        url = f"https://doi.org/{doi}"
    return meta["name"], url


class PublisherEngine:
    def __init__(self, proxies: list[str | None] | None = None, site: str = "publisher") -> None:
        self.proxies = proxies or net.DEFAULT_PROXIES
        self.site = site
        from .elsevier import ElsevierEngine
        self.elsevier = ElsevierEngine(proxies=self.proxies)
        self.session_cookie: dict[str, str] = {}
        # 自动加载浏览器授权捕获的登录态（合并所有订阅站点）
        try:
            from ..auth import load_site_cookies
            for site_name in ("publisher", "sciencedirect", "springer", "wiley", "oxford", "nature", "publisher"):
                for c in load_site_cookies(site_name):
                    dom = c.get("domain", "")
                    if site == "publisher" or any(d in dom for d in (".sciencedirect", ".springer", ".wiley", ".nature", ".oup", ".tandfonline", ".science.org")):
                        self.session_cookie[c["name"]] = c["value"]
        except Exception:
            pass

    def fetch(self, doi: str, target: Path) -> tuple[bool, str]:
        from .generic import (generic_fetch, try_download, load_learned, save_learned,
                              resolve_landing, _find_pdf_links)

        meta = publisher_of(doi)  # 注意: 内部 _sciencedirect_url 会请求 doi.org 解析 pii
        prefix = doi.split("/")[0] if "/" in doi else doi
        if prefix == "10.1016" and self.elsevier.api_key:
            ok, detail = self.elsevier.fetch(doi, target)
            if ok:
                return ok, detail
        learned = load_learned()
        candidates: list[tuple[str, str]] = []
        if meta:
            candidates.append((meta[1], f"模板:{meta[0]}"))
        learned_tpl = learned.get(prefix)
        if learned_tpl and not any(u == learned_tpl for u, _ in candidates):
            candidates.append((learned_tpl, "学习模板"))

        # 登录会话是直连获取的(cf_clearance 绑定登录 IP)，故 publisher 直连优先
        order = [None] + [p for p in self.proxies if p]
        cookie_header = "; ".join(f"{k}={v}" for k, v in self.session_cookie.items())
        last_err = ""
        for proxy in order:
            try:
                s = net.make_session(proxy)
                if cookie_header:
                    s.headers["Cookie"] = cookie_header
                for url, note in candidates:
                    data = try_download(s, url)
                    if data:
                        target.write_bytes(data)
                        return True, f"publisher({note}) → {url[:70]}"
                # 通用解析：doi.org 落地页提取 PDF 链接
                try:
                    final_url, html = resolve_landing(s, doi)
                except requests.RequestException as exc:
                    last_err = f"landing失败:{type(exc).__name__}"
                    continue
                for pdf_url in _find_pdf_links(s, final_url, html):
                    data = try_download(s, pdf_url)
                    if data:
                        target.write_bytes(data)
                        learned[prefix] = pdf_url
                        save_learned(learned)
                        return True, f"publisher(通用解析) → {pdf_url[:70]}"
                last_err = "落地页无PDF链接或需订阅会话"
                return False, last_err  # 第一个可用链路已走完，无需换代理
            except requests.RequestException as exc:
                last_err = f"{type(exc).__name__}: {str(exc)[:60]}"
                continue
        return False, f"publisher: 网络失败({last_err[:80]})"
