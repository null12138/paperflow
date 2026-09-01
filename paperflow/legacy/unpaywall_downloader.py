#!/usr/bin/env python3
"""基于 DOI 的 Unpaywall 批量 PDF 下载工具（网络加固版）。

对每个 DOI 查询 Unpaywall API，收集 best_oa_location 与 oa_locations 中的
url_for_pdf / url 候选逐个下载，PDF 头校验、断点续跑、多线程并发。
多代理 failover + 每代理重试；失败原因分三类记录。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

UA = "SpeciesUnpaywallDownloader/1.0 (mailto:{email})"
TIMEOUT = 30
API = "https://api.unpaywall.org/v2/{}"
PROXIES = ["http://127.0.0.1:7890", None]
PROXY_TIMEOUTS = [8, 12, 20]  # 对应每个代理短超时，快速失败切换


def clean_filename(title: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", title).strip("._")[:160] or "paper"


def make_session(email: str, proxy) -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = UA.format(email=email)
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def fetch_locations(email: str, doi: str) -> list[str]:
    """查询 Unpaywall；多代理×重试。返回 OA PDF 候选列表。"""
    last = None
    for attempt in range(3):
        for i, proxy in enumerate(PROXIES):
            sess = make_session(email, proxy)
            try:
                r = sess.get(API.format(doi), params={"email": email}, timeout=PROXY_TIMEOUTS[i])
                if r.status_code == 404:
                    return []
                r.raise_for_status()
                data = r.json()
                cands: list[str] = []
                locs = []
                if data.get("best_oa_location"):
                    locs.append(data["best_oa_location"])
                locs.extend(data.get("oa_locations") or [])
                for loc in locs:
                    url = loc.get("url_for_pdf") or loc.get("url")
                    if url and url not in cands:
                        cands.append(url)
                return cands
            except Exception as exc:
                last = exc
                continue
        time.sleep(1.0 + attempt)
    raise RuntimeError(str(last)[:120] if last else "all proxies failed")


def download_one(doi: str, title: str, out: Path, email: str) -> tuple[bool, str]:
    fname = out / (clean_filename(title or doi) + ".pdf")
    if fname.exists() and fname.read_bytes()[:5] == b"%PDF-":
        return True, "already-exists"
    try:
        cands = fetch_locations(email, doi)
    except Exception as exc:
        return False, f"Unpaywall查询失败: {type(exc).__name__}: {str(exc)[:100]}"
    if not cands:
        return False, "Unpaywall无OA候选"
    errs = []
    for url in cands:
        for i, proxy in enumerate(PROXIES):
            try:
                s2 = make_session(email, proxy)
                r = s2.get(url, timeout=PROXY_TIMEOUTS[i] + 15, allow_redirects=True)
                if r.content[:5] == b"%PDF-":
                    tmp = fname.with_suffix(".part")
                    tmp.write_bytes(r.content)
                    tmp.rename(fname)
                    return True, url
                errs.append(f"{url[:60]}: 非PDF")
                break  # 拿到响应但非PDF，换下一个候选
            except Exception as exc:
                errs.append(f"{url[:60]}: {type(exc).__name__}")
                continue
    return False, "; ".join(errs)[:400]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="TSV(doi<TAB>title) 或 纯txt(一行一个DOI)")
    parser.add_argument("--out", type=Path, default=Path("unpaywall_downloads"))
    parser.add_argument("--workers", type=int, default=28)
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条（0=全部）")
    parser.add_argument("--email", type=str, default="")
    args = parser.parse_args()

    email = args.email or os.getenv("UNPAYWALL_EMAIL", "").strip()
    if not email:
        print("错误：需要 --email（Unpaywall 要求真实邮箱）", file=sys.stderr)
        return 2

    lines = [ln.strip() for ln in args.input.read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
    items: list[tuple[str, str]] = []
    for ln in lines:
        if "\t" in ln:
            doi, title = ln.split("\t", 1)
        else:
            doi, title = ln, ""
        doi = doi.strip().rstrip(".").lower()
        if doi:
            items.append((doi, title))
    if args.limit:
        items = items[: args.limit]

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    failed_path = out.parent / "unpaywall_failed.txt"
    lock = threading.Lock()
    counters = {"ok": 0, "fail": 0, "skip": 0}
    total = len(items)

    def worker(item: tuple[str, str]) -> None:
        doi, title = item
        fname = out / (clean_filename(title or doi) + ".pdf")
        if fname.exists() and fname.read_bytes()[:5] == b"%PDF-":
            with lock:
                counters["skip"] += 1
                print(f"[{sum(counters.values())}/{total}] 跳过 {doi}", flush=True)
            return
        ok, info = download_one(doi, title, out, email)
        with lock:
            if ok:
                counters["ok"] += 1
                print(f"[{sum(counters.values())}/{total}] OK {doi}", flush=True)
            else:
                counters["fail"] += 1
                with failed_path.open("a", encoding="utf-8") as f:
                    f.write(f"{doi}\t{title}\t{info}\n")
                print(f"[{sum(counters.values())}/{total}] FAIL {doi} | {info[:90]}", flush=True)

    # 清空旧失败清单（本次重跑会重写）
    if failed_path.exists() and args.limit == 0:
        failed_path.unlink()

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for _ in pool.map(worker, items):
            pass
    print(f"\n完成: 成功 {counters['ok']} / 失败 {counters['fail']} / 跳过 {counters['skip']} "
          f"/ {total} / 用时 {time.time()-t0:.0f}s")
    print(f"输出: {out.resolve()} | 失败清单: {failed_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
