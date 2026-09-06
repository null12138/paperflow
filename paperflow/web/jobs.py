"""Persistent Web job queue and safe CLI command construction."""

from __future__ import annotations

import json
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import normalize_doi
from ..paths import data_path, data_root, ensure_data_directories


TASK_KINDS = {
    "search",
    "wos_fetch",
    "download_db",
    "download_list",
    "preflight",
    "impact_factor",
    "export_report",
    "reconcile",
    "dedupe",
    "full_run",
    "doctor",
}
WEB_DOWNLOAD_CHANNELS = {"oa", "scihub", "direct", "pmc"}

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',
    message TEXT NOT NULL DEFAULT '',
    log_path TEXT NOT NULL DEFAULT '',
    pid INTEGER NOT NULL DEFAULT 0,
    return_code INTEGER,
    attempt INTEGER NOT NULL DEFAULT 0,
    resume_count INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_id ON jobs(status, id);
"""


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    try:
        result["params"] = json.loads(result.pop("params_json") or "{}")
    except (TypeError, ValueError):
        result["params"] = {}
    return result


def path_in_data_root(value: str, *, must_exist: bool = False) -> Path:
    """Resolve a user-supplied relative path without escaping the data root."""
    root = data_root()
    candidate = Path(value or ".")
    candidate = candidate if candidate.is_absolute() else root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError("路径必须位于 PAPERFLOW_DATA_ROOT 内") from None
    if must_exist and not candidate.exists():
        raise ValueError(f"文件或目录不存在：{candidate.relative_to(root)}")
    return candidate


class JobStore:
    def __init__(self, path: Path | None = None) -> None:
        ensure_data_directories()
        self.path = Path(path or data_path("paperflow-jobs.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            # Setting journal_mode is a write-like filesystem operation.  Do it
            # once at store initialization, not for every read on remote mounts.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def enqueue(self, kind: str, params: dict[str, Any] | None = None) -> int:
        if kind not in TASK_KINDS:
            raise ValueError(f"不支持的任务类型：{kind}")
        payload = json.dumps(params or {}, ensure_ascii=False, separators=(",", ":"))
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO jobs(kind, params_json) VALUES (?, ?)", (kind, payload)
            )
            return int(cursor.lastrowid)

    def get(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            return _row_dict(connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)
            ).fetchall()
        return [_row_dict(row) for row in rows if row is not None]

    def counts(self) -> dict[str, int]:
        result = {status: 0 for status in ("queued", "running", "succeeded", "failed", "cancelled")}
        with self.connect() as connection:
            for row in connection.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"):
                result[row["status"]] = int(row["count"])
        return result

    def recover_interrupted(self) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE jobs
                   SET status = 'queued', pid = 0, cancel_requested = 0,
                       resume_count = resume_count + 1,
                       message = '服务重启后自动恢复，等待断点续跑',
                       updated_at = CURRENT_TIMESTAMP
                   WHERE status = 'running'"""
            )
            return int(cursor.rowcount)

    def claim_next(self) -> dict[str, Any] | None:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            params = json.loads(row["params_json"] or "{}")
            retry_status = str(params.get("status") or "pending")
            if (
                row["kind"] == "download_db"
                and retry_status in {"failed", "all", "candidate", "candidate-pmc"}
                and not params.get("attempt_before")
            ):
                params["attempt_before"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            log_path = str(data_path("job-logs", f"job-{row['id']}.log"))
            connection.execute(
                """UPDATE jobs
                   SET status = 'running', log_path = ?, params_json = ?, pid = 0,
                       attempt = attempt + 1, cancel_requested = 0,
                       started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                       finished_at = NULL, return_code = NULL,
                       message = '任务已由 worker 接管', updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND status = 'queued'""",
                (log_path, json.dumps(params, ensure_ascii=False, separators=(",", ":")), row["id"]),
            )
            connection.commit()
            return self.get(int(row["id"]))
        finally:
            connection.close()

    def set_pid(self, job_id: int, pid: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET pid = ?, message = '任务运行中', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (pid, job_id),
            )

    def touch(self, job_id: int, message: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET message = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (message[:300], job_id),
            )

    def finish(self, job_id: int, return_code: int, message: str = "") -> None:
        job = self.get(job_id)
        cancelled = bool(job and job.get("cancel_requested"))
        status = "cancelled" if cancelled else ("succeeded" if return_code == 0 else "failed")
        final_message = message or (
            "任务已取消" if cancelled else ("任务完成" if return_code == 0 else f"任务失败（退出码 {return_code}）")
        )
        with self.connect() as connection:
            connection.execute(
                """UPDATE jobs SET status = ?, return_code = ?, pid = 0,
                       message = ?, finished_at = CURRENT_TIMESTAMP,
                       updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (status, return_code, final_message[:300], job_id),
            )

    def fail(self, job_id: int, message: str) -> None:
        self.finish(job_id, 1, message)

    def request_cancel(self, job_id: int) -> bool:
        with self.connect() as connection:
            row = connection.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None or row["status"] not in {"queued", "running"}:
                return False
            if row["status"] == "queued":
                connection.execute(
                    """UPDATE jobs SET status = 'cancelled', cancel_requested = 1,
                           message = '任务在启动前取消', finished_at = CURRENT_TIMESTAMP,
                           updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                    (job_id,),
                )
            else:
                connection.execute(
                    """UPDATE jobs SET cancel_requested = 1, message = '正在请求停止任务',
                           updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                    (job_id,),
                )
            return True

    def retry(self, job_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE jobs SET status = 'queued', pid = 0, return_code = NULL,
                       cancel_requested = 0, message = '已手动重新排队',
                       finished_at = NULL, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND status IN ('failed', 'cancelled')""",
                (job_id,),
            )
            return bool(cursor.rowcount)

    def cancel_requested(self, job_id: int) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return bool(row and row[0])

    def read_log(self, job: dict[str, Any], tail_bytes: int = 200_000) -> str:
        path = Path(job.get("log_path") or data_path("job-logs", f"job-{job['id']}.log"))
        if not path.is_file():
            return ""
        with path.open("rb") as handle:
            size = path.stat().st_size
            if size > tail_bytes:
                handle.seek(-tail_bytes, 2)
            return handle.read().decode("utf-8", errors="replace")


def _add(command: list[str], flag: str, value: Any, *, allow_empty: bool = False) -> None:
    if value is None or (not allow_empty and value == ""):
        return
    command.extend((flag, str(value)))


def _positive_int(value: Any, default: int, *, zero_allowed: bool = False) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    minimum = 0 if zero_allowed else 1
    return max(minimum, number)


def web_download_mode(value: Any) -> str:
    tokens = [part.strip().casefold() for part in str(value or "oa+scihub").replace(",", "+").split("+") if part.strip()]
    invalid = sorted(set(tokens) - WEB_DOWNLOAD_CHANNELS)
    if invalid:
        raise ValueError(f"Web 版只允许 OA/PMC/直链和 Sci-Hub 通道：{', '.join(invalid)}")
    return "+".join(dict.fromkeys(tokens)) or "oa+scihub"


def download_archive_path(job_id: int) -> Path:
    return data_path("exports", f"pdf-task-{int(job_id)}.zip")


def create_download_archive(job: dict[str, Any]) -> tuple[Path, int]:
    from .archive import create_archive
    return create_archive(job)


def build_command(job: dict[str, Any]) -> list[str]:
    """Translate a persisted, validated job into an argv list (never a shell command)."""
    kind = job["kind"]
    params = job.get("params") or {}
    root = data_root()
    database = data_path("paperflow.db")
    command = [sys.executable, "-m", "paperflow"]

    if kind == "search":
        keywords = [str(value).strip() for value in params.get("keywords", []) if str(value).strip()]
        if not keywords:
            raise ValueError("检索任务至少需要一个关键词")
        command.append("search")
        for keyword in keywords:
            _add(command, "--species", keyword)
        _add(command, "--sources", ",".join(params.get("sources") or ["WOS", "PubMed", "Europe PMC"]))
        _add(command, "--limit", _positive_int(params.get("limit"), 0, zero_allowed=True))
        _add(command, "--email", params.get("email", ""))
        _add(command, "--out", data_path("exports", f"search-{job['id']}.txt"))
        _add(command, "--db", database)
    elif kind == "wos_fetch":
        keywords = [str(value).strip() for value in params.get("keywords", []) if str(value).strip()]
        if not keywords:
            raise ValueError("WOS 任务至少需要一个关键词")
        command.append("wos-fetch")
        for keyword in keywords:
            _add(command, "--keyword", keyword)
        _add(command, "--max-records", _positive_int(params.get("max_records"), 0, zero_allowed=True))
        _add(command, "--interval", params.get("interval", 1.0))
        _add(command, "--manifest", data_path("wos_api_runs", "manifest.json"))
        _add(command, "--db", database)
    elif kind == "download_db":
        status = str(params.get("status") or "pending")
        command.extend(("download-db", "--db", str(database), "--out", str(data_path("downloads"))))
        _add(command, "--mode", web_download_mode(params.get("mode")))
        _add(command, "--status", status)
        _add(command, "--keyword", params.get("keyword", ""))
        _add(command, "--source", params.get("source", ""))
        _add(command, "--limit", _positive_int(params.get("limit"), 0, zero_allowed=True))
        _add(command, "--rpm", _positive_int(params.get("rpm"), 60))
        _add(command, "--email", params.get("email", ""))
        _add(command, "--min-if", params.get("min_if", ""))
        _add(command, "--max-if", params.get("max_if", ""))
        _add(command, "--attempt-before", params.get("attempt_before", ""))
    elif kind == "download_list":
        source_file = path_in_data_root(str(params.get("path") or ""), must_exist=True)
        command.extend(("download", "--doi-file", str(source_file), "--out", str(data_path("downloads"))))
        # Web download jobs always try legal OA/PMC/direct locations first,
        # then the configured fallback channel; keep the persisted task
        # resumable even when it was created by an older Web release.
        _add(command, "--mode", "oa+scihub")
        _add(command, "--rpm", _positive_int(params.get("rpm"), 30))
        _add(command, "--limit", _positive_int(params.get("limit"), 0, zero_allowed=True))
        _add(command, "--email", params.get("email", ""))
        _add(command, "--failed", data_path("exports", f"download-failed-{job['id']}.txt"))
        _add(command, "--db", database)
    elif kind == "preflight":
        command.extend(("download-preflight", "--db", str(database)))
        _add(command, "--keyword", params.get("keyword", ""))
        _add(command, "--source", params.get("source", ""))
        _add(command, "--limit", _positive_int(params.get("limit"), 0, zero_allowed=True))
        _add(command, "--email", params.get("email", ""))
    elif kind == "impact_factor":
        source_file = path_in_data_root(str(params.get("path") or ""), must_exist=True)
        command.extend(("impact-factor", "import", "--file", str(source_file), "--db", str(database)))
        _add(command, "--source", params.get("source") or "JCR")
        _add(command, "--year", params.get("year", ""))
    elif kind == "export_report":
        command.extend((
            "db", "export-report", "--db", str(database),
            "--abstracts", str(data_path("exports", "abstracts.txt")),
            "--summary", str(data_path("exports", "summary.txt")),
        ))
    elif kind == "reconcile":
        command.extend(("db", "reconcile-pdfs", "--db", str(database)))
        paths = params.get("paths") or ["downloads", "pdf_downloaded"]
        for value in paths:
            _add(command, "--scan-dir", path_in_data_root(str(value), must_exist=True))
        _add(command, "--pdf-dir", data_path("pdf_downloaded"))
    elif kind == "dedupe":
        command.extend(("db", "dedupe", "--db", str(database)))
        if bool(params.get("dry_run", True)):
            command.append("--dry-run")
    elif kind == "full_run":
        input_path = path_in_data_root(str(params.get("path") or "input.txt"), must_exist=True)
        # A full run writes all search results before starting PDF work.  On a
        # worker restart, resume the durable database download queue instead of
        # repeating the expensive WOS/PubMed search and rebuilding the corpus.
        if int(job.get("resume_count") or 0) > 0:
            command.extend(("download-db", "--db", str(database), "--out", str(data_path("downloads"))))
            _add(command, "--mode", "oa+scihub")
            # Continue only papers not attempted in this run. Failed items
            # remain available to a separate retry task, avoiding repeated
            # mirror/API work after every worker restart.
            _add(command, "--status", "pending")
            _add(command, "--limit", 0)
            _add(command, "--rpm", _positive_int(params.get("rpm"), 30))
            _add(command, "--email", params.get("email", ""))
            return command
        command.extend(("run", "--input", str(input_path), "--db", str(database)))
        _add(command, "--sources", ",".join(params.get("sources") or ["WOS", "PubMed", "Europe PMC"]))
        _add(command, "--out", data_path("downloads"))
        _add(command, "--summary", data_path("exports", f"run-summary-{job['id']}.txt"))
        _add(command, "--mode", "oa+scihub")
        _add(command, "--rpm", _positive_int(params.get("rpm"), 30))
        _add(command, "--limit", _positive_int(params.get("limit"), 0, zero_allowed=True))
        _add(command, "--email", params.get("email", ""))
    elif kind == "doctor":
        command.append("doctor")
    else:
        raise ValueError(f"不支持的任务类型：{kind}")
    return command
