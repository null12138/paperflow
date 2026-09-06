"""PubMed / Europe PMC / Crossref / Semantic Scholar 数据源适配器。"""

from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from typing import Any

import requests

from .. import net
from ..models import Paper, PdfCandidate, author_names, clean_text, normalize_doi
from . import SOURCES


class PubMedSource:
    name = "PubMed"
    fetch_batch_size = 200

    @staticmethod
    def _get(client: Any, url: str, params: dict[str, Any], operation: str) -> Any:
        """Fetch an NCBI response with bounded retries for transient/bad replies."""
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = client.get(url, params=params, timeout=30)
                status = int(getattr(response, "status_code", 200))
                if status == 429 or status >= 500 or status == 408:
                    last_error = RuntimeError(f"HTTP {status}")
                elif status >= 400:
                    raise RuntimeError(f"HTTP {status}")
                else:
                    return response
            except requests.exceptions.RequestException as exc:
                last_error = exc
            if attempt < 2:
                time.sleep(attempt + 1)
        raise RuntimeError(f"PubMed {operation} 暂时不可用（已重试 3 次：{last_error}）") from None

    @classmethod
    def _json(cls, client: Any, url: str, params: dict[str, Any], operation: str) -> dict[str, Any]:
        for attempt in range(3):
            response = cls._get(client, url, params, operation)
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    return payload
                raise ValueError("JSON 根节点不是对象")
            except Exception as exc:
                if attempt == 2:
                    raise RuntimeError(f"PubMed {operation} 返回了无法解析的 JSON") from None
                time.sleep(attempt + 1)
        raise AssertionError

    @classmethod
    def _xml(cls, client: Any, url: str, params: dict[str, Any], operation: str) -> ET.Element:
        for attempt in range(3):
            response = cls._get(client, url, params, operation)
            try:
                return ET.fromstring(response.content)
            except (ET.ParseError, TypeError, ValueError):
                if attempt == 2:
                    raise RuntimeError(f"PubMed {operation} 返回了格式错误的 XML，已重试 3 次") from None
                time.sleep(attempt + 1)
        raise AssertionError

    def _search_ids(self, client, params: dict, limit: int) -> list[str]:
        """Page ESearch; split large queries by disjoint PMID ranges.

        PubMed only exposes the first 9,999 IDs per query. Range partitions
        avoid that window without limiting the number of collected articles.
        """
        url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        ids: list[str] = []
        seen: set[str] = set()
        base_term = params["term"]

        def fetch(term: str, start: int, size: int) -> dict:
            payload = self._json(client, url, {**params, "term": term, "retstart": start, "retmax": size}, "esearch")
            result = payload.get("esearchresult", {})
            if payload.get("error") or result.get("ERROR") or result.get("errorlist"):
                raise RuntimeError("PubMed 检索失败，未能完整获取结果")
            return result

        def collect(term: str, lo: int, hi: int, initial=None):
            size = min(1000, limit - len(ids)) if limit > 0 else 1000
            result = initial if initial is not None else fetch(term, 0, size)
            total = int(result.get("count", len(result.get("idlist", []))))
            needed = min(total, limit - len(ids)) if limit > 0 else total
            if needed > 9999:
                if lo >= hi:
                    raise RuntimeError("PubMed 无法继续拆分检索，结果未完整获取")
                mid = (lo + hi) // 2
                left_term = f'({base_term}) AND {lo}:{mid}[UID]'
                right_term = f'({base_term}) AND {mid + 1}:{hi}[UID]'
                left, right = fetch(left_term, 0, size), fetch(right_term, 0, size)
                if int(left["count"]) + int(right["count"]) != total:
                    raise RuntimeError("PubMed 分段总数与检索总数不一致，请重试")
                collect(left_term, lo, mid, left)
                if limit <= 0 or len(ids) < limit:
                    collect(right_term, mid + 1, hi, right)
                return
            offset = 0
            while offset < needed:
                batch = result.get("idlist", [])
                if not batch:
                    raise RuntimeError("PubMed 分页提前结束，结果未完整获取")
                fresh = [str(uid) for uid in batch[:needed - offset] if str(uid) not in seen]
                if not fresh:
                    raise RuntimeError("PubMed 返回重复分页，结果未完整获取")
                ids.extend(fresh)
                seen.update(fresh)
                offset += len(batch)
                if offset < needed:
                    result = fetch(term, offset, min(1000, needed - offset))
        collect(base_term, 1, 2147483647)
        return ids

    def search_species(self, client, species: str, limit: int) -> list[Paper]:
        params = {
            "db": "pubmed", "term": f'"{species}"[Title/Abstract]',
            "retmode": "json", "retmax": 1000,
            "tool": "species-literature-downloader",
        }
        if os.getenv("NCBI_EMAIL"):
            params["email"] = os.environ["NCBI_EMAIL"]
        if os.getenv("NCBI_API_KEY"):
            params["api_key"] = os.environ["NCBI_API_KEY"]
        ids = self._search_ids(client, params, limit)
        if not ids:
            return []
        fetch_params = {"db": "pubmed", "retmode": "xml", "tool": params["tool"]}
        if "email" in params:
            fetch_params["email"] = params["email"]
        if "api_key" in params:
            fetch_params["api_key"] = params["api_key"]
        papers: list[Paper] = []
        for start in range(0, len(ids), self.fetch_batch_size):
            batch_params = {**fetch_params, "id": ",".join(ids[start:start + self.fetch_batch_size])}
            root = self._xml(client, "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi", batch_params, "efetch")
            for article in root.findall(".//PubmedArticle"):
                title = "".join(article.findtext(".//ArticleTitle", default=""))
                abstract = " ".join("".join(n.itertext()) for n in article.findall(".//Abstract/AbstractText"))
                ids_map = {
                    node.attrib.get("IdType", ""): (node.text or "")
                    for node in article.findall("./PubmedData/ArticleIdList/ArticleId")
                }
                pmcid = ids_map.get("pmc", "")
                authors = []
                for node in article.findall(".//Author"):
                    collective = node.findtext("CollectiveName")
                    authors.append(collective or " ".join(filter(None, [node.findtext("ForeName"), node.findtext("LastName")])))
                papers.append(Paper(
                    title=clean_text(title), abstract=clean_text(abstract),
                    year=clean_text(article.findtext(".//PubDate/Year") or article.findtext(".//ArticleDate/Year")),
                    journal=clean_text(article.findtext(".//Journal/Title")), authors=author_names(authors),
                    doi=normalize_doi(ids_map.get("doi", "")), pmid=ids_map.get("pubmed", ""), pmcid=pmcid,
                    sources={"PubMed"}, species={species},
                ))
                if pmcid:
                    papers[-1].add_candidate(
                        f"https://europepmc.org/articles/{pmcid}?pdf=render",
                        "pubmed-pmc", priority=1,
                    )
        return papers

    def search_doi(self, client, doi: str) -> list[Paper]:
        if not doi:
            return []
        params = {"db": "pubmed", "term": f'"{doi}"[DOI]', "retmode": "json", "retmax": 5}
        ids = self._json(client, "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", params, "esearch").get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []
        params.update({"id": ",".join(ids), "retmode": "xml"})
        root = self._xml(client, "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi", params, "efetch")
        for article in root.findall(".//PubmedArticle"):
            title = "".join(article.findtext(".//ArticleTitle", default=""))
            if title:
                return [Paper(title=clean_text(title), doi=normalize_doi(doi), sources={"PubMed"})]
        return []


