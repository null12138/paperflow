#!/usr/bin/env python3
"""并发逐 DOI 下载驱动：每个 DOI 独立子进程 + 超时杀进程，并行执行避免单篇卡死拖垮整批。"""
import subprocess, sys, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE = Path("/Users/opener/code/abz")
dois = [l.strip() for l in (BASE / "doi_list_retry1.txt").read_text().splitlines() if l.strip()]
out_dir = "downloads"
mode = "oa+scihub+publisher"
timeout = 300   # 单篇秒数上限
workers = 4     # 并发数
failed_log = BASE / "download_failed_retry1.txt"
run_log = BASE / "retry1_run.log"
_lock = threading.Lock()

def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _lock:
        print(line, flush=True)
        with run_log.open("a") as f:
            f.write(line + "\n")

def work(doi: str) -> str:
    tmp = Path(f"/tmp/doi_one_{abs(hash(doi)) % 100000}.txt")
    tmp.write_text(doi + "\n")
    t0 = time.time()
    try:
        r = subprocess.run(
            [sys.executable, "-m", "paperflow.cli", "download",
             "--doi-file", str(tmp), "--out", out_dir, "--mode", mode,
             "--rpm", "60", "--db", "paperflow.db"],
            capture_output=True, text=True, timeout=timeout, cwd=str(BASE),
        )
        elapsed = time.time() - t0
        out = (r.stdout or "") + (r.stderr or "")
        tail = out.strip().replace("\n", " | ")[-260:]
        ok = "OK " in out
        if ok:
            return f"OK {elapsed:.0f}s {doi} | {tail}"
        with failed_log.open("a") as f:
            f.write(f"{doi}\t{tail}\n")
        return f"FAIL rc={r.returncode} {elapsed:.0f}s {doi} | {tail}"
    except subprocess.TimeoutExpired:
        elapsed = time.time() - t0
        with failed_log.open("a") as f:
            f.write(f"{doi}\tTIMEOUT({timeout}s)\n")
        return f"TIMEOUT {elapsed:.0f}s {doi}"

log(f"启动并发下载: {len(dois)} 篇, {workers} 并发, 每篇超时 {timeout}s")
done = 0
with ThreadPoolExecutor(max_workers=workers) as ex:
    futs = {ex.submit(work, d): d for d in dois}
    for fut in as_completed(futs):
        done += 1
        log(f"[{done}/{len(dois)}] {fut.result()}")
log("全部完成")