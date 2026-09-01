"""可供 CLI 与 TUI 共用的长任务工作流。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable

from . import net
from .database import PaperDatabase
from .models import Paper, add_paper, clean_text, unique_papers
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
        for index, paper in enumerate(papers, 1):
            ok, info = engine.fetch(paper)
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
    """只解析 DOI 的 OA/出版社候选并写回 SQLite，不下载文件。"""
    from .pdf.oa import OaEngine
    from .pdf.publisher import publisher_of

    oa = OaEngine(email=email, proxies=net.DEFAULT_PROXIES)
    with PaperDatabase(db_path) as database:
        papers = database.load_papers_for_download(
            keyword=keyword, source=source, status="all", limit=limit
        )
        parsed = with_candidates = failed = 0
        for index, paper in enumerate(papers, 1):
            if not paper.doi:
                _emit(progress, f"[{index}/{len(papers)}] 跳过（无 DOI）：{paper.title[:60]}")
                continue
            parsed += 1
            before = len(paper.pdf_candidates)
            try:
                for url in oa._candidates_for_doi(paper.doi):
                    paper.add_candidate(url, "oa", priority=2)
                meta = publisher_of(paper.doi)
                if meta:
                    paper.add_candidate(meta[1], "publisher", priority=4)
                if len(paper.pdf_candidates) > before:
                    with_candidates += 1
                database.save_papers([paper])
                _emit(progress, f"[{index}/{len(papers)}] ✓ {paper.title[:60]}：新增 {len(paper.pdf_candidates)-before} 个候选")
            except Exception as exc:
                failed += 1
                _emit(progress, f"[{index}/{len(papers)}] ✗ {paper.title[:60]}：{type(exc).__name__}")
    _emit(progress, f"预解析完成：处理 {parsed} 篇，发现候选 {with_candidates} 篇，失败 {failed} 篇")
    return {"total": len(papers), "parsed": parsed, "with_candidates": with_candidates, "failed": failed}