class EuropePmcSource:
    name = "Europe PMC"

    def search_species(self, client, species: str, limit: int) -> list[Paper]:
        # Search indexed article text, including body and back matter.
        # Do not restrict discovery to the open-access subset; PDF availability
        # is evaluated separately from whether the article text is indexed.
        phrase = species.replace('\\', '\\\\').replace('"', '\\"')
        query = "(" + " OR ".join(f'{field}:"{phrase}"' for field in
                                  ("TITLE", "ABSTRACT", "BODY", "BACK", "APPENDIX")) + ")"
        papers = []
        cursor = "*"
        while True:
            remaining = limit - len(papers) if limit > 0 else 1000
            page_size = min(max(remaining, 1), 1000)
            payload = client.get(
                "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                params={"query": query, "format": "json",
                        "pageSize": page_size, "resultType": "core", "cursorMark": cursor},
            ).json()
            items = payload.get("resultList", {}).get("result", [])
            for item in items:
                pmcid = clean_text(item.get("pmcid"))
                authors = [a.get("fullName", "") for a in (item.get("authorList") or {}).get("author", [])]
                paper = Paper(
                    title=clean_text(item.get("title")), abstract=clean_text(item.get("abstractText")),
                    year=clean_text(item.get("pubYear")), journal=clean_text(item.get("journalTitle")),
                    authors=author_names(authors), doi=normalize_doi(item.get("doi", "")),
                    pmid=clean_text(item.get("pmid") or (item.get("id") if item.get("source") == "MED" else "")), pmcid=pmcid, sources={"Europe PMC"},
                    species={species},
                )
                if pmcid and item.get("isOpenAccess") == "Y":
                    paper.add_candidate(f"https://europepmc.org/articles/{pmcid}?pdf=render", "europepmc", priority=1)
                papers.append(paper)
                if limit > 0 and len(papers) >= limit:
                    break
            if (limit > 0 and len(papers) >= limit) or not items:
                break
            next_cursor = payload.get("nextCursorMark")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return papers

    def search_doi(self, client, doi: str) -> list[Paper]:
        if not doi:
            return []
        payload = client.get(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={"query": f"DOI:{doi}", "format": "json", "pageSize": 5},
        ).json()
        out = []
        for item in payload.get("resultList", {}).get("result", []):
            pmcid = clean_text(item.get("pmcid"))
            paper = Paper(title=clean_text(item.get("title")), doi=normalize_doi(doi),
                          pmcid=pmcid, sources={"Europe PMC"})
            if pmcid and item.get("isOpenAccess") == "Y":
                paper.add_candidate(f"https://europepmc.org/articles/{pmcid}?pdf=render", "europepmc", priority=2)
            out.append(paper)
        return out


