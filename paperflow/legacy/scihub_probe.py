#!/usr/bin/env python3
"""批量体检候选 Sci-Hub 镜像：GET 真实 DOI 路径并按响应类型分类。"""

from __future__ import annotations

import argparse
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

from scihub_core import parse_scihub_page, is_fake_mirror, solve_captcha

DOI = "10.1038/nature12373"  # 经典 Nature 论文（SCI-Hub 必然收录）

CANDIDATES = [
    "sci-hub.se", "sci-hub.st", "sci-hub.ru", "sci-hub.jp", "sci-hub.us",
    "sci-hub.shop", "sci-hub.es", "sci-hub.name", "sci-hub.do", "sci-hub.pm",
    "sci-hub.su", "sci-hub.tv", "sci-hub.com", "sci-hub.net", "sci-hub.org",
    "sci-hub.info", "sci-hub.cc", "sci-hub.de", "sci-hub.it", "sci-hub.nl",
    "sci-hub.pl", "sci-hub.si", "sci-hub.io", "sci-hub.gg", "sci-hub.la",
    "sci-hub.nu", "sci-hub.cz", "sci-hub.dk", "sci-hub.do", "sci-hub.ph",
    "sci-hub.vc", "sci-hub.oj", "sci-hub.wf", "sci-hub.li", "sci-hub.ren",
    "sci-hub.mksa.top", "sci.hubg.org", "sci-hub123.com", "sci-hub.tech",
    "sci-hub.fyi", "sci-hub.zone", "sci-hub.live", "sci-hub.help", "sci-hub.link",
    "sci-hub.work", "sci-hub.cyou", "sci-hub.best", "sci-hub.top", "sci-hub.pw",
    "sci-hub.page", "sci-hub.site", "sci-hub.space", "sci-hub.xyz", "sci-hub.club",
    "sci-hub.online", "sci-hub.world", "sci-hub.download", "sci-hub.tw", "sci-hub.hk",
    "sci-hub.pro", "sci-hub.center", "sci-hub.wang", "sci-hub.bz", "sci-hub.sc",
    "sci-hub.wiki", "sci-hub.et-fine.com", "sci-hub.buzz", "sci-hub.press",
]


def probe(mirror: str) -> tuple[str, str, str]:
    url = f"https://{mirror}/{DOI}"
    try:
        page = parse_scihub_page(requests.Session(), url)
    except Exception as exc:
        return mirror, "error", f"{type(exc).__name__}: {str(exc)[:100]}"
    return mirror, page.kind, (page.pdf_url or page.notice)[:110]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--solve-captcha", action="store_true", help="对 altcha 镜像尝试求解（较慢）")
    args = parser.parse_args()

    results: list[tuple[str, str, str]] = []
    lock = threading.Lock()
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(probe, m): m for m in CANDIDATES}
        for fut in futures:
            mirror, kind, info = fut.result()
            with lock:
                results.append((mirror, kind, info))
                done += 1
                print(f"[{done}/{len(futures)}] {mirror:24s} {kind:12s} {info}", flush=True)

    by_kind: dict[str, list[tuple[str, str, str]]] = {}
    for row in results:
        by_kind.setdefault(row[1], []).append(row)

    print("\n===== 分类汇总 =====")
    for kind in ("pdf", "framepdf", "captcha", "cloudflare", "fake", "notfound", "article", "error"):
        rows = by_kind.get(kind, [])
        if rows:
            print(f"\n· {kind} ({len(rows)}):")
            for m, k, i in rows:
                print(f"   {m:24s} {i}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())