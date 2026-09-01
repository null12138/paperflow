"""可供 CLI 与 TUI 共用的长任务工作流。"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable

from . import net
from .database import PaperDatabase
from .models import Paper, add_paper, clean_text, normalize_title, unique_papers
from .sources import SOURCES


Progress = Callable[[str], None]


def _emit(progress: Progress | None, message: str) -> None:
    if progress:
        progress(message)


def search_to_database(
    species_names: Iterable[str],
    source_names: Iterable[str],
    limit: int,
    email: str,
    db_path: Path,
    progress: Progress | None = None,
) -> list[Paper]:
    """检索多个关键词和来源，去重后持久化；不触发 PDF 下载。"""
    client = net.make_session(email=email)
    collection: dict[str, Paper] = {}
    sources = SOURCES.ordered(list(source_names))
    for species in species_names:
        _emit(progress, f"检索关键词：{species}")
        def run_one(source):
            try:
                results = source.search_species(client, species, limit)
                return source, results, None
            except NotImplementedError as exc:
                return source, [], f"- {source.name}: 跳过（{clean_text(exc)[:80]}）"
            except Exception as exc:
                return source, [], f"✗ {source.name}: {clean_text(exc)[:120]}"
        # 每个关键词下各数据源并行；同一源自身仍负责限速，避免互相拖慢。
        with ThreadPoolExecutor(max_workers=max(1, min(6, len(sources)))) as pool:
            futures = [pool.submit(run_one, source) for source in sources]
            for future in as_completed(futures):
                source, results, error = future.result()
                for paper in results:
                    add_paper(collection, paper)
                _emit(progress, error or f"  ✓ {source.name}: {len(results)} 条")
    papers = sorted(unique_papers(collection), key=lambda paper: (paper.title.casefold(), paper.year))
    with PaperDatabase(db_path) as database:
        database.save_papers(papers)
    _emit(progress, f"完成：去重后 {len(papers)} 篇，已写入 {db_path}")
    return papers


def _load_wos_manifest(path: Path) -> dict:
    if not path.is_file():
        return {"version": 1, "queries": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data.get("queries"), dict):
            raise ValueError
        return data
    except (OSError, ValueError, TypeError):
        raise ValueError(f"WOS 断点清单无法解析: {path}") from None


def _save_wos_manifest(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def fetch_wos_to_database(
    keywords: Iterable[str],
    max_records: int,
    db_path: Path,
    manifest_path: Path = Path("wos_api_runs/manifest.json"),
    request_interval: float = 1.0,
    resume: bool = True,
    progress: Progress | None = None,
) -> dict[str, int]:
    """WOS Starter API 专用批量任务：逐页入库，入库后才推进断点。"""
    from .sources.wos import WosSource

    source = SOURCES.get("WOS")
    if not isinstance(source, WosSource):
        raise RuntimeError("WOS 数据源注册异常")
    if not source.api_key():
        raise RuntimeError("WOS_API_KEY 未配置；请写入本机 .env")
    manifest = _load_wos_manifest(manifest_path) if resume else {"version": 1, "queries": {}}
    client = net.make_session()
    total_saved = total_requests = 0
    with PaperDatabase(db_path) as database:
        for raw_keyword in keywords:
            keyword = clean_text(raw_keyword)
            if not keyword:
                continue
            state = manifest["queries"].get(keyword, {}) if resume else {}
            if state.get("database") != str(db_path.resolve()):
                state = {}
            start_record = max(0, int(state.get("fetched", 0) or 0))
            known_total = max(0, int(state.get("total", 0) or 0))
            if max_records == 0 and state.get("status") == "complete" and start_record >= known_total:
                _emit(progress, f"WOS {keyword}：断点已完成 {start_record} 条，跳过")
                continue
            if max_records > 0 and start_record >= max_records:
                _emit(progress, f"WOS {keyword}：断点已有 {start_record} 条，达到目标，跳过")
                continue
            _emit(progress, f"WOS {keyword}：从第 {start_record + 1} 条开始")
            for page in source.iter_species_pages(
                client, keyword, max_records, start_record=start_record,
                request_interval=max(0.0, request_interval),
            ):
                database.save_papers(page.papers)
                total_saved += len(page.papers)
                total_requests += 1
                status = "complete" if page.record_end >= page.total else "in_progress"
                if max_records > 0 and page.record_end >= max_records and page.record_end < page.total:
                    status = "limited"
                manifest["queries"][keyword] = {
                    "database": str(db_path.resolve()),
                    "total": page.total,
                    "fetched": page.record_end,
                    "last_page": page.page,
                    "status": status,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                _save_wos_manifest(manifest_path, manifest)
                quota = f"，今日剩余请求 {page.remaining_day}" if page.remaining_day is not None else ""
                _emit(
                    progress,
                    f"  ✓ 第 {page.page} 页：本次 {len(page.papers)} 条，"
                    f"已处理 {page.record_end}/{page.total}{quota}",
                )
    _emit(progress, f"WOS 批量获取完成：新处理 {total_saved} 条，{total_requests} 次 API 请求")
    return {"saved": total_saved, "requests": total_requests}


def download_database_queue(
    db_path: Path,
    out_dir: Path,
    mode: str,
    rpm: int,
    email: str,
    keyword: str = "",
    source: str = "",
    status: str = "pending",
    limit: int = 100,
    min_if: float | None = None,
    max_if: float | None = None,
    progress: Progress | None = None,
) -> dict[str, int]:
    """从 SQLite 恢复论文队列并下载，逐条把结果写回同一数据库。"""
    from .pdf import PdfEngine

    engine = PdfEngine(
        out_dir=out_dir,
        email=email,
        max_per_minute=rpm,
        cookie_file=Path("scihub_cookies.json"),
        use_scihub="scihub" in mode,
        use_oa="oa" in mode,
        use_publisher="publisher" in mode,
        use_cnki="cnki" in mode,
        use_direct_candidates="direct" in mode or "oa" in mode,
        pmc_only="pmc" in mode,
    )
    with PaperDatabase(db_path) as database:
        papers = database.load_papers_for_download(
            keyword=keyword,
            source=source,
            status=status,
            limit=limit,
            min_if=min_if,
            max_if=max_if,
        )
        _emit(progress, f"数据库下载队列：{len(papers)} 篇")
        ok_count = fail_count = 0
        tokens = {part.strip() for part in mode.replace("+", ",").split(",") if part.strip()}
        # Europe PMC 对过高并发会显著变慢甚至超时；8 路实测吞吐更稳定。
        workers = 8 if tokens and tokens <= {"direct", "oa", "pmc"} else 1

        def fetch_one(paper: Paper):
            ok, info = engine.fetch(paper)
            return paper, ok, info

        with ThreadPoolExecutor(max_workers=min(workers, max(1, len(papers)))) as pool:
            futures = [pool.submit(fetch_one, paper) for paper in papers]
            for index, future in enumerate(as_completed(futures), 1):
                paper, ok, info = future.result()
                database.save_download(paper, ok, info)
                if ok:
                    ok_count += 1
                    _emit(progress, f"[{index}/{len(papers)}] ✓ {paper.title[:60]} | {info[:100]}")
                else:
                    fail_count += 1
                    _emit(progress, f"[{index}/{len(papers)}] ✗ {paper.title[:60]} | {info[:120]}")
    _emit(progress, f"下载完成：成功 {ok_count}，失败 {fail_count}")
    return {"total": len(papers), "success": ok_count, "failed": fail_count}


def preflight_download_candidates(
    db_path: Path,
    email: str = "",
    limit: int = 100,
    keyword: str = "",
    source: str = "",
    progress: Progress | None = None,
) -> dict[str, int]:
    """批量解析 DOI 的 OpenAlex OA 候选和摘要并写回 SQLite，不下载文件。"""
    from .pdf.oa import OaEngine

    oa = OaEngine(email=email, proxies=net.DEFAULT_PROXIES)
    with PaperDatabase(db_path) as database:
        papers = database.load_papers_for_download(
            keyword=keyword, source=source, status="all", limit=limit
        )
        doi_papers = [paper for paper in papers if paper.doi]
        parsed = with_candidates = failed = enriched_abstracts = 0
        openalex_available = True
        if doi_papers:
            try:
                oa.bulk_openalex([doi_papers[0].doi])
            except Exception:
                openalex_available = False
        provider = "openalex" if openalex_available else ("s2" if oa.s2_api_key else "unpaywall")
        batch_size = 20 if provider != "s2" else 100
        batches = [doi_papers[i:i + batch_size] for i in range(0, len(doi_papers), batch_size)]
        if not openalex_available:
            _emit(progress, f"OpenAlex 当前限流，自动切换 {provider} 批量解析")

        def resolve_batch(batch: list[Paper]):
            if provider == "openalex":
                try:
                    return batch, oa.bulk_openalex(paper.doi for paper in batch), "openalex"
                except Exception:
                    pass
            if provider == "s2":
                try:
                    return batch, oa.bulk_s2(paper.doi for paper in batch), "s2"
                except Exception:
                    pass
            # OpenAlex 匿名出口可能 429；逐 DOI 使用 Unpaywall 合法 OA 数据回退。
            resolved = {}
            for paper in batch:
                urls = oa._unpaywall_candidates(paper.doi)
                resolved[paper.doi.lower()] = {"abstract": "", "candidates": urls}
            return batch, resolved, "unpaywall"

        # 每个请求包含最多 20 个 DOI；4 路受控并发比逐 DOI 请求快一个数量级。
        workers = 1 if provider == "s2" else min(4, max(1, len(batches)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(resolve_batch, batch): batch for batch in batches}
            for batch_index, future in enumerate(as_completed(futures), 1):
                try:
                    batch, resolved, provider = future.result()
                except Exception as exc:
                    batch = futures[future]
                    failed += len(batch)
                    _emit(progress, f"[{batch_index}/{len(batches)}] ✗ OpenAlex 批次失败：{type(exc).__name__}")
                    continue
                for paper in batch:
                    parsed += 1
                    before = len(paper.pdf_candidates)
                    item = resolved.get(paper.doi.lower()) or {}
                    abstract = clean_text(item.get("abstract"))
                    if abstract and len(abstract) > len(paper.abstract):
                        paper.abstract = abstract
                        enriched_abstracts += 1
                    for url in item.get("candidates") or []:
                        lowered = url.casefold()
                        priority = 1 if any(token in lowered for token in (
                            "pmc.ncbi.nlm.nih.gov", "europepmc.org", ".pdf", "type=printable"
                        )) else (5 if "doi.org/" in lowered else 2)
                        paper.add_candidate(url, provider, priority=priority)
                    if len(paper.pdf_candidates) > before:
                        with_candidates += 1
                database.save_papers(batch)
                _emit(
                    progress,
                    f"[{batch_index}/{len(batches)}] ✓ {provider} 解析 {len(batch)} 篇；"
                    f"累计候选 {with_candidates}，补摘要 {enriched_abstracts}",
                )
    _emit(progress, f"预解析完成：处理 {parsed} 篇，发现候选 {with_candidates} 篇，失败 {failed} 篇")
    return {
        "total": len(papers), "parsed": parsed, "with_candidates": with_candidates,
        "failed": failed, "enriched_abstracts": enriched_abstracts,
    }


def reconcile_existing_pdfs(
    db_path: Path,
    scan_dirs: Iterable[Path],
    target_dir: Path = Path("pdf_downloaded"),
    progress: Progress | None = None,
) -> dict[str, int]:
    """把旧目录中的有效 PDF 按长题名前缀匹配数据库，集中复制并回写路径。"""
    from .pdf import pdf_ok

    target_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    seen_paths: set[Path] = set()
    for directory in scan_dirs:
        directory = Path(directory)
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.pdf"):
            resolved = path.resolve()
            if resolved not in seen_paths and pdf_ok(path):
                seen_paths.add(resolved)
                files.append(path)

    matched = copied = ambiguous = 0
    with PaperDatabase(db_path) as database:
        papers = database.load_papers_for_download(status="", limit=0)
        normalized = [(paper, normalize_title(paper.title)) for paper in papers]
        for index, source in enumerate(files, 1):
            stem = re.sub(r"_[0-9a-f]{8}$", "", source.stem, flags=re.I)
            key = normalize_title(stem)
            if len(key) < 20:
                continue
            matches = [paper for paper, title_key in normalized if title_key == key or title_key.startswith(key)]
            if len(matches) != 1:
                ambiguous += int(len(matches) > 1)
                continue
            paper = matches[0]
            destination = target_dir / source.name
            if destination.resolve() != source.resolve() and not destination.exists():
                shutil.copy2(source, destination)
                copied += 1
            if not pdf_ok(destination):
                continue
            paper.downloaded_path = str(destination)
            paper.download_source = paper.download_source or "existing"
            paper.download_detail = paper.download_detail or f"从旧目录整理：{source.parent}"
            paper.failure_reason = ""
            database.save_papers([paper])
            matched += 1
            if index % 100 == 0:
                _emit(progress, f"已扫描 {index}/{len(files)} 个 PDF，匹配 {matched}")
    _emit(progress, f"PDF 整理完成：有效文件 {len(files)}，匹配 {matched}，复制 {copied}，歧义 {ambiguous}")
    return {"files": len(files), "matched": matched, "copied": copied, "ambiguous": ambiguous}
