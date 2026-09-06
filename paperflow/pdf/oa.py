"""OA 下载引擎：Unpaywall（含 PMC/EuropePMC 候选）。"""

from __future__ import annotations

import re
import os
import time
import threading
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urlparse

import requests

from .. import net
from .io import save_streamed_pdf

API = "https://api.unpaywall.org/v2/{}"
OPENALEX_API = "https://api.openalex.org/works/https://doi.org/{}"
OPENALEX_WORKS_API = "https://api.openalex.org/works"
S2_BATCH_API = "https://api.semanticscholar.org/graph/v1/paper/batch"
PMC_IDCONV_API = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
CROSSREF_WORK_API = "https://api.crossref.org/works/{}"
CROSSREF_WORKS_API = "https://api.crossref.org/works"
PROXY_TIMEOUTS = [8, 12, 25]


class OaEngine:
    def __init__(self, email: str = "", proxies: list[str | None] | None = None) -> None:
        self.email = email or os.getenv("UNPAYWALL_EMAIL", "").strip() or os.getenv("NCBI_EMAIL", "").strip()
        self.openalex_api_key = os.getenv("OPENALEX_API_KEY", "").strip()
        self.s2_api_key = os.getenv("S2_API_KEY", "").strip()
        self.s2_limiter = net.RateLimiter(60)
        self.crossref_limiter = net.RateLimiter(120)
        self.proxies = proxies or net.DEFAULT_PROXIES
        self._openalex_retry_at = 0.0
        self._openalex_lock = threading.Lock()

    def _pause_openalex(self, response: requests.Response) -> None:
        retry_after = 3600
        try:
            retry_after = int(response.headers.get("Retry-After") or 0) or int(
                (response.json() or {}).get("retryAfter") or 3600
            )
        except (TypeError, ValueError):
            pass
        with self._openalex_lock:
            self._openalex_retry_at = max(
                self._openalex_retry_at, time.monotonic() + min(max(retry_after, 60), 86400)
            )

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
    def _normalize_candidate(url: str) -> str:
        """Turn common PMC landing URLs into a stable public PDF endpoint."""
        value = str(url or "").strip()
        parsed = urlparse(value)
        # Keep a real NCBI PDF URL intact. Replacing it with an article-level
        # Europe PMC URL can lose NIH manuscript PDFs that Europe PMC cannot
        # render itself.
        if "/pdf/" in parsed.path.casefold() or parsed.path.casefold().endswith(".pdf"):
            return value
        match = re.search(
            r"https?://(?:www\.)?(?:pmc\.)?ncbi\.nlm\.nih\.gov/(?:pmc/)?articles/(?:PMC)?(\d+)",
            value,
            flags=re.I,
        )
        if match:
            return f"https://europepmc.org/articles/PMC{match.group(1)}?pdf=render"
        return value

    def _mdpi_asset_candidates(self, doi: str, candidates: list[str]) -> list[str]:
        """Derive MDPI's public static PDF URL when its front door blocks servers.

        MDPI's article host frequently answers legitimate server downloads with
        HTTP 403 while the same OA PDF is available from ``mdpi-res.com``.  The
        journal name comes from Crossref metadata; volume/article numbers come
        from the already-discovered MDPI URL.  This runs only for MDPI records
        and quietly preserves the original candidate on any metadata failure.
        """
        if any("mdpi-res.com/" in value.casefold() for value in candidates):
            return candidates
        mdpi_url = next((value for value in candidates if re.search(
            r"https?://(?:www\.)?mdpi\.com/[^/]+/\d+/\d+/\d+(?:/pdf)?", value, re.I
        )), "")
        if not mdpi_url and not doi.casefold().startswith("10.3390/"):
            return candidates
        try:
            self.crossref_limiter.wait()
            session = net.make_session(None, email=self.email)
            response = session.get(
                CROSSREF_WORK_API.format(quote(doi, safe="")), timeout=15
            )
            response.raise_for_status()
            message = response.json().get("message") or {}
            title = str((message.get("container-title") or [""])[0]).strip()
            if not mdpi_url:
                mdpi_url = next((
                    str(link.get("URL") or "") for link in message.get("link") or []
                    if "mdpi.com/" in str(link.get("URL") or "").casefold()
                ), "")
        except (requests.RequestException, ValueError, TypeError, IndexError):
            return candidates
        match = re.search(r"mdpi\.com/[^/]+/(\d+)/\d+/(\d+)", mdpi_url, re.I)
        if not match:
            return candidates
        words = re.findall(r"[a-z0-9]+", title.casefold())
        if not words:
            return candidates
        stopwords = {"a", "an", "and", "for", "in", "of", "on", "the", "to"}
        meaningful = [word for word in words if word not in stopwords]
        slugs = [words[0], "".join(word[0] for word in meaningful), "".join(words)]
        doi_code = re.match(r"[a-z]+", doi.partition("/")[2].casefold())
        if doi_code:
            slugs.append(doi_code.group())
        volume = match.group(1).zfill(2)
        article = match.group(2).zfill(5)
        generated = []
        for slug in dict.fromkeys(filter(None, slugs)):
            stem = f"{slug}-{volume}-{article}"
            generated.append(
                f"https://mdpi-res.com/d_attachment/{slug}/{stem}/article_deploy/{stem}.pdf"
            )
        return [*generated, *candidates]

    @staticmethod
    def _normalized_title(value: str) -> str:
        return "".join(character for character in value.casefold() if character.isalnum())

    def crossref_title_match(self, title: str, year: str = "") -> dict[str, object]:
        """Resolve a missing DOI only when Crossref metadata is unambiguous.

        Exact normalized titles are accepted. A near-exact title must also have
        the same publication year. This intentionally favors precision over
        recall because a wrong DOI can attach the wrong full text to a record.
        """
        wanted = self._normalized_title(title)
        if len(wanted) < 16:
            return {}
        self.crossref_limiter.wait()
        try:
            session = net.make_session(None, email=self.email)
            response = session.get(
                CROSSREF_WORKS_API,
                params={
                    "query.title": title,
                    "rows": 3,
                    "select": "DOI,title,published,issued,link",
                    **({"mailto": self.email} if self.email else {}),
                },
                timeout=20,
            )
            response.raise_for_status()
            items = (response.json().get("message") or {}).get("items") or []
        except (requests.RequestException, ValueError, TypeError):
            return {}
        for item in items:
            if not isinstance(item, dict):
                continue
            candidate_title = str((item.get("title") or [""])[0])
            normalized = self._normalized_title(candidate_title)
            date_parts = (
                (item.get("published") or item.get("issued") or {}).get("date-parts")
                or [[]]
            )
            candidate_year = str(date_parts[0][0]) if date_parts and date_parts[0] else ""
            exact = normalized == wanted
            near = bool(
                year and candidate_year == str(year)
                and SequenceMatcher(None, wanted, normalized).ratio() >= 0.98
            )
            doi = re.sub(
                r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "",
                str(item.get("DOI") or "").strip(), flags=re.I,
            ).casefold()
            if not doi or not (exact or near):
                continue
            urls = []
            for link in item.get("link") or []:
                if not isinstance(link, dict):
                    continue
                url = str(link.get("URL") or "").strip()
                content_type = str(link.get("content-type") or "").casefold()
                if url and ("pdf" in content_type or url.casefold().endswith(".pdf")):
                    urls.append(url)
            return {"doi": doi, "candidates": list(dict.fromkeys(urls))}
        return {}

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
            url = OaEngine._normalize_candidate(url)
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
                        self._pause_openalex(response)
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
                        candidates.append(self._normalize_candidate(str(oa_pdf["url"])))
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

    def bulk_pmc_idconv(self, dois: Iterable[str]) -> dict[str, str]:
        """Use NCBI's official batch API to resolve DOI values to PMC IDs."""
        values = list(dict.fromkeys(
            doi.strip().lower() for doi in dois if doi.strip()
        ))[:200]
        if not values:
            return {}
        params = {
            "ids": ",".join(values),
            "format": "json",
            "versions": "no",
            "tool": "paperflow",
            **({"email": self.email} if self.email else {}),
        }
        last: Exception | None = None
        exits = [None] + [proxy for proxy in self.proxies if proxy]
        for attempt in range(3):
            for proxy in exits:
                try:
                    session = net.make_session(proxy, email=self.email)
                    response = session.get(PMC_IDCONV_API, params=params, timeout=30)
                    if response.status_code == 429:
                        last = RuntimeError("PMC ID converter HTTP 429")
                        continue
                    response.raise_for_status()
                    output: dict[str, str] = {}
                    for record in response.json().get("records") or []:
                        doi = str(record.get("doi") or record.get("requested-id") or "").lower()
                        pmcid = str(record.get("pmcid") or "").upper()
                        if doi and re.fullmatch(r"PMC\d+", pmcid):
                            output[doi] = pmcid
                    return output
                except (requests.RequestException, ValueError) as exc:
                    last = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
        raise RuntimeError(f"PMC DOI 批量转换失败: {type(last).__name__ if last else 'unknown'}")

    def _candidates_for_doi(self, doi: str) -> list[str]:
        # OpenAlex 不强制邮箱，先查其公开 locations；再用 Unpaywall 补充更完整的 OA 列表。
        candidates: list[str] = []
        if time.monotonic() >= self._openalex_retry_at:
            for proxy in self.proxies:
                try:
                    session = net.make_session(proxy, email=self.email)
                    params = {"mailto": self.email} if self.email else {}
                    response = session.get(OPENALEX_API.format(doi), params=params, timeout=20)
                    if response.status_code == 404:
                        break
                    if response.status_code == 429:
                        self._pause_openalex(response)
                        break
                    response.raise_for_status()
                    payload = response.json()
                    for source in self._openalex_candidates(payload):
                        if source not in candidates:
                            candidates.append(source)
                    break
                except Exception:
                    continue
        # A configured S2 key provides an independent OA fallback when the
        # OpenAlex daily budget is exhausted.  Keep Unpaywall last because it
        # requires a contact email and may return repository landing pages.
        if not candidates and self.s2_api_key:
            try:
                item = self.bulk_s2([doi]).get(doi.lower()) or {}
                for source in item.get("candidates") or []:
                    source = self._normalize_candidate(source)
                    if source and source not in candidates:
                        candidates.append(source)
            except Exception:
                pass
        candidates = self._mdpi_asset_candidates(doi, candidates)
        if self.email and not any("mdpi-res.com/" in value.casefold() for value in candidates):
            candidates = self._unpaywall_candidates(doi, candidates)
        return candidates

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
                        url = self._normalize_candidate(loc.get("url_for_pdf") or loc.get("url"))
                        if url and url not in cands:
                            cands.append(url)
                    return cands
                except Exception as exc:
                    last = exc
                    continue
        return candidates

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
                    r = s.get(url, timeout=25, allow_redirects=True, stream=True)
                    r.raise_for_status()
                    if save_streamed_pdf(r, target):
                        return True, f"OA → {url[:80]}"
                    # This location returned HTML/JSON; continue with another
                    # location and proxy instead of abandoning the DOI.
                    continue
                except requests.RequestException:
                    continue
        return False, "OA 候选下载失败"