class CrossrefSource:
    name = "Crossref"

    def _search(self, client, query: str, species: str, limit: int) -> list[Paper]:
        params = {"query.bibliographic": query, "rows": min(limit, 1000) if limit > 0 else 1000,
                  "select": "DOI,title,abstract,author,published,container-title,type,link", "cursor": "*"}
        papers = []
        seen: set[str] = set()
        previous_page = None
        while True:
            response = client.get("https://api.crossref.org/works", params=dict(params), timeout=30)
            response.raise_for_status()
            message = response.json()["message"]
            items = message["items"]
            if not items:
                break
            signature = tuple(str(item.get("DOI")) for item in items)
            if signature == previous_page:
                raise RuntimeError("Crossref 返回重复分页，结果未完整获取")
            previous_page = signature
            for item in items:
                if item.get("type") not in {"journal-article", "proceedings-article", "posted-content"}:
                    continue
                title = clean_text(" ".join(item.get("title", [])))
                combined = f"{title} {clean_text(item.get('abstract'))}".casefold()
                if species and species.casefold() not in combined:
                    continue
                date_parts = (item.get("published") or {}).get("date-parts") or [[]]
                paper = Paper(
                    title=title, abstract=clean_text(item.get("abstract")),
                    year=str(date_parts[0][0]) if date_parts and date_parts[0] else "",
                    journal=clean_text(" ".join(item.get("container-title", []))),
                    authors=author_names(item.get("author", [])), doi=normalize_doi(item.get("DOI", "")),
                    sources={"Crossref"}, species={species} if species else set(),
                )
                for link in item.get("link") or []:
                    url = link.get("URL") if isinstance(link, dict) else ""
                    content_type = str(link.get("content-type") or "").casefold() if isinstance(link, dict) else ""
                    if url and ("pdf" in content_type or str(url).casefold().endswith(".pdf")):
                        paper.add_candidate(str(url), "crossref", priority=1)
                if paper.key not in seen:
                    seen.add(paper.key)
                    papers.append(paper)
                if limit > 0 and len(papers) >= limit:
                    return papers
            if len(items) < params["rows"]:
                break
            cursor = message.get("next-cursor")
            if not cursor:
                raise RuntimeError("Crossref 缺少下一页游标，结果未完整获取")
            params["cursor"] = cursor
        return papers

    def search_species(self, client, species: str, limit: int) -> list[Paper]:
        return self._search(client, f'"{species}"', species, limit)

    def search_doi(self, client, doi: str) -> list[Paper]:
        if not doi:
            return []
        try:
            payload = client.get(f"https://api.crossref.org/works/{doi}",
                                 params={"select": "DOI,title,abstract,author,published,container-title,link"}).json()["message"]
        except Exception:
            return []
        title = clean_text(" ".join(payload.get("title", [])))
        date_parts = (payload.get("published") or {}).get("date-parts") or [[]]
        paper = Paper(
            title=title or doi, abstract=clean_text(payload.get("abstract")),
            year=str(date_parts[0][0]) if date_parts and date_parts[0] else "",
            journal=clean_text(" ".join(payload.get("container-title", []))),
            authors=author_names(payload.get("author", [])), doi=normalize_doi(doi),
            sources={"Crossref"},
        )
        for link in payload.get("link") or []:
            url = link.get("URL") if isinstance(link, dict) else ""
            content_type = str(link.get("content-type") or "").casefold() if isinstance(link, dict) else ""
            if url and ("pdf" in content_type or str(url).casefold().endswith(".pdf")):
                paper.add_candidate(str(url), "crossref", priority=1)
        return [paper]


