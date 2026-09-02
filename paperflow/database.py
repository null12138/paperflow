"""SQLite 持久化：关键词、论文、检索来源与下载历史。"""

from __future__ import annotations

import json
import csv
import re
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import Paper, clean_text, normalize_doi, normalize_title


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY,
    identity_key TEXT NOT NULL UNIQUE,
    doi TEXT NOT NULL DEFAULT '',
    pmid TEXT NOT NULL DEFAULT '',
    pmcid TEXT NOT NULL DEFAULT '',
    normalized_title TEXT NOT NULL,
    title TEXT NOT NULL,
    abstract TEXT NOT NULL DEFAULT '',
    year TEXT NOT NULL DEFAULT '',
    journal TEXT NOT NULL DEFAULT '',
    normalized_journal TEXT NOT NULL DEFAULT '',
    authors_json TEXT NOT NULL DEFAULT '[]',
    pdf_path TEXT NOT NULL DEFAULT '',
    download_source TEXT NOT NULL DEFAULT '',
    download_detail TEXT NOT NULL DEFAULT '',
    failure_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_doi
    ON papers(doi) WHERE doi <> '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_pmid
    ON papers(pmid) WHERE pmid <> '';
CREATE INDEX IF NOT EXISTS idx_papers_title ON papers(normalized_title);

CREATE TABLE IF NOT EXISTS keywords (
    id INTEGER PRIMARY KEY,
    keyword TEXT NOT NULL COLLATE NOCASE UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE
);

CREATE TABLE IF NOT EXISTS paper_keywords (
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    keyword_id INTEGER NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    PRIMARY KEY (paper_id, keyword_id)
);

CREATE TABLE IF NOT EXISTS paper_sources (
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    PRIMARY KEY (paper_id, source_id)
);

CREATE TABLE IF NOT EXISTS pdf_candidates (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    source TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (paper_id, url)
);

CREATE INDEX IF NOT EXISTS idx_pdf_candidates_paper
    ON pdf_candidates(paper_id, priority, id);

CREATE TABLE IF NOT EXISTS journal_metrics (
    id INTEGER PRIMARY KEY,
    normalized_journal TEXT NOT NULL,
    journal TEXT NOT NULL,
    year INTEGER NOT NULL,
    impact_factor REAL NOT NULL CHECK (impact_factor >= 0),
    source TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (normalized_journal, year, source)
);

CREATE INDEX IF NOT EXISTS idx_journal_metrics_lookup
    ON journal_metrics(normalized_journal, year DESC, id DESC);

