"""数据模型：论文、PDF 候选、检索结果。"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    # CNKI 等页面会在文本中混入零宽/双向控制等 Unicode Cf 字符。
    text = "".join(character for character in text if unicodedata.category(character) != "Cf")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_doi(doi: str) -> str:
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", str(doi).strip(), flags=re.I).lower()


def normalize_title(title: str) -> str:
    value = unicodedata.normalize("NFKD", str(title)).casefold()
    return "".join(character for character in value if character.isalnum())


def author_names(items: Iterable[Any]) -> list[str]:
    names: list[str] = []
    for item in items or []:
        try:
            if isinstance(item, str):
                name = clean_text(item)
            else:
                name = clean_text(
                    item.get("name")
                    or " ".join(filter(None, [item.get("given", ""), item.get("family", "")]))
                )
        except AttributeError:
            name = clean_text(item)
        if name and name not in names:
            names.append(name)
    return names


@dataclass
class PdfCandidate:
    url: str
    source: str  # scihub / unpaywall / pmc / europepmc / publisher / local
    priority: int = 3


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
    download_source: str = ""
    download_detail: str = ""
    failure_reason: str = ""

    @property
    def key(self) -> str:
        if self.doi:
            return "doi:" + normalize_doi(self.doi)
        if self.pmid:
            return "pmid:" + self.pmid
        return "title:" + normalize_title(self.title)

    def add_candidate(self, url: str, source: str, priority: int = 3) -> None:
        if url and all(c.url != url for c in self.pdf_candidates):
            self.pdf_candidates.append(PdfCandidate(url, source, priority))
        self.pdf_candidates.sort(key=lambda c: c.priority)


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
    for c in incoming.pdf_candidates:
        target.add_candidate(c.url, c.source, c.priority)


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


def unique_papers(collection: dict[str, Paper]) -> list[Paper]:
    """返回 alias 映射中的唯一论文对象，并保持首次出现顺序。"""
    seen: set[int] = set()
    papers: list[Paper] = []
    for paper in collection.values():
        marker = id(paper)
        if marker not in seen:
            seen.add(marker)
            papers.append(paper)
    return papers