class SemanticScholarSource:
    name = "S2"

    def __init__(self) -> None:
        # S2 全局共享实例会被并行检索线程调用；统一限制为每秒最多 1 请求。
        self._limiter = net.RateLimiter(max_per_minute=60)

    @staticmethod
    def _headers() -> dict[str, str]:
        return {"x-api-key": os.environ["S2_API_KEY"]} if os.getenv("S2_API_KEY") else {}

    def _get(self, client, url: str, **kwargs):
        self._limiter.wait()
        response = client.get(url, headers=self._headers(), **kwargs)
        status = getattr(response, "status_code", None)
        if status == 429:
            if os.getenv("S2_API_KEY"):
                raise RuntimeError("S2 接口触发限流（HTTP 429），请稍后重试或降低检索频率")
            raise RuntimeError("S2 匿名接口配额已限流（HTTP 429）；请在 .env 配置 S2_API_KEY 后重试")
        if status == 403:
            raise RuntimeError("S2 API Key 无权访问或配额已用完（HTTP 403）")
        response.raise_for_status()
        return response

    def _search(self, client, query: str, limit: int, require_hit: str = "") -> list[Paper]:
        fields = "title,abstract,year,authors,venue,externalIds,openAccessPdf,url"
        params = {"query": query, "fields": fields}
        papers = []
        seen: set[str] = set()
        tokens: set[str] = set()
        while True:
            payload = self._get(
                client, "https://api.semanticscholar.org/graph/v1/paper/search/bulk",
                params=dict(params), timeout=30,
            ).json()
            if "data" not in payload:
                raise RuntimeError("S2 返回无效检索结果")
            data = payload["data"]
            for item in data:
                combined = f"{item.get('title', '')} {item.get('abstract', '')}".casefold()
                if require_hit and require_hit.casefold() not in combined:
                    continue
                ids = item.get("externalIds") or {}
                oa = item.get("openAccessPdf") or {}
                paper = Paper(
                    title=clean_text(item.get("title")), abstract=clean_text(item.get("abstract")),
                    year=str(item.get("year") or ""), journal=clean_text(item.get("venue")),
                    authors=author_names(item.get("authors", [])), doi=normalize_doi(ids.get("DOI", "")),
                    pmid=str(ids.get("PubMed") or ""), sources={"S2"}, species={require_hit} if require_hit else set(),
                )
                if oa.get("url"):
                    paper.add_candidate(oa["url"], "s2", priority=3)
                key = item.get("paperId") or paper.key
                if key not in seen:
                    seen.add(key)
                    papers.append(paper)
                if limit > 0 and len(papers) >= limit:
                    return papers
            token = payload.get("token")
            if not token:
                break
            if token in tokens:
                raise RuntimeError("S2 返回重复分页游标，结果未完整获取")
            tokens.add(token)
            params["token"] = token
        return papers

    def search_species(self, client, species: str, limit: int) -> list[Paper]:
        return self._search(client, species, limit, require_hit=species)

    def search_doi(self, client, doi: str) -> list[Paper]:
        if not doi:
            return []
        fields = "title,abstract,year,authors,venue,externalIds,openAccessPdf,url"
        data = self._get(
            client,
            f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}",
            params={"fields": fields},
        ).json()
        if not data or "title" not in data:
            return []
        oa = data.get("openAccessPdf") or {}
        paper = Paper(title=clean_text(data.get("title")), abstract=clean_text(data.get("abstract")),
                      year=str(data.get("year") or ""), journal=clean_text(data.get("venue")),
                      authors=author_names(data.get("authors", [])), doi=normalize_doi(doi),
                      sources={"S2"})
        if oa.get("url"):
            paper.add_candidate(oa["url"], "s2", priority=3)
        return [paper]


SOURCES.register(PubMedSource())
SOURCES.register(EuropePmcSource())
SOURCES.register(CrossrefSource())
SOURCES.register(SemanticScholarSource())
