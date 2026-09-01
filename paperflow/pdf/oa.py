"""OA 下载引擎：Unpaywall（含 PMC/EuropePMC 候选）。"""

from __future__ import annotations

import re
import os
import time
from pathlib import Path
from typing import Iterable

import requests

from .. import net

API = "https://api.unpaywall.org/v2/{}"
OPENALEX_API = "https://api.openalex.org/works/https://doi.org/{}"
OPENALEX_WORKS_API = "https://api.openalex.org/works"
S2_BATCH_API = "https://api.semanticscholar.org/graph/v1/paper/batch"
PROXY_TIMEOUTS = [8, 12, 25]


class OaEngine:
    def __init__(self, email: str = "", proxies: list[str | None] | None = None) -> None:
        self.email = email or os.getenv("UNPAYWALL_EMAIL", "").strip() or os.getenv("NCBI_EMAIL", "").strip()
        self.openalex_api_key = os.getenv("OPENALEX_API_KEY", "").strip()
        self.s2_api_key = os.getenv("S2_API_KEY", "").strip()
        self.s2_limiter = net.RateLimiter(60)
        self.proxies = proxies or net.DEFAULT_PROXIES

    @staticmethod
    def _abstract(payload: dict) -> str:
        inverted = payload.get("abstract_inverted_index") or {}
        words: list[tuple[int, str]] = []
        for word, positions in inverted.items():
            for position in positions or []:
                try:
                    words.append((int(position), str(word)))
                except (TypeError, ValueError):
                    continue
        return " ".join(word for _, word in sorted(words))

    @staticmethod
    def _openalex_candidates(payload: dict) -> list[str]:
        candidates: list[str] = []
        locations = []
        if payload.get("best_oa_location"):
            locations.append(payload["best_oa_location"])
        locations.extend(payload.get("locations") or [])
        for location in locations:
            if not isinstance(location, dict):
                continue
            url = location.get("pdf_url") or (
                location.get("landing_page_url") if location.get("is_oa") else ""
            )
            if url and url not in candidates:
                candidates.append(url)
        return candidates

    def bulk_openalex(self, dois: Iterable[str]) -> dict[str, dict]:
        """一次查询一批 DOI，返回摘要和 OA 候选；适合大队列预解析。"""
        values = [doi.strip().lower() for doi in dois if doi.strip()]
        if not values:
            return {}
        doi_filter = "|".join(f"https://doi.org/{doi}" for doi in values)
        last: Exception | None = None
        for attempt in range(3):
            for proxy in self.proxies:
                try:
                    session = net.make_session(proxy, email=self.email)
                    params = {
                        "filter": f"doi:{doi_filter}",
                        "per-page": len(values),
                        "select": "doi,abstract_inverted_index,best_oa_location,locations",
                        **({"mailto": self.email} if self.email else {}),
                        **({"api_key": self.openalex_api_key} if self.openalex_api_key else {}),
                    }
                    response = session.get(OPENALEX_WORKS_API, params=params, timeout=30)
                    if response.status_code == 429:
                        last = RuntimeError("OpenAlex HTTP 429")
                        continue
                    response.raise_for_status()
                    output: dict[str, dict] = {}
                    for work in response.json().get("results") or []:
                        raw = str(work.get("doi") or "")
                        doi = re.sub(r"^https?://doi\.org/", "", raw, flags=re.I).lower()
                        if doi:
                            output[doi] = {
                                "abstract": self._abstract(work),
                                "candidates": self._openalex_candidates(work),
                            }
                    return output
                except (requests.RequestException, ValueError) as exc:
                    last = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
        raise RuntimeError(f"OpenAlex 批量查询失败: {type(last).__name__ if last else 'unknown'}")

    def bulk_s2(self, dois: Iterable[str]) -> dict[str, dict]:
        """通过 Semantic Scholar batch API 批量补摘要和公开 PDF 候选。"""
        values = [doi.strip().lower() for doi in dois if doi.strip()]
        if not values or not self.s2_api_key:
            return {}
        last: Exception | None = None
        for attempt in range(4):
            self.s2_limiter.wait()
            try:
                session = net.make_session(None, email=self.email)
                response = session.post(
                    S2_BATCH_API,
                    params={"fields": "abstract,openAccessPdf,externalIds"},
                    headers={"x-api-key": self.s2_api_key},
                    json={"ids": [f"DOI:{doi}" for doi in values]},
                    timeout=45,
                )
                if response.status_code == 429:
                    last = RuntimeError("S2 HTTP 429")
                    time.sleep(2 ** attempt)
                    continue
                response.raise_for_status()
                output: dict[str, dict] = {}
                for requested, paper in zip(values, response.json()):
                    if not paper:
                        continue
                    external = paper.get("externalIds") or {}
                    doi = str(external.get("DOI") or requested).lower()
                    candidates: list[str] = []
                    oa_pdf = paper.get("openAccessPdf") or {}
                    if oa_pdf.get("url"):
                        candidates.append(str(oa_pdf["url"]))
                    pmcid = str(external.get("PubMedCentral") or "").strip()
                    if pmcid:
                        pmcid = pmcid if pmcid.upper().startswith("PMC") else f"PMC{pmcid}"
                        candidates.append(f"https://europepmc.org/articles/{pmcid}?pdf=render")
                    output[doi] = {
                        "abstract": str(paper.get("abstract") or ""),
                        "candidates": list(dict.fromkeys(candidates)),
                    }
                return output
            except (requests.RequestException, ValueError) as exc:
                last = exc
        raise RuntimeError(f"S2 批量查询失败: {type(last).__name__ if last else 'unknown'}")

    def _candidates_for_doi(self, doi: str) -> list[str]:
        # OpenAlex 不强制邮箱，先查其公开 locations；再用 Unpaywall 补充更完整的 OA 列表。
        candidates: list[str] = []
        for proxy in self.proxies:
            try:
                session = net.make_session(proxy, email=self.email)
                params = {"mailto": self.email} if self.email else {}
                response = session.get(OPENALEX_API.format(doi), params=params, timeout=20)
                if response.status_code == 404:
                    break
                response.raise_for_status()
                payload = response.json()
                for source in self._openalex_candidates(payload):
                    if source not in candidates:
                        candidates.append(source)
                break
            except Exception:
                continue
        if not self.email:
            return candidates
        return self._unpaywall_candidates(doi, candidates)

    def _unpaywall_candidates(self, doi: str, initial: list[str] | None = None) -> list[str]:
        """查询 Unpaywall；可在 OpenAlex 批量接口限流时作为合法 OA 回退。"""
        candidates = list(initial or [])
        if not self.email:
            return candidates
        last = None
        for attempt in range(3):
            for i, proxy in enumerate(self.proxies):
                s = net.make_session(proxy, email=self.email)
                try:
                    r = s.get(API.format(doi), params={"email": self.email}, timeout=PROXY_TIMEOUTS[i])
                    if r.status_code == 404:
                        return candidates
                    r.raise_for_status()
                    data = r.json()
                    cands: list[str] = list(candidates)
                    locs = []
                    if data.get("best_oa_location"):
                        locs.append(data["best_oa_location"])
                    locs.extend(data.get("oa_locations") or [])
                    for loc in locs:
                        url = loc.get("url_for_pdf") or loc.get("url")
                        if url and url not in cands:
                            cands.append(url)
                    return cands
                except Exception as exc:
                    last = exc
                    continue
        raise RuntimeError(f"unpaywall查询失败: {str(last)[:100]}")

    def fetch(self, doi: str, target: Path) -> tuple[bool, str]:
        try:
            cands = self._candidates_for_doi(doi)
        except Exception as exc:
            return False, str(exc)[:120]
        if not cands:
            return False, "Unpaywall 无 OA 候选"
        for url in cands:
            if re.search(r"pmc\.ncbi\.nlm\.nih\.gov", url):
                proxies = [None] + [p for p in self.proxies if p]  # PMC 直连优先
            else:
                proxies = self.proxies
            for proxy in proxies:
                try:
                    s = net.make_session(proxy, email=self.email)
                    r = s.get(url, timeout=25, allow_redirects=True)
                    if r.content[:5] == b"%PDF-":
                        target.write_bytes(r.content)
                        return True, f"OA → {url[:80]}"
                    break
                except requests.RequestException:
                    continue
        return False, "OA 候选下载失败"
