#!/usr/bin/env python3
"""paperflow 统一命令行：多源检索（WOS/PubMed/Crossref/S2/CNKI/EuropePMC）+ PDF 下载（Sci-Hub/OA/出版社）。

用法示例:
  python -m paperflow search --species "Panthera tigris" --sources pubmed,crossref --limit 20
  python -m paperflow wos-fetch --input input.txt --max-records 1000
  python -m paperflow download --doi-file doi_list.tsv --out downloads --mode scihub+oa
  python -m paperflow run --input input.txt    # 全流程：多源检索 → 汇总 → PDF
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .config import load_env

load_env()

from .models import Paper, add_paper, clean_text, unique_papers
from .sources import SOURCES


DEFAULT_DB = Path("paperflow.db")


def _load_species(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"输入文件不存在: {path}")
    values = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        value = line.strip()
        if value and not value.startswith("#") and value not in values:
            values.append(value)
    if not values:
        raise ValueError("输入中没有物种名")
    return values


def _load_doi_file(path: Path) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue
        if "\t" in line:
            doi, title = line.split("\t", 1)
        else:
            doi, title = line, ""
        doi = doi.strip().rstrip(".").lower()
        if doi:
            items.append((doi, title))
    return items


def cmd_search(args: argparse.Namespace) -> int:
    species_names = _load_species(args.input) if args.input else args.species
    source_names = args.sources.split(",") if args.sources else [
        "WOS", "PubMed", "Europe PMC", "Crossref", "S2", "CNKI"
    ]
    from .workflows import search_to_database
    papers = search_to_database(
        species_names, source_names, args.limit, args.email, args.db, progress=print
    )
    with Path(args.out).open("w", encoding="utf-8") as f:
        for i, p in enumerate(papers, 1):
            f.write(f"[{i}] {p.title}\n")
            f.write(f"Authors: {', '.join(p.authors) or 'N/A'}\nYear: {p.year or 'N/A'}\n")
            f.write(f"Journal: {p.journal or 'N/A'}\nDOI: {p.doi or 'N/A'}\n")
            f.write(f"Sources: {', '.join(sorted(p.sources))}\n")
            f.write(f"Abstract: {p.abstract or 'N/A'}\n\n")
    print(f"\n完成: 检索到 {len(papers)} 篇 (去重后)。输出: {args.out}；数据库: {args.db}")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    from .pdf import PdfEngine
    from .database import PaperDatabase
    items = _load_doi_file(args.doi_file)
    if args.limit:
        items = items[: args.limit]
    engine = PdfEngine(
        out_dir=args.out,
        email=args.email,
        max_per_minute=args.rpm,
        cookie_file=Path("scihub_cookies.json"),
        use_scihub="scihub" in args.mode,
        use_oa="oa" in args.mode,
        use_publisher="publisher" in args.mode,
        use_cnki="cnki" in args.mode,
    )
    ok_count = fail_count = 0
    t0 = time.time()
    failed_path = Path(args.failed)
    with PaperDatabase(args.db) as database:
        for i, (doi, title) in enumerate(items, 1):
            paper = Paper(title=title or doi, doi=doi, sources={"manual"})
            ok, info = engine.fetch(paper)
            database.save_download(paper, ok, info, args.keyword)
            if ok:
                ok_count += 1
                print(f"[{i}/{len(items)}] OK {doi} | {info[:60]}", flush=True)
            else:
                fail_count += 1
                with failed_path.open("a", encoding="utf-8") as f:
                    f.write(f"{doi}\t{title}\t{info}\n")
                print(f"[{i}/{len(items)}] FAIL {doi} | {info[:80]}", flush=True)
    print(f"\n完成: 成功 {ok_count} / 失败 {fail_count} / {len(items)} / 用时 {time.time()-t0:.0f}s；数据库: {args.db}")
    return 0


def cmd_download_db(args: argparse.Namespace) -> int:
    """从 SQLite 恢复检索结果与候选，独立执行下载队列。"""
    from .workflows import download_database_queue
    result = download_database_queue(
        db_path=args.db, out_dir=args.out, mode=args.mode, rpm=args.rpm, email=args.email,
        keyword=args.keyword, source=args.source, status=args.status, limit=args.limit,
        min_if=args.min_if, max_if=args.max_if, progress=print,
    )
    print(f"数据库: {args.db}")
    return 0 if result["failed"] == 0 else 3


def cmd_download_preflight(args: argparse.Namespace) -> int:
    from .workflows import preflight_download_candidates
    result = preflight_download_candidates(
        args.db, args.email, args.limit, args.keyword, args.source, progress=print
    )
    print(f"预解析完成：处理 {result['parsed']} 篇，发现候选 {result['with_candidates']} 篇")
    return 0


def cmd_impact_factor(args: argparse.Namespace) -> int:
    """导入正式 JIF 表，自动关联数据库中的历史和未来论文。"""
    from .database import PaperDatabase

    with PaperDatabase(args.db) as database:
        result = database.import_impact_factors(args.file, args.source, args.year)
    print(
        f"影响因子导入完成: 导入 {result['imported']}，跳过 {result['skipped']}，"
        f"已匹配论文 {result['matched_papers']}；数据库: {args.db}"
    )
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    """启动 Textual 全屏终端界面。"""
    from .tui import run_tui
    return run_tui(args.db)


def cmd_run(args: argparse.Namespace) -> int:
    """全流程：物种 → 多源检索 → 汇总 → PDF 下载。"""
    from .pdf import PdfEngine
    from .database import PaperDatabase
    from . import net
    client = net.make_session(email=args.email)
    species_names = _load_species(args.input)
    requested_sources = [name.strip() for name in args.sources.split(",") if name.strip()]
    sources = SOURCES.ordered(requested_sources)
    collection: dict[str, Paper] = {}
    for species in species_names:
        print(f"\n检索物种: {species}")
        for source in sources:
            try:
                for p in source.search_species(client, species, args.limit):
                    add_paper(collection, p)
                print(f"  {source.name}: 完成")
            except NotImplementedError:
                print(f"  {source.name}: 跳过（CNKI 需浏览器会话）")
            except Exception as exc:
                print(f"  {source.name}: 失败（{clean_text(exc)[:80]}）")
    papers = sorted(unique_papers(collection), key=lambda p: p.year, reverse=True)
    engine = PdfEngine(out_dir=args.out, email=args.email, max_per_minute=args.rpm,
                       cookie_file=Path("scihub_cookies.json"),
                       use_scihub="scihub" in args.mode, use_oa="oa" in args.mode,
                       use_publisher="publisher" in args.mode,
                       use_cnki="cnki" in args.mode)
    ok_count = fail_count = 0
    with PaperDatabase(args.db) as database:
        database.save_papers(papers)
        for i, paper in enumerate(papers, 1):
            ok, info = engine.fetch(paper)
            database.save_download(paper, ok, info)
            if ok:
                ok_count += 1
                print(f"[{i}/{len(papers)}] OK {paper.doi or paper.title[:40]} | {info[:50]}", flush=True)
            else:
                fail_count += 1
                print(f"[{i}/{len(papers)}] FAIL {paper.doi or paper.title[:40]} | {info[:60]}", flush=True)
    with Path(args.summary).open("w", encoding="utf-8") as f:
        f.write(f"检索到论文: {len(papers)}\n成功下载: {ok_count}\n失败: {fail_count}\n\n")
        for p in papers:
            status = "downloaded" if p.downloaded_path else "failed"
            f.write(f"[{status}] {p.title} | doi:{p.doi} | {p.failure_reason or p.downloaded_path}\n")
    print(f"\n完成: {len(papers)} 篇, 成功 {ok_count}, 失败 {fail_count}。摘要: {args.summary}；数据库: {args.db}")
    return 0


def cmd_export_wos(args: argparse.Namespace) -> int:
    """调用 legacy 浏览器导出脚本，仅作 Full Record 备用。"""
    import subprocess
    cmd = [sys.executable, str(Path(__file__).resolve().parent / "legacy" / "wos_edge_export.py")]
    if args.input:
        cmd += ["--input", str(args.input)]
    if args.max_records:
        cmd += ["--max-records", str(args.max_records)]
    return subprocess.call(cmd)


def cmd_wos_fetch(args: argparse.Namespace) -> int:
    """通过 WOS Starter API 分页批量获取，逐页写入 SQLite。"""
    from .workflows import fetch_wos_to_database

    keywords = _load_species(args.input) if args.input else (args.keyword or [])
    if args.max_records < 0:
        raise ValueError("--max-records 必须大于等于 0（0 表示获取全部）")
    result = fetch_wos_to_database(
        keywords=keywords,
        max_records=args.max_records,
        db_path=args.db,
        manifest_path=args.manifest,
        request_interval=args.interval,
        resume=not args.no_resume,
        progress=print,
    )
    print(
        f"数据库: {args.db}；断点清单: {args.manifest}；"
        f"本次处理 {result['saved']} 条"
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """环境自检：Python/依赖/Playwright 浏览器/代理/登录态。"""
    import platform
    import shutil
    print(f"Python: {platform.python_version()}", flush=True)
    ok = True
    for mod in ("requests", "bs4", "playwright", "playwright_stealth", "textual"):
        try:
            __import__(mod)
            print(f"  依赖 {mod}: OK", flush=True)
        except ImportError:
            print(f"  依赖 {mod}: 缺失", flush=True)
            ok = False
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            exe = p.chromium.executable_path
            print(f"  Playwright chromium: {exe}", flush=True)
            if not Path(exe).exists():
                print("    ⚠ 浏览器未安装，请运行: playwright install chromium", flush=True)
                ok = False
    except Exception as exc:
        print(f"  Playwright 检查失败: {str(exc)[:80]}", flush=True)
        ok = False
    from .net import DEFAULT_PROXIES
    for pxx in (proxy for proxy in DEFAULT_PROXIES if proxy):
        import socket
        host = pxx.split("@")[-1].split(":")[0]
        port = int(pxx.split(":")[-1])
        try:
            s = socket.create_connection((host, port), timeout=2)
            s.close()
            print(f"  代理 {host}:{port}: 可达", flush=True)
        except OSError:
            print(f"  代理 {host}:{port}: 不可达（可忽略，直连也可用）", flush=True)
    from . import auth
    auth.status()
    print(f"  WOS Starter API Key: {'已配置' if os.getenv('WOS_API_KEY', '').strip() else '未配置'}")
    print("\n医生结论:", "一切就绪 🎉" if ok else "请修复上面 ⚠ 项后重试")
    return 0 if ok else 1


def cmd_auth(args: argparse.Namespace) -> int:
    """浏览器授权：弹出窗口登录站点并持久化登录态。"""
    from . import auth
    if args.action == "login":
        if args.site not in auth.AUTH_SITES:
            print(f"未知站点 {args.site}；可用: {', '.join(auth.AUTH_SITES)}")
            return 2
        auth.login(args.site, headful=not args.headless)
    elif args.action == "status":
        auth.status(args.site)
    return 0


def cmd_wizard(args):
    """引导式数据采集向导：一步步回答问题，自动执行检索+下载+三件套输出。"""
    print("""
