#!/usr/bin/env python3
"""基于 DOI 的 Sci-Hub 批量 PDF 下载工具（altcha 验证码自动求解 + 多镜像/多代理 failover）。

- 输入：TSV(DOI<TAB>Title) 或纯文本(一行一个 DOI)
- 每个 DOI 依次尝试多个镜像；镜像遇 altcha 自动求解，遇 Cloudflare/广告站/不可达自动跳过
- 多代理 failover：Clash 7890 → 机场代理
- 并发下载、PDF 头校验、断点续跑
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from scihub_core import (
    DEFAULT_PROXIES,
    TIMEOUT,
    build_session,
    parse_scihub_page,
    solve_captcha,
)

COOKIE_FILE = Path(__file__).resolve().parent / "scihub_cookies.json"
COOKIE_GEN = Path(__file__).resolve().parent / "scihub_get_cookies.py"


def load_cookies() -> list[dict]:
    if COOKIE_FILE.exists():
        try:
            return json.loads(COOKIE_FILE.read_text())
        except Exception:
            pass
    return []


def refresh_cookies() -> list[dict]:
    """调用 Playwright 重新获取 DDoS-Guard 放行 cookie。"""
    print("刷新 DDoS-Guard cookie...", flush=True)
    try:
        subprocess.run([sys.executable, str(COOKIE_GEN)], timeout=120, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        try:
            subprocess.run(["/usr/bin/python3", str(COOKIE_GEN)], timeout=120, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            print(f"cookie 刷新失败: {exc}", flush=True)
    return load_cookies()


_cookie_pool: list[list[dict]] = []
_cookie_state = {"idx": 0}


def next_session(proxy: str) -> requests.Session:
    """建 session 并注入当前 cookie 池中的一个。"""
    s = build_session(proxy)
    with threading.Lock():
        if _cookie_pool:
            cookies = _cookie_pool[_cookie_state["idx"] % len(_cookie_pool)]
            _cookie_state["idx"] += 1
        else:
            cookies = load_cookies()
    for c in cookies:
        s.cookies.set(c["name"], c["value"], domain=c["domain"].lstrip("."), path=c.get("path", "/"))
    return s

log = logging.getLogger("scihub")

# 最优镜像（体检确认：双代理可达 ~1s、altcha 可解）
MIRRORS = [
    "https://sci-hub.jp/",
    # 以下为备选（响应慢/不稳，自动降级）
    "https://sci-hub.su/",
    "https://sci-hub.ru/",
]

# 镜像黑名单（体检证实是广告站/无效）
BLACKLISTED = {"sci-hub.de", "sci-hub.cc", "sci-hub.it", "sci-hub.nl", "sci-hub.pl", "sci-hub.si", "sci.hubg.org"}


def clean_filename(title: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", title).strip("._")[:160] or "paper"


def download_one(doi: str, title: str, out: Path, worker_id: int = 0) -> tuple[str, bool, str]:
    """对单个 DOI 尝试镜像×代理；DDoS 拦截时刷新 cookie 重试。"""
    fname = out / (clean_filename(title or doi) + ".pdf")
    if fname.exists() and fname.read_bytes()[:5] == b"%PDF-":
        return doi, True, "already-exists"
    mirrors = [m for m in MIRRORS if m.split("//")[1].rstrip("/") not in BLACKLISTED]
    errors = []
    for attempt in range(3):
        session = next_session(DEFAULT_PROXIES[0])
        for mirror in mirrors:
            url = mirror.rstrip("/") + "/" + doi.rstrip("/")
            try:
                page = parse_scihub_page(session, url)
            except Exception as exc:
                errors.append(f"{mirror}: {type(exc).__name__}")
                continue
            if page.kind == "captcha":
                page = solve_captcha(session, url) or page
            if page.kind in ("pdf", "framepdf"):
                try:
                    r = session.get(page.pdf_url, timeout=TIMEOUT + 15)
                    if r.content[:5] == b"%PDF-":
                        tmp = fname.with_suffix(".part")
                        tmp.write_bytes(r.content)
                        tmp.rename(fname)
                        return doi, True, page.pdf_url
                    errors.append(f"{mirror}: 非PDF响应")
                except Exception as exc:
                    errors.append(f"{mirror}: {type(exc).__name__}: {str(exc)[:80]}")
            else:
                if page.kind == "cloudflare" and "DDoS" in page.notice and attempt < 2:
                    _cookie_pool.clear()
                    _cookie_pool.append(refresh_cookies())
                    errors.append(f"{mirror}: DDoS-Guard(已刷新cookie，重试)")
                    break  # 换新 cookie 重新整轮
                errors.append(f"{mirror}: {page.kind}")
    return doi, False, "; ".join(errors)[:600] or "全部镜像失败"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="TSV(doi<TAB>title) 或纯txt(一行一个DOI)")
    parser.add_argument("--out", type=Path, default=Path("scihub_downloads"))
    parser.add_argument("--workers", type=int, default=24, help="并发线程数")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条（0=全部）")
    args = parser.parse_args()

    lines = [ln.strip() for ln in args.input.read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
    items: list[tuple[str, str]] = []
    for ln in lines:
        if "\t" in ln:
            doi, title = ln.split("\t", 1)
        else:
            doi, title = ln.rsplit("/", 1)[-1], ""
        doi = doi.strip().rstrip(".").lower()
        if doi:
            items.append((doi, title))
    if args.limit:
        items = items[: args.limit]

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    _cookie_pool.append(load_cookies() or refresh_cookies())
    lock = threading.Lock()
    counters = {"ok": 0, "fail": 0, "skip": 0}
    total = len(items)
    failed_path = out.parent / "scihub_failed.txt"

    def worker(item: tuple[str, str]) -> None:
        doi, title = item
        fname = out / (clean_filename(title or doi) + ".pdf")
        if fname.exists() and fname.read_bytes()[:5] == b"%PDF-":
            with lock:
                counters["skip"] += 1
                print(f"[{sum(counters.values())}/{total}] 跳过已有 {doi}")
            return
        doi, took, info = download_one(doi, title, out)
        with lock:
            if took:
                counters["ok"] += 1
                print(f"[{sum(counters.values())}/{total}] OK {doi}")
            else:
                counters["fail"] += 1
                with failed_path.open("a", encoding="utf-8") as f:
                    f.write(f"{doi}\t{title}\t{info}\n")
                print(f"[{sum(counters.values())}/{total}] FAIL {doi} | {info[:120]}")

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for _ in pool.map(worker, items):
            pass
    print(f"\n完成: 成功 {counters['ok']} / 失败 {counters['fail']} / 跳过 {counters['skip']} "
          f"/ 总数 {total} / 用时 {time.time()-t0:.0f}s")
    print(f"输出目录: {out.resolve()} | 失败清单: {failed_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())