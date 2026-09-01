"""PubMed / Europe PMC / Crossref / Semantic Scholar 数据源适配器。"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET

from .. import net
from ..models import Paper, PdfCandidate, author_names, clean_text, normalize_doi
from . import SOURCES


class PubMedSource:
    name = "PubMed"

    def search_species(self, client, species: str, limit: int) -> list[Paper]:
        params = {
            "db": "pubmed", "term": f'"{species}"[Title/Abstract]',
            "retmode": "json", "retmax": min(limit, 10000) if limit > 0 else 10000,
            "tool": "species-literature-downloader",
        }
        if os.getenv("NCBI_EMAIL"):
            params["email"] = os.environ["NCBI_EMAIL"]
        if os.getenv("NCBI_API_KEY"):
            params["api_key"] = os.environ["NCBI_API_KEY"]
        ids = client.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                         params=params).json()["esearchresult"]["idlist"]
        if not ids:
            return []
        fetch_params = {"db": "pubmed", "id": ",".join(ids), "retmode": "xml", "tool": params["tool"]}
        if "email" in params:
            fetch_params["email"] = params["email"]
        root = ET.fromstring(client.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
            params=fetch_params).content)
        papers: list[Paper] = []
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
                sources={"PubMed"},
            ))
        return papers

    def search_doi(self, client, doi: str) -> list[Paper]:
        if not doi:
            return []
        params = {"db": "pubmed", "term": f'"{doi}"[DOI]', "retmode": "json", "retmax": 5}
        ids = client.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                         params=params).json()["esearchresult"]["idlist"]
        if not ids:
            return []
        params.update({"id": ",".join(ids), "retmode": "xml"})
        root = ET.fromstring(client.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi", params=params).content)
        for article in root.findall(".//PubmedArticle"):
            title = "".join(article.findtext(".//ArticleTitle", default=""))
            if title:
                return [Paper(title=clean_text(title), doi=normalize_doi(doi), sources={"PubMed"})]
        return []


class EuropePmcSource:
    name = "Europe PMC"

    def search_species(self, client, species: str, limit: int) -> list[Paper]:
        payload = client.get(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={"query": f'"{species}" OPEN_ACCESS:Y', "format": "json",
                    "pageSize": min(limit, 100) if limit > 0 else 100, "resultType": "core"},
        ).json()
        papers = []
        for item in payload.get("resultList", {}).get("result", []):
            pmcid = clean_text(item.get("pmcid"))
            authors = [a.get("fullName", "") for a in (item.get("authorList") or {}).get("author", [])]
            paper = Paper(
                title=clean_text(item.get("title")), abstract=clean_text(item.get("abstractText")),
                year=clean_text(item.get("pubYear")), journal=clean_text(item.get("journalTitle")),
                authors=author_names(authors), doi=normalize_doi(item.get("doi", "")),
                pmid=clean_text(item.get("pmid")), pmcid=pmcid, sources={"Europe PMC"},
                species={species},
            )
            if pmcid and item.get("isOpenAccess") == "Y":
                paper.add_candidate(f"https://europepmc.org/articles/{pmcid}?pdf=render", "europepmc", priority=2)
            papers.append(paper)
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
                  "select": "DOI,title,abstract,author,published,container-title,type"}
        items = client.get("https://api.crossref.org/works", params=params).json()["message"]["items"]
        papers = []
        for item in items:
            if item.get("type") not in {"journal-article", "proceedings-article", "posted-content"}:
                continue
            title = clean_text(" ".join(item.get("title", [])))
            combined = f"{title} {clean_text(item.get('abstract'))}".casefold()
            if species and species.casefold() not in combined:
                continue
            date_parts = (item.get("published") or {}).get("date-parts") or [[]]
            papers.append(Paper(
                title=title, abstract=clean_text(item.get("abstract")),
                year=str(date_parts[0][0]) if date_parts and date_parts[0] else "",
                journal=clean_text(" ".join(item.get("container-title", []))),
                authors=author_names(item.get("author", [])), doi=normalize_doi(item.get("DOI", "")),
                sources={"Crossref"}, species={species} if species else set(),
            ))
        return papers

    def search_species(self, client, species: str, limit: int) -> list[Paper]:
        return self._search(client, f'"{species}"', species, limit)

    def search_doi(self, client, doi: str) -> list[Paper]:
        if not doi:
            return []
        try:
            payload = client.get(f"https://api.crossref.org/works/{doi}",
                                 params={"select": "DOI,title,abstract,author,published,container-title"}).json()["message"]
        except Exception:
            return []
        title = clean_text(" ".join(payload.get("title", [])))
        date_parts = (payload.get("published") or {}).get("date-parts") or [[]]
        return [Paper(
            title=title or doi, abstract=clean_text(payload.get("abstract")),
            year=str(date_parts[0][0]) if date_parts and date_parts[0] else "",
            journal=clean_text(" ".join(payload.get("container-title", []))),
            authors=author_names(payload.get("author", [])), doi=normalize_doi(doi),
            sources={"Crossref"},
        )]


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
        data = self._get(
            client,
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={"query": query, "limit": min(limit, 100) if limit > 0 else 100, "fields": fields},
        ).json().get("data", [])
        papers = []
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
                pmid=str(ids.get("PubMed", "")), sources={"S2"},
            )
            if oa.get("url"):
                paper.add_candidate(oa["url"], "s2", priority=3)
            papers.append(paper)
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