╔══════════════════════════════════════════════════════════╗
║   paperflow 数据采集向导 v0.6                             ║
║   多源检索(WOS/PubMed/EuropePMC/Crossref/S2/CNKI)          ║
║   + PDF 下载(CNKI/Sci-Hub/OA/出版社) → abstracts/summary/pdf ║
╚══════════════════════════════════════════════════════════╝
""")
    get = lambda prompt, default: input(prompt).strip() or default

    # 1. 物种
    raw = get("① 物种拉丁名（逗号分隔，如: Panthera tigris, Ginkgo biloba）: ", "Ginkgo biloba")
    if raw.endswith((".txt", ".tsv")) and Path(raw).exists():
        species_names = [l.strip() for l in Path(raw).read_text(encoding="utf-8-sig").splitlines()
                         if l.strip() and not l.startswith("#")]
    else:
        species_names = [s.strip() for s in raw.split(",") if s.strip()]
    print(f"   → 物种: {species_names}\n")

    # 2. 数据源
    print("② 选择检索数据源（每项回车=默认）:")
    src_choice = input("   WOS? [y/N]: ").strip().lower()
    pm = input("   PubMed? [y/N]: ").strip().lower()
    epmc = input("   Europe PMC? [y/N]: ").strip().lower()
    cr = input("   Crossref? [y/N]: ").strip().lower()
    s2 = input("   Semantic Scholar? [y/N]: ").strip().lower()
    cnki = input("   CNKI(中文知网,需Edge会话)? [y/N]: ").strip().lower()
    wanted = []
    for name, yes in (("WOS", src_choice), ("PubMed", pm), ("Europe PMC", epmc),
                      ("Crossref", cr), ("S2", s2), ("CNKI", cnki)):
        if yes == "y":
            wanted.append(name)
    if not wanted:
        wanted = ["PubMed", "Europe PMC", "Crossref", "S2"]
    print(f"   → 数据源: {wanted}\n")

    # 3. 条数/模式/限速
    limit = int(get("③ 每源、每物种最多条数 [默认 20]: ", "20"))
    print("④ PDF 下载通道（多选，逗号分隔）:")
    mode = get("   cnki,scihub,oa,publisher（如: cnki,oa）: ", "cnki,scihub,oa")
    mode = "+".join(m.strip() for m in mode.replace(",", "+").split("+") if m.strip())
    email = os.getenv("UNPAYWALL_EMAIL", "") or "species.literature.research2026@outlook.com"
    if "oa" in mode:
        print(f"   （OA 下载使用邮箱: {email}，可在 .env 设置 UNPAYWALL_EMAIL 替换）")
    rpm = int(get("⑤ 下载限速 篇/分钟 [默认 30]: ", "30"))

    # 6. 输出
    clean = get("⑥ 清空旧 pdf_downloaded 后全新下载? [y/N]: ", "").lower() == "y"
    out = get("⑦ 输出目录（abstracts.txt/summary.txt 所在，回车用当前目录）: ", ".")

    print(f"\n即将执行:")
    print(f"  物种      : {species_names}")
    print(f"  数据源    : {wanted}")
    print(f"  每源条数  : {limit}")
    print(f"  下载通道  : {mode}  (限速 {rpm}/分钟)")
    print(f"  输出      : {out}/abstracts.txt , summary.txt , pdf_downloaded/")
    if get("开始执行? [y/N]: ", "y").strip().lower() != "y":
        print("已取消。")
        return 0

    return run_report(
        species_names, out, "pdf_downloaded", mode, rpm, email, limit,
        clean=clean, db_path=Path(out) / DEFAULT_DB,
    )


def cmd_db(args: argparse.Namespace) -> int:
    """查看数据库统计/论文，或迁移旧 abstracts.txt + summary.txt。"""
    from .database import PaperDatabase

    with PaperDatabase(args.db) as database:
        if args.action == "stats":
            stats = database.stats()
            print(f"数据库: {args.db}")
            print(f"文章: {stats['papers']}")
            print(f"关键词: {stats['keywords']}")
            print(f"检索来源: {stats['sources']}")
            print(f"已有 PDF: {stats['downloaded']}")
            print(f"下载记录: {stats['download_attempts']}")
            print(f"PDF 候选: {stats['pdf_candidates']}")
            print(f"影响因子记录: {stats['journal_metrics']}")
            print(f"已匹配影响因子文章: {stats['papers_with_impact_factor']}")
        elif args.action == "list":
            rows = database.list_papers(
                args.keyword, args.limit, args.source, args.status, args.min_if, args.max_if
            )
            for row in rows:
                factor = (
                    f"{row['impact_factor']:.3f} ({row['impact_factor_year']}, {row['impact_factor_source']})"
                    if row["impact_factor"] is not None else "-"
                )
                print(
                    f"[{row['id']}] {row['year'] or '-'} | {row['title']}\n"
                    f"    DOI: {row['doi'] or '-'} | 关键词: {row['keywords'] or '-'} | 来源: {row['sources'] or '-'}\n"
                    f"    影响因子: {factor} | PDF: {row['pdf_path'] or '-'} | 下载源: {row['download_source'] or '-'}"
                )
            print(f"共显示 {len(rows)} 条")
        elif args.action == "import-legacy":
            result = database.import_legacy(args.abstracts, args.summary)
            print(
                f"迁移完成: 文章 {result['papers']} 条，匹配 PDF 路径 {result['pdf_paths']} 条；"
                f"数据库: {args.db}"
            )
        elif args.action == "dedupe":
            result = database.deduplicate(args.dry_run)
            prefix = "预览" if args.dry_run else "去重完成"
            print(
                f"{prefix}: 可合并组 {result['groups']}，"
                f"{'将移除' if args.dry_run else '已移除'} {result['removed']} 条，"
                f"因标识冲突跳过 {result['skipped_conflicts']} 组；数据库: {args.db}"
            )
        elif args.action == "reconcile-pdfs":
            from .workflows import reconcile_existing_pdfs
            directories = args.scan_dir or [
                Path("downloads"), Path("unpaywall_downloads"),
                Path("scihub_downloads"), Path("pdf_downloaded"),
            ]
            result = reconcile_existing_pdfs(
                args.db, directories, args.pdf_dir, progress=print
            )
            print(
                f"整理完成：扫描 {result['files']}，匹配 {result['matched']}，"
                f"复制 {result['copied']}，歧义 {result['ambiguous']}"
            )
        elif args.action == "export-report":
            papers = database.load_papers_for_download(status="", limit=0)
            from .pdf import pdf_ok
            for paper in papers:
                if paper.downloaded_path and not pdf_ok(Path(paper.downloaded_path)):
                    paper.downloaded_path = ""
                    paper.failure_reason = paper.failure_reason or "数据库中的 PDF 文件已不存在"
                elif not paper.downloaded_path:
                    paper.failure_reason = paper.failure_reason or "未找到合法可直接下载的 PDF"
            _write_abstracts(papers, args.abstracts)
            _write_summary(papers, args.summary)
            print(f"导出完成：{len(papers)} 篇 → {args.abstracts} / {args.summary}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")

    p_s = sub.add_parser("search", help="多源检索物种文献 → 元数据清单")
    p_s.add_argument("--input", type=Path, help="物种名文件（每行一个拉丁名）")
    p_s.add_argument("--species", action="append", help="或直接指定物种名（可多次）")
    p_s.add_argument("--sources", default="", help="逗号分隔: WOS,PubMed,Europe PMC,Crossref,S2,CNKI")
    p_s.add_argument("--limit", type=int, default=20)
    p_s.add_argument("--email", default="", help="NCBI/Unpaywall 联系邮箱")
    p_s.add_argument("--out", default="papers_meta.txt")
    p_s.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 数据库路径")
    p_s.set_defaults(fn=cmd_search)

    p_d = sub.add_parser("download", help="按 DOI 清单批量下载 PDF")
    p_d.add_argument("--doi-file", type=Path, required=True)
    p_d.add_argument("--out", type=Path, default=Path("downloads"))
    p_d.add_argument("--mode", default="scihub+oa", help="可组合: cnki, scihub, oa, publisher（逗号或+分隔）")
    p_d.add_argument("--rpm", type=int, default=30, help="下载限速（篇/分钟），稳定优先")
    p_d.add_argument("--limit", type=int, default=0)
    p_d.add_argument("--email", default="")
    p_d.add_argument("--failed", default="download_failed.txt")
    p_d.add_argument("--keyword", action="append", default=[], help="给 DOI 清单关联关键词（可多次）")
    p_d.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 数据库路径")
    p_d.set_defaults(fn=cmd_download)

    p_dd = sub.add_parser("download-db", help="从 SQLite 队列独立下载（与检索完全解耦）")
    p_dd.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 数据库路径")
    p_dd.add_argument("--out", type=Path, default=Path("downloads"))
    p_dd.add_argument("--mode", default="oa+scihub", help="可组合: direct, cnki, oa, scihub, publisher, authorized；默认 oa+scihub")
    p_dd.add_argument("--keyword", default="", help="只下载指定检索关键词")
    p_dd.add_argument("--source", default="", help="只下载指定元数据来源，如 CNKI")
    p_dd.add_argument("--status", choices=["pending", "failed", "all", "candidate", "candidate-pending", "candidate-pmc-pending"], default="pending",
                      help="pending=未尝试，failed=重试失败项，candidate=已有候选，candidate-pending=候选且未尝试，candidate-pmc-pending=PMC候选且未尝试，all=全部未下载")
    p_dd.add_argument("--min-if", type=float, default=None, help="最低影响因子（需先导入 JIF）")
    p_dd.add_argument("--max-if", type=float, default=None, help="最高影响因子（需先导入 JIF）")
    p_dd.add_argument("--limit", type=int, default=100)
    p_dd.add_argument("--rpm", type=int, default=60, help="下载限速（推荐 60，即每篇约 1 秒）")
    p_dd.add_argument("--email", default="")
    p_dd.set_defaults(fn=cmd_download_db)

    p_pf = sub.add_parser("download-preflight", help="预解析 DOI 的 OA/出版社下载候选，不下载文件")
    p_pf.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_pf.add_argument("--keyword", default="")
    p_pf.add_argument("--source", default="")
    p_pf.add_argument("--limit", type=int, default=100)
    p_pf.add_argument("--email", default="")
    p_pf.set_defaults(fn=cmd_download_preflight)

    p_if = sub.add_parser("impact-factor", help="导入正式 JIF CSV/TSV 并自动匹配期刊")
    p_if.add_argument("action", choices=["import"])
    p_if.add_argument("--file", type=Path, required=True, help="含 journal/impact_factor/year 的 CSV/TSV")
    p_if.add_argument("--source", default="JCR", help="数据来源标签，如 JCR 2025")
    p_if.add_argument("--year", type=int, default=None, help="文件无 year 列时使用的年份")
    p_if.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_if.set_defaults(fn=cmd_impact_factor)

    p_tui = sub.add_parser("tui", help="启动全屏终端界面")
    p_tui.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 数据库路径")
    p_tui.set_defaults(fn=cmd_tui)

    p_r = sub.add_parser("run", help="全流程：检索 + 下载")
    p_r.add_argument("--input", type=Path, default=Path("input.txt"))
    p_r.add_argument("--sources", default="PubMed,Europe PMC,Crossref,S2,CNKI",
                     help="逗号分隔检索源；仅下载知网可设为 CNKI")
    p_r.add_argument("--out", type=Path, default=Path("downloads"))
    p_r.add_argument("--mode", default="cnki+scihub+oa")
    p_r.add_argument("--rpm", type=int, default=30)
    p_r.add_argument("--limit", type=int, default=20)
    p_r.add_argument("--email", default="")
    p_r.add_argument("--summary", default="run_summary.txt")
    p_r.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 数据库路径")
    p_r.set_defaults(fn=cmd_run)

    p_rep = sub.add_parser("report", help="三件套：现取检索 + 重新下载 → abstracts.txt / pdf_downloaded/ / summary.txt")
    p_rep.add_argument("--input", type=Path, default=Path("input.txt"))
    p_rep.add_argument("--out", type=Path, default=Path("."))
    p_rep.add_argument("--pdf-dir", type=Path, default=Path("pdf_downloaded"))
    p_rep.add_argument("--mode", default="cnki+scihub+oa")
    p_rep.add_argument("--rpm", type=int, default=90)
    p_rep.add_argument("--limit", type=int, default=50, help="每源每物种条数")
    p_rep.add_argument("--email", default="")
    p_rep.add_argument("--clean", action="store_true", help="清空旧 pdf_downloaded")
    p_rep.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 数据库路径")
    p_rep.set_defaults(fn=cmd_report)

    p_wiz = sub.add_parser("wizard", help="引导式数据采集（交互问答 → 自动执行三件套）")
    p_wiz.set_defaults(fn=cmd_wizard)

    p_wf = sub.add_parser("wos-fetch", help="WOS Starter API 分页批量获取 → SQLite")
    input_group = p_wf.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--input", type=Path, help="关键词文件（每行一个）")
    input_group.add_argument("--keyword", action="append", help="检索关键词（可多次）")
    p_wf.add_argument("--max-records", type=int, default=1000, help="每个关键词的目标总数；0=全部")
    p_wf.add_argument("--interval", type=float, default=1.0, help="API 请求间隔秒数（默认 1.0）")
    p_wf.add_argument("--manifest", type=Path, default=Path("wos_api_runs/manifest.json"), help="断点清单")
    p_wf.add_argument("--no-resume", action="store_true", help="忽略断点，从第 1 条重新获取（SQLite 仍会去重）")
    p_wf.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 数据库路径")
    p_wf.set_defaults(fn=cmd_wos_fetch)

    p_w = sub.add_parser("export-wos", help="旧版浏览器 Full Record 导出（备用）")
    p_w.add_argument("--input", type=Path)
    p_w.add_argument("--max-records", type=int, default=20)
    p_w.set_defaults(fn=cmd_export_wos)

    p_a = sub.add_parser("auth", help="浏览器授权（弹窗登录 WOS/CNKI/出版社等站点）")
    p_a.add_argument("action", choices=["login", "status"])
    p_a.add_argument("site", nargs="?", default="", help="scihub/cnki/wos/sciencedirect/springer/wiley/publisher")
    p_a.add_argument("--headless", action="store_true", help="无头模式（仅适用 auto 站点如 scihub）")
    p_a.set_defaults(fn=cmd_auth)

    p_dr = sub.add_parser("doctor", help="环境自检（依赖/浏览器/代理/授权）")
    p_dr.set_defaults(fn=cmd_doctor)

    p_db = sub.add_parser("db", help="SQLite 数据库统计、查询与旧数据迁移")
    p_db.add_argument("action", choices=[
        "stats", "list", "import-legacy", "dedupe", "reconcile-pdfs", "export-report"
    ])
    p_db.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 数据库路径")
    p_db.add_argument("--keyword", default="", help="list 时按关键词过滤")
    p_db.add_argument("--source", default="", help="list 时按元数据来源过滤")
    p_db.add_argument("--status", choices=["pending", "failed", "all", "downloaded"], default="",
                      help="list 时按下载状态过滤")
    p_db.add_argument("--min-if", type=float, default=None, help="list 时按最低影响因子过滤")
    p_db.add_argument("--max-if", type=float, default=None, help="list 时按最高影响因子过滤")
    p_db.add_argument("--limit", type=int, default=20, help="list 最多显示条数")
    p_db.add_argument("--abstracts", type=Path, default=Path("abstracts.txt"))
    p_db.add_argument("--summary", type=Path, default=Path("summary.txt"))
    p_db.add_argument("--scan-dir", type=Path, action="append", default=[], help="reconcile-pdfs 扫描目录（可多次）")
    p_db.add_argument("--pdf-dir", type=Path, default=Path("pdf_downloaded"), help="整理后的 PDF 目录")
    p_db.add_argument("--dry-run", action="store_true", help="dedupe 时仅预览，不修改数据库")
    p_db.set_defaults(fn=cmd_db)

    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return 1
    return args.fn(args)


def citation(paper: Paper) -> str:
    authors = ", ".join(paper.authors) if paper.authors else "作者不详"
    year = paper.year or "年份不详"
    journal = f" {paper.journal}." if paper.journal else ""
    doi = f" doi:{paper.doi}." if paper.doi else ""
    title = paper.title.rstrip(".。")
    return f"{authors} ({year}). {title}.{journal}{doi}".strip()


def _write_abstracts(papers, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, p in enumerate(papers, 1):
            f.write(f"[{i}] {p.title}\n")
            f.write(f"Authors: {', '.join(p.authors) or 'N/A'}\nYear: {p.year or 'N/A'}\n")
            f.write(f"Journal: {p.journal or 'N/A'}\nDOI: {p.doi or 'N/A'}\n")
            f.write(f"Species: {', '.join(sorted(p.species))}\nSources: {', '.join(sorted(p.sources))}\n")
            f.write(f"Abstract: {p.abstract or 'N/A'}\n\n")


def _write_summary(papers, path):
    downloaded = [p for p in papers if p.downloaded_path]
    failed = [p for p in papers if not p.downloaded_path]
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"检索到论文: {len(papers)}\n成功下载: {len(downloaded)}\n下载失败: {len(failed)}\n\n")
        f.write("downloaded:\n")
        for p in downloaded:
            f.write(f"- {citation(p)} [PDF: {p.downloaded_path}]\n")
        f.write("\nfailedDownload:\n")
        for p in failed:
            f.write(f"- {citation(p)} [原因: {p.failure_reason}]\n")


def run_report(
    species_names, out_dir, pdf_dir, mode, rpm, email, limit,
    clean=True, db_path=DEFAULT_DB,
):
    """三件套执行核心：现取检索 → 下载 PDF → abstracts/summary。"""
    import shutil
    from .pdf import PdfEngine
    from .database import PaperDatabase
    from . import net as _net

    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir = Path(pdf_dir)
    if clean and pdf_dir.exists():
        shutil.rmtree(pdf_dir)
    pdf_dir.mkdir(parents=True, exist_ok=True)

    sources = SOURCES.ordered(["PubMed", "Europe PMC", "Crossref", "S2", "CNKI"])
    api_sources = [s for s in sources if s.name not in ("WOS", "CNKI")]
    web_sources = [s for s in sources if s.name in ("WOS", "CNKI")]
    collection: dict = {}
    client = _net.make_session(proxy=None, email=email)

    def _run_one(sp, source):
        try:
            results = source.search_species(client, sp, limit)
            return sp, source.name, results, None
        except NotImplementedError as exc:
            return sp, source.name, [], f"跳过:{clean_text(exc)[:50]}"
        except Exception as exc:
            return sp, source.name, [], clean_text(exc)[:80]

    # API 源并行；Web 源（WOS/CNKI，共用浏览器桥）串行
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from tqdm import tqdm

    tasks = [(sp, s) for sp in species_names for s in api_sources]
    if tasks:
        with ThreadPoolExecutor(max_workers=min(6, len(tasks))) as pool:
            futs = {pool.submit(_run_one, sp, s): (sp, s) for sp, s in tasks}
            for fut in tqdm(as_completed(futs), total=len(futs), desc="[并行检索 API 源]", unit="任务"):
                sp, sname = futs[fut]
                sp_, name, results, err = fut.result()
                if err:
                    print(f"  ⚠ {name}({sp_}): {err}")
                    continue
                for p in results:
                    add_paper(collection, p)
    for sp, s in [(sp_, s_) for sp_ in species_names for s_ in web_sources]:
        sp_, name, results, err = _run_one(sp, s)
        if err:
            print(f"  ⚠ {name}({sp_}): {err}")
            continue
        for p in results:
            add_paper(collection, p)
        print(f"  ✅ {name}({sp_}): {len(results)} 条")

    papers = sorted(unique_papers(collection), key=lambda p: (p.year, p.title.casefold()), reverse=True)
    print(f"\n检索汇总: {len(papers)} 篇（去重后）")
    _write_abstracts(papers, out_dir / "abstracts.txt")

    engine = PdfEngine(out_dir=pdf_dir, email=email, max_per_minute=rpm,
                       cookie_file=Path("scihub_cookies.json"),
                       use_scihub="scihub" in mode,
                       use_oa="oa" in mode,
                       use_publisher="publisher" in mode,
                       use_cnki="cnki" in mode)
    from tqdm import tqdm
    with PaperDatabase(db_path) as database:
        database.save_papers(papers)
        for paper in tqdm(papers, desc="[下载 PDF]", unit="篇"):
            ok, info = engine.fetch(paper)
            database.save_download(paper, ok, info)
            mark = "✅" if ok else "❌"
            tqdm.write(f"{mark} {paper.doi or paper.title[:40]} | {info[:50]}")

    _write_summary(papers, out_dir / "summary.txt")
    print(f"\n完成:")
    print(f"  abstracts.txt → {out_dir / 'abstracts.txt'}")
    print(f"  pdf_downloaded → {pdf_dir}")
    print(f"  summary.txt → {out_dir / 'summary.txt'}")
    print(f"  SQLite → {db_path}")
    return 0


def cmd_report(args):
    """全新数据端到端：现取检索 + 重新下载 PDF + 生成 abstracts.txt/summary.txt。"""
    species_names = _load_species(args.input)
    return run_report(species_names, args.out, args.pdf_dir, args.mode,
                      args.rpm, args.email, args.limit, clean=args.clean, db_path=args.db)


if __name__ == "__main__":
    raise SystemExit(main())