CREATE TABLE IF NOT EXISTS download_attempts (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    success INTEGER NOT NULL CHECK (success IN (0, 1)),
    download_source TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    pdf_path TEXT NOT NULL DEFAULT '',
    attempted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_download_attempts_paper
    ON download_attempts(paper_id, attempted_at DESC);
"""


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        value = clean_text(value)
        if value and value not in result:
            result.append(value)
    return result


class PaperDatabase:
    def __init__(self, path: str | Path = "paperflow.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)
        self._migrate_schema()

    def clear_all(self) -> dict[str, int]:
        """清空全部业务数据但保留 schema；调用方应在此之前完成备份。"""
        tables = (
            "download_attempts", "pdf_candidates", "paper_keywords", "paper_sources",
            "papers", "keywords", "sources", "journal_metrics",
        )
        counts = {
            table: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
        with self.connection:
            for table in tables:
                self.connection.execute(f"DELETE FROM {table}")
            # 让新数据从干净的自增 ID 开始；不影响表结构或索引。
            try:
                self.connection.execute("DELETE FROM sqlite_sequence WHERE name IN ({})".format(
                    ",".join("?" for _ in tables)
                ), tables)
            except sqlite3.OperationalError:
                # SQLite 只有在使用 AUTOINCREMENT 时才创建 sqlite_sequence。
                pass
        return counts

    def _migrate_schema(self) -> None:
        """原位补齐旧数据库字段，并为已有期刊建立规范名。"""
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(papers)")}
        if "normalized_journal" not in columns:
            self.connection.execute(
                "ALTER TABLE papers ADD COLUMN normalized_journal TEXT NOT NULL DEFAULT ''"
            )
        rows = self.connection.execute(
            "SELECT id, journal FROM papers WHERE journal <> '' AND normalized_journal = ''"
        ).fetchall()
        self.connection.executemany(
            "UPDATE papers SET normalized_journal = ? WHERE id = ?",
            [(normalize_title(row["journal"]), row["id"]) for row in rows],
        )
        self.connection.commit()

    def __enter__(self) -> "PaperDatabase":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        self.connection.close()

    def close(self) -> None:
        self.connection.commit()
        self.connection.close()

    def _find_paper(self, paper: Paper) -> sqlite3.Row | None:
        doi = normalize_doi(paper.doi)
        checks = (("doi", doi), ("pmid", paper.pmid.strip()), ("pmcid", paper.pmcid.strip()))
        for column, value in checks:
            if value:
                row = self.connection.execute(f"SELECT * FROM papers WHERE {column} = ?", (value,)).fetchone()
                if row:
                    return row
        normalized = normalize_title(paper.title)
        if normalized:
            if doi:
                return self.connection.execute(
                    """SELECT * FROM papers
                       WHERE normalized_title = ? AND (doi = '' OR doi = ?)
                       ORDER BY CASE WHEN doi = ? THEN 0 ELSE 1 END, id
                       LIMIT 1""",
                    (normalized, doi, doi),
                ).fetchone()
            return self.connection.execute(
                "SELECT * FROM papers WHERE normalized_title = ? ORDER BY id LIMIT 1", (normalized,)
            ).fetchone()
        return None

    def upsert_paper(self, paper: Paper, keywords: Iterable[str] = ()) -> int:
        """写入或合并一篇论文，返回 papers.id；关联表只增不减。"""
        paper.doi = normalize_doi(paper.doi)
        normalized = normalize_title(paper.title) or clean_text(paper.title).casefold()
        normalized_journal = normalize_title(paper.journal)
        row = self._find_paper(paper)
        if row:
            doi_placeholder = bool(paper.doi and normalize_doi(paper.title) == paper.doi)
            if doi_placeholder:
                normalized = row["normalized_title"]
            old_authors = json.loads(row["authors_json"] or "[]")
            authors = _unique([*old_authors, *paper.authors])
            identity_key = row["identity_key"]
            preferred_key = paper.key
            if identity_key.startswith("title:") and preferred_key.startswith(("doi:", "pmid:")):
                collision = self.connection.execute(
                    "SELECT id FROM papers WHERE identity_key = ? AND id <> ?", (preferred_key, row["id"])
                ).fetchone()
                if not collision:
                    identity_key = preferred_key
            old_title = clean_text(row["title"])
            old_abstract = clean_text(row["abstract"])
            abstract = paper.abstract if len(paper.abstract) > len(old_abstract) else old_abstract
            pdf_path = paper.downloaded_path or row["pdf_path"]
            download_source = paper.download_source or row["download_source"]
            download_detail = paper.download_detail or row["download_detail"]
            failure_reason = "" if pdf_path else (paper.failure_reason or row["failure_reason"])
            self.connection.execute(
                """UPDATE papers SET
                       identity_key = ?, doi = ?, pmid = ?, pmcid = ?, normalized_title = ?,
                       title = ?, abstract = ?, year = ?, journal = ?, authors_json = ?,
                       normalized_journal = ?,
                       pdf_path = ?, download_source = ?, download_detail = ?, failure_reason = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (
                    identity_key,
                    paper.doi or row["doi"],
                    paper.pmid or row["pmid"],
                    paper.pmcid or row["pmcid"],
                    normalized or row["normalized_title"],
                    paper.title if not doi_placeholder and len(paper.title) >= len(old_title) else old_title,
                    abstract,
                    paper.year or row["year"],
                    paper.journal or row["journal"],
                    json.dumps(authors, ensure_ascii=False),
                    normalized_journal or row["normalized_journal"],
                    pdf_path,
                    download_source,
                    download_detail,
                    failure_reason,
                    row["id"],
                ),
            )
            paper_id = int(row["id"])
        else:
            cursor = self.connection.execute(
                """INSERT INTO papers(
                       identity_key, doi, pmid, pmcid, normalized_title, title, abstract,
                       year, journal, normalized_journal, authors_json, pdf_path, download_source,
                       download_detail, failure_reason
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    paper.key,
                    paper.doi,
                    paper.pmid,
                    paper.pmcid,
                    normalized,
                    paper.title,
                    paper.abstract,
                    paper.year,
                    paper.journal,
                    normalized_journal,
                    json.dumps(_unique(paper.authors), ensure_ascii=False),
                    paper.downloaded_path,
                    paper.download_source,
                    paper.download_detail,
                    paper.failure_reason,
                ),
            )
            paper_id = int(cursor.lastrowid)

        for keyword in _unique([*keywords, *paper.species]):
            self.connection.execute("INSERT OR IGNORE INTO keywords(keyword) VALUES (?)", (keyword,))
            self.connection.execute(
                """INSERT OR IGNORE INTO paper_keywords(paper_id, keyword_id)
                   SELECT ?, id FROM keywords WHERE keyword = ? COLLATE NOCASE""",
                (paper_id, keyword),
            )
        for source in _unique(paper.sources):
            self.connection.execute("INSERT OR IGNORE INTO sources(name) VALUES (?)", (source,))
            self.connection.execute(
                """INSERT OR IGNORE INTO paper_sources(paper_id, source_id)
                   SELECT ?, id FROM sources WHERE name = ? COLLATE NOCASE""",
                (paper_id, source),
            )
        for candidate in paper.pdf_candidates:
            url = clean_text(candidate.url)
            source = clean_text(candidate.source).casefold()
            if not url or not source:
                continue
            self.connection.execute(
                """INSERT INTO pdf_candidates(paper_id, url, source, priority)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(paper_id, url) DO UPDATE SET
                       source = excluded.source,
                       priority = MIN(pdf_candidates.priority, excluded.priority),
                       updated_at = CURRENT_TIMESTAMP""",
                (paper_id, url, source, int(candidate.priority)),
            )
        return paper_id

    def save_papers(self, papers: Iterable[Paper], keywords: Iterable[str] = ()) -> int:
        count = 0
        with self.connection:
            for paper in papers:
                self.upsert_paper(paper, keywords)
                count += 1
        return count

    def save_download(
        self,
        paper: Paper,
        success: bool,
        detail: str = "",
        keywords: Iterable[str] = (),
    ) -> int:
        paper.download_detail = detail or paper.download_detail
        paper_id = self.upsert_paper(paper, keywords)
        # Each attempt is authoritative: a failed retry must not leave a stale
        # PDF path/source from an earlier run looking like a current success.
        if not success:
            self.connection.execute(
                "UPDATE papers SET pdf_path = '', download_source = '', download_detail = ?, failure_reason = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (detail or paper.failure_reason or "下载失败（未提供原因）", detail or paper.failure_reason or "下载失败（未提供原因）", paper_id),
            )
        else:
            self.connection.execute(
                "UPDATE papers SET failure_reason = '', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (paper_id,),
            )
        self.connection.execute(
            """INSERT INTO download_attempts(
                   paper_id, success, download_source, detail, pdf_path
               ) VALUES (?, ?, ?, ?, ?)""",
            (
                paper_id,
                int(success),
                paper.download_source,
                detail,
                paper.downloaded_path,
            ),
        )
        self.connection.commit()
        return paper_id

    def record_download_start(self, paper: Paper, detail: str = "下载任务已开始") -> int:
        """Persist queue start immediately, before network work begins."""
        paper_id = self.upsert_paper(paper)
        self.connection.execute(
            "INSERT INTO download_attempts(paper_id, success, download_source, detail, pdf_path) VALUES (?, 0, '', ?, '')",
            (paper_id, detail),
        )
        self.connection.commit()
        return paper_id

    def stats(self) -> dict[str, int]:
        queries = {
            "papers": "SELECT COUNT(*) FROM papers",
            "keywords": "SELECT COUNT(*) FROM keywords",
            "sources": "SELECT COUNT(*) FROM sources",
            "downloaded": "SELECT COUNT(*) FROM papers WHERE pdf_path <> ''",
            "download_attempts": "SELECT COUNT(*) FROM download_attempts",
            "pdf_candidates": "SELECT COUNT(*) FROM pdf_candidates",
            "journal_metrics": "SELECT COUNT(*) FROM journal_metrics",
            "papers_with_impact_factor": """SELECT COUNT(*) FROM papers p WHERE EXISTS (
                SELECT 1 FROM journal_metrics jm
                WHERE jm.normalized_journal = p.normalized_journal)""",
        }
        return {name: int(self.connection.execute(sql).fetchone()[0]) for name, sql in queries.items()}

    def list_papers(
        self,
        keyword: str = "",
        limit: int = 20,
        source: str = "",
        status: str = "",
        min_if: float | None = None,
        max_if: float | None = None,
    ) -> list[sqlite3.Row]:
        params: list[object] = []
        conditions: list[str] = []
        if keyword:
            conditions.append("EXISTS (SELECT 1 FROM paper_keywords pk2 JOIN keywords k2 ON k2.id = pk2.keyword_id WHERE pk2.paper_id = p.id AND k2.keyword = ? COLLATE NOCASE)")
            params.append(keyword)
        if source:
            conditions.append("EXISTS (SELECT 1 FROM paper_sources ps2 JOIN sources s2 ON s2.id = ps2.source_id WHERE ps2.paper_id = p.id AND s2.name = ? COLLATE NOCASE)")
            params.append(source)
        if status == "pending":
            conditions.extend(("p.pdf_path = ''", "NOT EXISTS (SELECT 1 FROM download_attempts da WHERE da.paper_id = p.id)"))
        elif status == "failed":
            conditions.extend(("p.pdf_path = ''", "EXISTS (SELECT 1 FROM download_attempts da WHERE da.paper_id = p.id)"))
        elif status == "all":
            conditions.append("p.pdf_path = ''")
        elif status == "downloaded":
            conditions.append("p.pdf_path <> ''")
        elif status == "candidate":
            conditions.extend((
                "p.pdf_path = ''",
                "EXISTS (SELECT 1 FROM pdf_candidates pc WHERE pc.paper_id = p.id "
                "AND lower(pc.source) NOT IN ('publisher', 'cnki'))",
            ))
        elif status == "candidate-pending":
            conditions.extend((
                "p.pdf_path = ''",
                "EXISTS (SELECT 1 FROM pdf_candidates pc WHERE pc.paper_id = p.id "
                "AND lower(pc.source) NOT IN ('publisher', 'cnki'))",
                "NOT EXISTS (SELECT 1 FROM download_attempts da WHERE da.paper_id = p.id)",
            ))
        elif status == "candidate-pmc-pending":
            conditions.extend((
                "p.pdf_path = ''",
                "EXISTS (SELECT 1 FROM pdf_candidates pc WHERE pc.paper_id = p.id AND "
                "(lower(pc.url) LIKE '%europepmc.org%' OR lower(pc.url) LIKE '%pmc.ncbi.nlm.nih.gov%'))",
                "NOT EXISTS (SELECT 1 FROM download_attempts da WHERE da.paper_id = p.id)",
            ))
        if min_if is not None:
            conditions.append("jm.impact_factor >= ?")
            params.append(float(min_if))
        if max_if is not None:
            conditions.append("jm.impact_factor <= ?")
            params.append(float(max_if))
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        # 0 表示不设本地上限；SQLite 的 LIMIT -1 会返回全部结果。
        params.append(-1 if limit == 0 else max(1, int(limit)))
        return self.connection.execute(
            f"""SELECT p.*,
                       GROUP_CONCAT(DISTINCT s.name) AS sources,
                       GROUP_CONCAT(DISTINCT k.keyword) AS keywords,
                       jm.impact_factor, jm.year AS impact_factor_year,
                       jm.source AS impact_factor_source
                FROM papers p
                LEFT JOIN journal_metrics jm ON jm.id = (
                    SELECT jm2.id FROM journal_metrics jm2
                    WHERE jm2.normalized_journal = p.normalized_journal
                    ORDER BY jm2.year DESC, jm2.id DESC LIMIT 1
                )
                LEFT JOIN paper_sources ps ON ps.paper_id = p.id
                LEFT JOIN sources s ON s.id = ps.source_id
                LEFT JOIN paper_keywords pk ON pk.paper_id = p.id
                LEFT JOIN keywords k ON k.id = pk.keyword_id
                {where}
                GROUP BY p.id
                ORDER BY p.year DESC, p.id DESC
                LIMIT ?""",
            params,
        ).fetchall()

    def load_papers_for_download(
        self,
        keyword: str = "",
        source: str = "",
        status: str = "pending",
        limit: int = 100,
        min_if: float | None = None,
        max_if: float | None = None,
    ) -> list[Paper]:
        """从持久化队列恢复完整 Paper，包括来源、关键词和 PDF 候选。"""
        rows = self.list_papers(keyword, limit, source, status, min_if, max_if)
        papers: list[Paper] = []
        for row in rows:
            paper = Paper(
                title=row["title"], abstract=row["abstract"], year=row["year"],
                journal=row["journal"], authors=json.loads(row["authors_json"] or "[]"),
                doi=row["doi"], pmid=row["pmid"], pmcid=row["pmcid"],
                sources=set(filter(None, (row["sources"] or "").split(","))),
                species=set(filter(None, (row["keywords"] or "").split(","))),
                downloaded_path=row["pdf_path"], download_source=row["download_source"],
                download_detail=row["download_detail"], failure_reason=row["failure_reason"],
            )
            candidates = self.connection.execute(
                "SELECT url, source, priority FROM pdf_candidates WHERE paper_id = ? ORDER BY priority, id",
                (row["id"],),
            ).fetchall()
            for candidate in candidates:
                paper.add_candidate(candidate["url"], candidate["source"], candidate["priority"])
            papers.append(paper)
        return papers

    def import_impact_factors(
        self,
        path: Path,
        source: str = "JCR",
        default_year: int | None = None,
    ) -> dict[str, int]:
        """导入合法取得的 JIF CSV/TSV；按期刊规范名自动关联现有和未来论文。"""
        text = path.read_text(encoding="utf-8-sig")
        delimiter = "\t" if path.suffix.casefold() in {".tsv", ".txt"} else ","
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",\t;").delimiter
        except csv.Error:
            pass
        reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
        headers = {normalize_title(name): name for name in (reader.fieldnames or [])}

        def header(*names: str) -> str:
            return next((headers[normalize_title(name)] for name in names if normalize_title(name) in headers), "")

        journal_key = header("journal", "journal_name", "full journal title", "期刊", "期刊名称")
        factor_key = header(
            "impact_factor", "impact factor", "journal impact factor", "jif", "if", "影响因子"
        )
        year_key = header("year", "jcr year", "年份")
        if not journal_key or not factor_key or (not year_key and default_year is None):
            raise ValueError("JIF 文件至少需要 journal、impact_factor，以及 year 列（或 --year）")

        imported = skipped = 0
        with self.connection:
            for item in reader:
                journal = clean_text(item.get(journal_key))
                raw_factor = clean_text(item.get(factor_key)).replace(",", "")
                raw_year = clean_text(item.get(year_key)) if year_key else str(default_year or "")
                try:
                    factor = float(raw_factor)
                    year = int(float(raw_year))
                    if factor < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    skipped += 1
                    continue
                normalized = normalize_title(journal)
                if not normalized:
                    skipped += 1
                    continue
                self.connection.execute(
                    """INSERT INTO journal_metrics(
                           normalized_journal, journal, year, impact_factor, source
                       ) VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(normalized_journal, year, source) DO UPDATE SET
                           journal = excluded.journal,
                           impact_factor = excluded.impact_factor,
                           imported_at = CURRENT_TIMESTAMP""",
                    (normalized, journal, year, factor, clean_text(source) or "JCR"),
                )
                imported += 1
        matched = int(self.connection.execute(
            """SELECT COUNT(*) FROM papers p WHERE EXISTS (
                   SELECT 1 FROM journal_metrics jm
                   WHERE jm.normalized_journal = p.normalized_journal
               )"""
        ).fetchone()[0])
        return {"imported": imported, "skipped": skipped, "matched_papers": matched}

    def deduplicate(self, dry_run: bool = False) -> dict[str, int]:
        """保守合并重复题名；冲突 DOI/PMID 的组只报告、不合并。"""
        groups = self.connection.execute(
            """SELECT normalized_title
               FROM papers
               WHERE normalized_title <> ''
               GROUP BY normalized_title
               HAVING COUNT(*) > 1"""
        ).fetchall()
        merged_groups = removed = skipped = 0
        for group in groups:
            rows = self.connection.execute(
                "SELECT * FROM papers WHERE normalized_title = ? ORDER BY id",
                (group["normalized_title"],),
            ).fetchall()
            dois = {row["doi"] for row in rows if row["doi"]}
            pmids = {row["pmid"] for row in rows if row["pmid"]}
            pmcids = {row["pmcid"] for row in rows if row["pmcid"]}
            if len(dois) > 1 or len(pmids) > 1 or len(pmcids) > 1:
                skipped += 1
                continue
            merged_groups += 1
            removed += len(rows) - 1
            if dry_run:
                continue
            survivor = max(
                rows,
                key=lambda row: (
                    bool(row["pdf_path"]), bool(row["doi"]), bool(row["pmid"]),
                    len(row["abstract"]), -int(row["id"]),
                ),
            )
            duplicates = [row for row in rows if row["id"] != survivor["id"]]
            merged = dict(survivor)
            authors = json.loads(merged["authors_json"] or "[]")
            for duplicate in duplicates:
                authors = _unique([*authors, *json.loads(duplicate["authors_json"] or "[]")])
                for field in ("doi", "pmid", "pmcid", "year", "journal", "pdf_path", "download_source", "download_detail"):
                    if not merged[field] and duplicate[field]:
                        merged[field] = duplicate[field]
                if len(duplicate["title"]) > len(merged["title"]):
                    merged["title"] = duplicate["title"]
                if len(duplicate["abstract"]) > len(merged["abstract"]):
                    merged["abstract"] = duplicate["abstract"]
                if not merged["failure_reason"] and duplicate["failure_reason"]:
                    merged["failure_reason"] = duplicate["failure_reason"]
                self.connection.execute(
                    """INSERT OR IGNORE INTO paper_keywords(paper_id, keyword_id)
                       SELECT ?, keyword_id FROM paper_keywords WHERE paper_id = ?""",
                    (survivor["id"], duplicate["id"]),
                )
                self.connection.execute(
                    """INSERT OR IGNORE INTO paper_sources(paper_id, source_id)
                       SELECT ?, source_id FROM paper_sources WHERE paper_id = ?""",
                    (survivor["id"], duplicate["id"]),
                )
                self.connection.execute(
                    """INSERT OR IGNORE INTO pdf_candidates(paper_id, url, source, priority)
                       SELECT ?, url, source, priority FROM pdf_candidates WHERE paper_id = ?""",
                    (survivor["id"], duplicate["id"]),
                )
                self.connection.execute(
                    "UPDATE download_attempts SET paper_id = ? WHERE paper_id = ?",
                    (survivor["id"], duplicate["id"]),
                )
                self.connection.execute("DELETE FROM papers WHERE id = ?", (duplicate["id"],))

            identity_key = (
                f"doi:{merged['doi']}" if merged["doi"] else
                f"pmid:{merged['pmid']}" if merged["pmid"] else
                f"title:{merged['normalized_title']}"
            )
            self.connection.execute(
                """UPDATE papers SET
                       identity_key = ?, doi = ?, pmid = ?, pmcid = ?, title = ?, abstract = ?,
                       year = ?, journal = ?, authors_json = ?, pdf_path = ?, download_source = ?,
                       normalized_journal = ?, download_detail = ?, failure_reason = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (
                    identity_key, merged["doi"], merged["pmid"], merged["pmcid"], merged["title"],
                    merged["abstract"], merged["year"], merged["journal"],
                    json.dumps(authors, ensure_ascii=False), merged["pdf_path"],
                    merged["download_source"], normalize_title(merged["journal"]), merged["download_detail"],
                    "" if merged["pdf_path"] else merged["failure_reason"], survivor["id"],
                ),
            )
        if not dry_run:
            self.connection.commit()
        return {"groups": merged_groups, "removed": removed, "skipped_conflicts": skipped}

    def import_legacy(self, abstracts_path: Path, summary_path: Path | None = None) -> dict[str, int]:
        papers = parse_abstracts_file(abstracts_path)
        self.save_papers(papers)
        matched_paths = self._import_summary_paths(summary_path) if summary_path and summary_path.exists() else 0
        return {"papers": len(papers), "pdf_paths": matched_paths}

    def _import_summary_paths(self, path: Path) -> int:
        matched = 0
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "[PDF:" not in line:
                continue
            doi_match = re.search(r"doi:(.*?)\. \[PDF:", line)
            path_match = re.search(r"\[PDF:\s*(.*?)\]\s*$", line)
            if not path_match:
                continue
            row = None
            if doi_match:
                doi = normalize_doi(doi_match.group(1))
                row = self.connection.execute("SELECT id FROM papers WHERE doi = ?", (doi,)).fetchone()
            else:
                citation = line.split("[PDF:", 1)[0].casefold()
                candidates = self.connection.execute(
                    "SELECT id, title FROM papers WHERE doi = '' ORDER BY LENGTH(title) DESC"
                ).fetchall()
                row = next(
                    (candidate for candidate in candidates
                     if f"). {candidate['title'].rstrip('.。')}".casefold() in citation),
                    None,
                )
            if not row:
                continue
            pdf_path = path_match.group(1).strip()
            source = "legacy"
            low = pdf_path.casefold()
            if "unpaywall" in low:
                source = "oa"
            elif "scihub" in low or "sci-hub" in low:
                source = "scihub"
            self.connection.execute(
                """UPDATE papers SET pdf_path = ?, download_source = ?,
                       download_detail = ?, failure_reason = '', updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (pdf_path, source, f"imported from {path.name}", row["id"]),
            )
            detail = f"imported from {path.name}"
            exists = self.connection.execute(
                """SELECT 1 FROM download_attempts
                   WHERE paper_id = ? AND success = 1 AND download_source = ?
                     AND detail = ? AND pdf_path = ?""",
                (row["id"], source, detail, pdf_path),
            ).fetchone()
            if not exists:
                self.connection.execute(
                    """INSERT INTO download_attempts(
                           paper_id, success, download_source, detail, pdf_path
                       ) VALUES (?, 1, ?, ?, ?)""",
                    (row["id"], source, detail, pdf_path),
                )
            matched += 1
        self.connection.commit()
        return matched


def parse_abstracts_file(path: Path) -> list[Paper]:
    """解析 paperflow 既有 abstracts.txt，供一次性迁移使用。"""
    text = path.read_text(encoding="utf-8-sig")
    starts = list(re.finditer(r"(?m)^\[(\d+)\] (.*)$", text))
    papers: list[Paper] = []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        block = text[match.end():end]

        def field(name: str) -> str:
            found = re.search(rf"(?m)^{re.escape(name)}:\s*(.*)$", block)
            return clean_text(found.group(1)) if found else ""

        title = clean_text(match.group(2))
        if not title:
            continue
        authors_text = field("Authors")
        sources = {item.strip() for item in field("Sources").split(",") if item.strip() and item != "N/A"}
        species = {item.strip() for item in field("Species").split(",") if item.strip() and item != "N/A"}
        papers.append(Paper(
            title=title,
            abstract=field("Abstract") if field("Abstract") != "N/A" else "",
            year=field("Year") if field("Year") != "N/A" else "",
            journal=field("Journal") if field("Journal") != "N/A" else "",
            authors=[] if not authors_text or authors_text == "N/A" else [authors_text],
            doi=field("DOI") if field("DOI") != "N/A" else "",
            sources=sources,
            species=species,
        ))
    return papers
