"""Single persistent worker that executes queued Paperflow jobs."""

from __future__ import annotations

import fcntl
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path

from ..paths import data_root, ensure_data_directories
from .jobs import JobStore, build_command, create_download_archive


class Worker:
    def __init__(self, store: JobStore | None = None, poll_interval: float = 2.0) -> None:
        self.store = store or JobStore()
        self.poll_interval = max(0.2, poll_interval)
        self.stopping = False
        self.process: subprocess.Popen[str] | None = None

    def stop(self, *_args) -> None:
        self.stopping = True
        if self.process and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def run_job(self, job: dict) -> None:
        job_id = int(job["id"])
        try:
            command = build_command(job)
        except Exception as exc:
            self.store.fail(job_id, f"参数错误：{exc}")
            return

        log_path = Path(job["log_path"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment["PAPERFLOW_DATA_ROOT"] = str(data_root())
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PAPERFLOW_JOB_ID"] = str(job_id)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n=== attempt {job['attempt']} / resume {job['resume_count']} ===\n")
            log.write("argv: " + " ".join(command) + "\n")
            log.flush()
            try:
                self.process = subprocess.Popen(
                    command,
                    cwd=str(data_root()),
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
            except Exception as exc:
                log.write(f"启动失败：{type(exc).__name__}: {exc}\n")
                self.store.fail(job_id, f"任务启动失败：{exc}")
                return
            self.store.set_pid(job_id, self.process.pid)
            selector = selectors.DefaultSelector()
            assert self.process.stdout is not None
            selector.register(self.process.stdout, selectors.EVENT_READ)
            last_line = ""
            terminated_at = 0.0
            while self.process.poll() is None:
                for key, _ in selector.select(timeout=1.0):
                    line = key.fileobj.readline()
                    if line:
                        log.write(line)
                        log.flush()
                        last_line = line.strip()
                        if last_line:
                            self.store.touch(job_id, last_line)
                should_cancel = self.stopping or self.store.cancel_requested(job_id)
                if should_cancel and not terminated_at:
                    terminated_at = time.monotonic()
                    try:
                        os.killpg(self.process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                elif terminated_at and time.monotonic() - terminated_at > 10:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            for line in self.process.stdout:
                log.write(line)
                if line.strip():
                    last_line = line.strip()
            return_code = int(self.process.returncode or 0)
            if self.stopping and not self.store.cancel_requested(job_id):
                self.store.touch(job_id, "worker 停止，任务将在服务恢复后自动续跑")
                self.process = None
                return
            if return_code == 0 and job["kind"] in {"download_list", "download_db", "full_run"}:
                try:
                    archive, included = create_download_archive(job)
                    archive_message = f"ZIP 已生成：{archive.name}（{included} 篇 PDF）"
                    log.write(archive_message + "\n")
                    log.flush()
                    last_line = archive_message
                except Exception as exc:
                    return_code = 1
                    last_line = f"PDF 已处理，但生成 ZIP 失败：{type(exc).__name__}: {exc}"
                    log.write(last_line + "\n")
                    log.flush()
            self.store.finish(job_id, return_code, last_line)
            self.process = None

    def run_forever(self) -> None:
        ensure_data_directories()
        recovered = self.store.recover_interrupted()
        if recovered:
            print(f"恢复 {recovered} 个中断任务", flush=True)
        while not self.stopping:
            job = self.store.claim_next()
            if job is None:
                time.sleep(self.poll_interval)
                continue
            self.run_job(job)


def main() -> int:
    lock_path = Path(os.getenv("PAPERFLOW_WORKER_LOCK", "/run/lock/paperflow-worker.lock"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("另一个 Paperflow worker 已在运行", flush=True)
        return 2
    worker = Worker()
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
