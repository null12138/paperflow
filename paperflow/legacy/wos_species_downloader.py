#!/usr/bin/env python3
"""按物种拉丁名检索论文元数据，并下载、校验合法可访问的 PDF。"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import requests
from pypdf import PdfReader


USER_AGENT = "SpeciesLiteratureDownloader/1.0 (research utility; contact: {email})"
TIMEOUT = 35


@dataclass
class PdfCandidate:
    url: str
    source: str


@dataclass
class Paper:
    title: str
    abstract: str = ""
    year: str = ""
    journal: str = ""
    authors: list[str] = field(default_factory=list)
    doi: str = ""
    pmid: str = ""
    pmcid: str = ""
    sources: set[str] = field(default_factory=set)
    species: set[str] = field(default_factory=set)
    pdf_candidates: list[PdfCandidate] = field(default_factory=list)
    downloaded_path: str = ""
    failure_reason: str = ""

    @property
    def key(self) -> str:
        if self.doi:
            return "doi:" + normalize_doi(self.doi)
        if self.pmid:
            return "pmid:" + self.pmid
        return "title:" + normalize_title(self.title)


class Client:
    def __init__(self, email: str = "") -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT.format(email=email or "not-provided")

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self.session.get(url, timeout=TIMEOUT, allow_redirects=True, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1.5 * (2**attempt))
        assert last_error is not None
        raise last_error


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_doi(doi: str) -> str:
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", doi.strip(), flags=re.I).lower()


def normalize_title(title: str) -> str:
    value = unicodedata.normalize("NFKD", title).casefold()
    return re.sub(r"[^a-z0-9]+", "", value)


def author_names(items: Iterable[Any]) -> list[str]:
    names: list[str] = []
    for item in items or []:
        if isinstance(item, str):
            name = clean_text(item)
        else:
            name = clean_text(
                item.get("name")
                or " ".join(filter(None, [item.get("given", ""), item.get("family", "")]))
            )
        if name and name not in names:
            names.append(name)
    return names


def merge_paper(target: Paper, incoming: Paper) -> None:
    for attr in ("abstract", "year", "journal", "doi", "pmid", "pmcid"):
        old = getattr(target, attr)
        new = getattr(incoming, attr)
        if (not old or (attr == "abstract" and len(new) > len(old))) and new:
            setattr(target, attr, new)
    if len(incoming.title) > len(target.title):
        target.title = incoming.title
    for author in incoming.authors:
        if author not in target.authors:
            target.authors.append(author)
    target.sources.update(incoming.sources)
    target.species.update(incoming.species)
    seen = {candidate.url for candidate in target.pdf_candidates}
    target.pdf_candidates.extend(c for c in incoming.pdf_candidates if c.url not in seen)


def add_paper(collection: dict[str, Paper], paper: Paper) -> None:
    if not paper.title:
        return
    aliases = [paper.key]
    if paper.doi:
        aliases.append("doi:" + normalize_doi(paper.doi))
    if paper.pmid:
        aliases.append("pmid:" + paper.pmid)
    aliases.append("title:" + normalize_title(paper.title))
    existing = next((collection[key] for key in aliases if key in collection), None)
    if existing:
        merge_paper(existing, paper)
        for key in aliases:
            collection[key] = existing
    else:
        for key in aliases:
            collection[key] = paper


def search_wos(client: Client, species: str, limit: int) -> list[Paper]:
    api_key = os.getenv("WOS_API_KEY", "").strip()
    if not api_key:
        return []
    response = client.get(
        "https://api.clarivate.com/apis/wos-starter/v1/documents",
        headers={"X-ApiKey": api_key},
        params={"db": "WOS", "q": f'TS=("{species}")', "limit": min(limit, 50), "page": 1},
    )
    payload = response.json()
    records = payload.get("hits") or payload.get("documents") or payload.get("data") or []
    papers: list[Paper] = []
    for record in records:
        names = record.get("names", {}).get("authors") or record.get("authors") or []
        identifiers = record.get("identifiers") or {}
        source = record.get("source") or {}
        papers.append(
            Paper(
                title=clean_text(record.get("title")),
                abstract=clean_text(record.get("abstract")),
                year=clean_text(record.get("year") or source.get("publishYear")),
                journal=clean_text(source.get("sourceTitle") or record.get("journal")),
                authors=author_names(names),
                doi=normalize_doi(identifiers.get("doi", "")),
                sources={"WOS"},
                species={species},
            )
        )
    return papers


def parse_wos_plain_text(text: str, species: str) -> list[Paper]:
    """解析 WOS 官方 Plain text / savedrecs 格式。"""
    papers: list[Paper] = []
    for raw_record in re.split(r"\nER\s*(?:\n|$)", text.replace("\r\n", "\n")):
        fields: dict[str, list[str]] = {}
        current_tag = ""
        for line in raw_record.splitlines():
            match = re.match(r"^([A-Z0-9]{2}) (.*)$", line)
            if match:
                current_tag = match.group(1)
                fields.setdefault(current_tag, []).append(match.group(2).strip())
            elif line.startswith("   ") and current_tag and fields.get(current_tag):
                if current_tag == "AU":
                    fields[current_tag].append(line.strip())
                else:
                    fields[current_tag][-1] += " " + line.strip()
        title = clean_text(" ".join(fields.get("TI", [])))
        if not title:
            continue
        papers.append(Paper(
            title=title,
            abstract=clean_text(" ".join(fields.get("AB", []))),
            year=clean_text(" ".join(fields.get("PY", []))),
            journal=clean_text(" ".join(fields.get("SO", []))),
            authors=author_names(fields.get("AU", [])),
            doi=normalize_doi(" ".join(fields.get("DI", []))),
            pmid=clean_text(" ".join(fields.get("PM", []))),
            sources={"WOS"},
            species={species},
        ))
    return papers


def load_wos_exports(directory: Path) -> list[Paper]:
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    papers: list[Paper] = []
    for item in manifest.get("exports", []):
        path = directory / item["file"]
        if path.exists():
            papers.extend(parse_wos_plain_text(path.read_text(encoding="utf-8-sig"), item["species"]))
    return papers


def search_pubmed(client: Client, species: str, limit: int) -> list[Paper]:
    params: dict[str, Any] = {
        "db": "pubmed",
        "term": f'"{species}"[Title/Abstract]',
        "retmode": "json",
        "retmax": limit,
        "tool": "species-literature-downloader",
    }
    if os.getenv("NCBI_EMAIL"):
        params["email"] = os.environ["NCBI_EMAIL"]
    if os.getenv("NCBI_API_KEY"):
        params["api_key"] = os.environ["NCBI_API_KEY"]
    ids = client.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", params=params).json()["esearchresult"]["idlist"]
    if not ids:
        return []
    fetch_params = {"db": "pubmed", "id": ",".join(ids), "retmode": "xml", "tool": params["tool"]}
    if "email" in params:
        fetch_params["email"] = params["email"]
    root = ET.fromstring(client.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi", params=fetch_params).content)
    papers: list[Paper] = []
    for article in root.findall(".//PubmedArticle"):
        title = "".join(article.findtext(".//ArticleTitle", default=""))
        abstract = " ".join("".join(node.itertext()) for node in article.findall(".//Abstract/AbstractText"))
        # 只读取当前论文的标识符，不能使用 `.//ArticleId`，否则会误取参考文献 DOI。
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
    return papers


def search_crossref(client: Client, species: str, limit: int) -> list[Paper]:
    params = {"query.bibliographic": f'"{species}"', "rows": limit, "select": "DOI,title,abstract,author,published,container-title,type"}
    items = client.get("https://api.crossref.org/works", params=params).json()["message"]["items"]
    papers = []
    for item in items:
        if item.get("type") not in {"journal-article", "proceedings-article", "posted-content"}:
            continue
        title = clean_text(" ".join(item.get("title", [])))
        combined = f"{title} {clean_text(item.get('abstract'))}".casefold()
        if species.casefold() not in combined:
            continue
        date_parts = (item.get("published") or {}).get("date-parts") or [[]]
        papers.append(Paper(
            title=title, abstract=clean_text(item.get("abstract")),
            year=str(date_parts[0][0]) if date_parts and date_parts[0] else "",
            journal=clean_text(" ".join(item.get("container-title", []))),
            authors=author_names(item.get("author", [])), doi=normalize_doi(item.get("DOI", "")),
            sources={"Crossref"}, species={species},
        ))
    return papers


def search_europe_pmc(client: Client, species: str, limit: int) -> list[Paper]:
    """检索 Europe PMC 开放全文；查询范围包含全文，适合发现正文中的物种名。"""
    payload = client.get(
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        params={
            "query": f'"{species}" OPEN_ACCESS:Y',
            "format": "json",
            "pageSize": min(limit, 100),
            "resultType": "core",
        },
    ).json()
    papers = []
    for item in payload.get("resultList", {}).get("result", []):
        pmcid = clean_text(item.get("pmcid"))
        authors = [author.get("fullName", "") for author in (item.get("authorList") or {}).get("author", [])]
        candidates = []
        if pmcid and item.get("isOpenAccess") == "Y":
            candidates.append(PdfCandidate(f"https://europepmc.org/articles/{pmcid}?pdf=render", "Europe PMC"))
        papers.append(Paper(
            title=clean_text(item.get("title")), abstract=clean_text(item.get("abstractText")),
            year=clean_text(item.get("pubYear")), journal=clean_text(item.get("journalTitle")),
            authors=author_names(authors), doi=normalize_doi(item.get("doi", "")),
            pmid=clean_text(item.get("pmid")), pmcid=pmcid, sources={"Europe PMC"},
            species={species}, pdf_candidates=candidates,
        ))
    return papers


def search_semantic_scholar(client: Client, species: str, limit: int) -> list[Paper]:
    fields = "title,abstract,year,authors,venue,externalIds,openAccessPdf,url"
    headers = {"x-api-key": os.environ["S2_API_KEY"]} if os.getenv("S2_API_KEY") else {}
    data = client.get(
        "https://api.semanticscholar.org/graph/v1/paper/search",
        headers=headers, params={"query": species, "limit": min(limit, 100), "fields": fields},
    ).json().get("data", [])
    papers = []
    for item in data:
        combined = f"{item.get('title', '')} {item.get('abstract', '')}".casefold()
        if species.casefold() not in combined:
            continue
        ids = item.get("externalIds") or {}
        oa = item.get("openAccessPdf") or {}
        candidates = [PdfCandidate(oa["url"], "Semantic Scholar")] if oa.get("url") else []
        papers.append(Paper(
            title=clean_text(item.get("title")), abstract=clean_text(item.get("abstract")),
            year=str(item.get("year") or ""), journal=clean_text(item.get("venue")),
            authors=author_names(item.get("authors", [])), doi=normalize_doi(ids.get("DOI", "")),
            pmid=str(ids.get("PubMed", "")), sources={"Semantic Scholar"}, species={species},
            pdf_candidates=candidates,
        ))
    return papers


def add_unpaywall_candidates(client: Client, papers: Iterable[Paper], email: str) -> None:
    if not email:
        return
    for paper in papers:
        if not paper.doi:
            continue
        try:
            data = client.get(f"https://api.unpaywall.org/v2/{paper.doi}", params={"email": email}).json()
            locations = []
            if data.get("best_oa_location"):
                locations.append(data["best_oa_location"])
            locations.extend(data.get("oa_locations") or [])
            seen = {c.url for c in paper.pdf_candidates}
            for location in locations:
                url = location.get("url_for_pdf")
                if url and url not in seen:
                    paper.pdf_candidates.append(PdfCandidate(url, "Unpaywall"))
                    seen.add(url)
        except (requests.RequestException, ValueError):
            continue


def unique_papers(collection: dict[str, Paper]) -> list[Paper]:
    seen: set[int] = set()
    result = []
    for paper in collection.values():
        if id(paper) not in seen:
            seen.add(id(paper))
            result.append(paper)
    return sorted(result, key=lambda p: (p.year, p.title.casefold()), reverse=True)


def safe_filename(paper: Paper) -> str:
    stem = re.sub(r"[^\w.-]+", "_", paper.title, flags=re.UNICODE).strip("._")[:110] or "paper"
    digest = hashlib.sha1(paper.key.encode("utf-8")).hexdigest()[:8]
    return f"{stem}_{digest}.pdf"


def pdf_contains_species(path: Path, species_names: Iterable[str]) -> tuple[bool, str]:
    try:
        reader = PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages).casefold()
    except Exception as exc:
        return False, f"PDF 无法解析: {exc}"
    if not text.strip():
        return False, "PDF 没有可提取文本（可能是扫描件）"
    normalized = re.sub(r"\s+", " ", text)
    matched = [name for name in species_names if re.sub(r"\s+", " ", name.casefold()) in normalized]
    return (True, "命中: " + ", ".join(matched)) if matched else (False, "全文未出现目标物种名")


def download_pdfs(client: Client, papers: list[Paper], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="species-pdf-") as temp_dir:
        for index, paper in enumerate(papers, 1):
            destination = output_dir / safe_filename(paper)
            if destination.exists():
                matched, _ = pdf_contains_species(destination, paper.species)
                if matched:
                    paper.downloaded_path = str(destination)
                    print(f"  PDF [{index}/{len(papers)}]: 已存在，跳过 {paper.title[:55]}")
                    continue
            if not paper.pdf_candidates:
                paper.failure_reason = "未找到合法可直接下载的 PDF 链接"
                print(f"  PDF [{index}/{len(papers)}]: 无开放链接 {paper.title[:55]}")
                continue
            errors = []
            for candidate in paper.pdf_candidates:
                temp_path = Path(temp_dir) / f"candidate-{index}.pdf"
                print(f"  PDF [{index}/{len(papers)}]: 尝试 {candidate.source} — {paper.title[:45]}")
                try:
                    response = client.get(candidate.url, headers={"Accept": "application/pdf"}, stream=True)
                    content_type = response.headers.get("Content-Type", "").lower()
                    with temp_path.open("wb") as handle:
                        for chunk in response.iter_content(1024 * 128):
                            handle.write(chunk)
                    with temp_path.open("rb") as handle:
                        signature = handle.read(5)
                    if signature != b"%PDF-":
                        raise ValueError("链接返回的不是 PDF")
                    matched, reason = pdf_contains_species(temp_path, paper.species)
                    if not matched:
                        raise ValueError(reason)
                    shutil.move(str(temp_path), destination)
                    paper.downloaded_path = str(destination)
                    paper.failure_reason = ""
                    print(f"  PDF [{index}/{len(papers)}]: 成功")
                    break
                except (requests.RequestException, OSError, ValueError) as exc:
                    errors.append(f"{candidate.source}: {clean_text(exc)}")
                    temp_path.unlink(missing_ok=True)
            if not paper.downloaded_path:
                paper.failure_reason = "; ".join(errors)[:1000] or "所有候选链接均失败"
                print(f"  PDF [{index}/{len(papers)}]: 失败")


def citation(paper: Paper) -> str:
    authors = ", ".join(paper.authors) if paper.authors else "作者不详"
    year = paper.year or "年份不详"
    journal = f" {paper.journal}." if paper.journal else ""
    doi = f" doi:{paper.doi}." if paper.doi else ""
    title = paper.title.rstrip(".。")
    return f"{authors} ({year}). {title}.{journal}{doi}".strip()


def write_outputs(papers: list[Paper], abstracts_path: Path, summary_path: Path) -> None:
    with abstracts_path.open("w", encoding="utf-8") as handle:
        for i, paper in enumerate(papers, 1):
            handle.write(f"[{i}] {paper.title}\n")
            handle.write(f"Authors: {', '.join(paper.authors) or 'N/A'}\nYear: {paper.year or 'N/A'}\n")
            handle.write(f"Journal: {paper.journal or 'N/A'}\nDOI: {paper.doi or 'N/A'}\n")
            handle.write(f"Species: {', '.join(sorted(paper.species))}\nSources: {', '.join(sorted(paper.sources))}\n")
            handle.write(f"Abstract: {paper.abstract or 'N/A'}\n\n")
    downloaded = [p for p in papers if p.downloaded_path]
    failed = [p for p in papers if not p.downloaded_path]
    with summary_path.open("w", encoding="utf-8") as handle:
        handle.write(f"检索到论文: {len(papers)}\n成功下载: {len(downloaded)}\n下载失败: {len(failed)}\n\n")
        handle.write("downloaded:\n")
        for paper in downloaded:
            handle.write(f"- {citation(paper)} [PDF: {paper.downloaded_path}]\n")
        handle.write("\nfailedDownload:\n")
        for paper in failed:
            handle.write(f"- {citation(paper)} [原因: {paper.failure_reason}]\n")


def load_species(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"输入文件不存在: {path}")
    values = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        value = line.strip()
        if value and not value.startswith("#") and value not in values:
            values.append(value)
    if not values:
        raise ValueError("input.txt 中没有物种名")
    return values


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="input.txt", type=Path, help="物种名输入文件")
    parser.add_argument("--limit", default=20, type=int, help="每个数据源、每个物种最多返回条数")
    parser.add_argument("--output-dir", default="pdf_downloaded", type=Path)
    parser.add_argument("--metadata-only", action="store_true", help="只检索元数据，不下载 PDF")
    parser.add_argument("--wos-exports", default="wos_exports", type=Path,
                        help="由 wos_edge_export.py 生成的 WOS 导出目录")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.limit < 1 or args.limit > 100:
        print("错误：--limit 必须在 1 到 100 之间", file=sys.stderr)
        return 2
    try:
        species_names = load_species(args.input)
    except (OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    email = os.getenv("UNPAYWALL_EMAIL") or os.getenv("NCBI_EMAIL", "")
    client = Client(email)
    collection: dict[str, Paper] = {}
    try:
        imported = load_wos_exports(args.wos_exports)
        for paper in imported:
            add_paper(collection, paper)
        if imported:
            print(f"已导入 WOS Edge 导出记录：{len(imported)} 条")
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"WOS 导出目录读取失败：{clean_text(exc)}", file=sys.stderr)
    searchers = [
        ("WOS", search_wos),
        ("PubMed", search_pubmed),
        ("Europe PMC", search_europe_pmc),
        ("Crossref", search_crossref),
        ("Semantic Scholar", search_semantic_scholar),
    ]
    for species in species_names:
        print(f"\n检索物种：{species}")
        for source_name, searcher in searchers:
            try:
                results = searcher(client, species, args.limit)
                for paper in results:
                    add_paper(collection, paper)
                print(f"  {source_name}: {len(results)} 条")
            except (requests.RequestException, KeyError, ValueError, ET.ParseError) as exc:
                print(f"  {source_name}: 失败（{clean_text(exc)}）", file=sys.stderr)
    papers = unique_papers(collection)
    add_unpaywall_candidates(client, papers, os.getenv("UNPAYWALL_EMAIL", "").strip())
    if not args.metadata_only:
        download_pdfs(client, papers, args.output_dir)
    else:
        for paper in papers:
            paper.failure_reason = "本次使用了 --metadata-only，未尝试下载"
    write_outputs(papers, Path("abstracts.txt"), Path("summary.txt"))
    successes = sum(bool(p.downloaded_path) for p in papers)
    print(f"\n完成：检索到 {len(papers)} 篇，下载 {successes} 篇。详见 summary.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
