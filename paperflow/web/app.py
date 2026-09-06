"""Flask application for Paperflow's persistent Web control plane."""

from __future__ import annotations

import hmac
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from .. import __version__
from ..database import PaperDatabase
from ..paths import data_path, data_root, ensure_data_directories
from .jobs import JobStore, TASK_KINDS, download_archive_path, path_in_data_root


SOURCE_NAMES = ("WOS", "PubMed", "Europe PMC", "Crossref", "S2")
JOB_LABELS = {
    "search": "多源检索",
    "wos_fetch": "WOS 批量",
    "download_db": "数据库下载",
    "download_list": "DOI 清单下载",
    "preflight": "OA 候选预解析",
    "impact_factor": "导入影响因子",
    "export_report": "导出报告",
    "reconcile": "整理已有 PDF",
    "dedupe": "数据库去重",
    "full_run": "检索与下载全流程",
    "doctor": "环境自检",
}
PROTECTED_DATA_DIRS = {"sessions", "job-logs"}
PROTECTED_DATA_FILES = {"paperflow.db", "paperflow-jobs.db"}
PUBLIC_FILE_DIRS = {"downloads", "exports", "imports", "pdf_downloaded"}


def _public_message(value: str) -> str:
    """Convert worker output into concise, implementation-neutral UI text."""
    message = str(value or "").strip()
    match = re.match(r"^\[(\d+)\s*/\s*(\d+)\]\s*([✓✗]|OK|FAIL)\s*(.*)$", message, re.I)
    if match:
        done, total, marker, remainder = match.groups()
        title = remainder.split("|", 1)[0].strip()[:110]
        action = "已获取" if marker.casefold() in {"✓", "ok"} else "未获取"
        return f"[{done}/{total}] {action} · {title}"
    if message.startswith("数据库下载队列："):
        return message.replace("数据库下载队列：", "准备处理：", 1)
    if message.startswith("argv:") or "Traceback (most recent call last)" in message:
        return "任务正在准备"
    if message.startswith("参数错误："):
        return "任务参数有误，请重新创建任务"
    if "ModuleNotFoundError" in message:
        return "任务运行环境异常，请稍后重试"
    message = message.split("；数据库:", 1)[0].split("；数据库：", 1)[0]
    message = message.split("。摘要:", 1)[0].split("。摘要：", 1)[0]
    message = re.sub(r"(?:输出|摘要)\s*[:：]\s*\S+", "文件已生成", message)
    message = re.sub(r"/(?:mnt|opt|tmp|var)/\S+", "文件", message)
    message = message.split(" | ", 1)[0]
    for detail in ("Unpaywall", "OpenAlex", "Europe PMC", "scihub", "Sci-Hub"):
        message = message.replace(detail, "公开来源")
    return message[:240]


def _public_log(value: str) -> str:
    """Keep useful progress while hiding command lines, paths and source URLs."""
    output: list[str] = []
    for raw in str(value or "").splitlines()[-300:]:
        line = raw.strip()
        if not line or line.startswith(("argv:", "File \"", "Traceback", "During handling")):
            continue
        if line.startswith(("===", "^", "from ", "raise ")) or "site-packages/" in line:
            continue
        friendly = _public_message(line)
        if friendly and (not output or output[-1] != friendly):
            output.append(friendly)
    return "\n".join(output[-160:])


def _public_job(job: dict[str, Any], *, log: str = "") -> dict[str, Any]:
    allowed = (
        "id", "kind", "status", "message", "attempt", "resume_count",
        "created_at", "started_at", "finished_at", "updated_at", "return_code",
    )
    result = {key: job.get(key) for key in allowed}
    result["message"] = _public_message(str(job.get("message") or ""))
    result["txt_ready"] = job["kind"] == "search" and job["status"] == "succeeded" and data_path("exports", f"search-{job['id']}.txt").is_file()
    result["txt_url"] = url_for("search_results", job_id=job["id"]) if result["txt_ready"] else None
    if result["txt_ready"] and "ZIP" in result["message"]:
        result["message"] = "检索完成，可下载 TXT 文献清单"
    if log:
        result["log"] = _public_log(log)
    return result


def _number(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _web_limit(value: Any) -> int:
    """Parse the optional public limit: zero means unlimited."""
    raw = str(value if value is not None else "0").strip()
    if not raw:
        return 0
    try:
        number = int(raw)
    except (TypeError, ValueError):
        raise ValueError("篇数上限必须是 0 或正整数") from None
    if number < 0 or number > 100000:
        raise ValueError("篇数上限必须在 0 到 100000 之间")
    return number


def _optional_float(value: str) -> float | None:
    try:
        return float(value) if str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _lines(value: str) -> list[str]:
    return list(dict.fromkeys(line.strip() for line in value.replace(",", "\n").splitlines() if line.strip()))


def _doi_import_lines(value: str) -> list[str]:
    """Normalize a pasted/uploaded DOI list while preserving optional titles."""
    result: list[str] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(value.splitlines(), 1):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        doi, separator, title = raw_line.partition("\t")
        doi = re.sub(r"^doi\s*:\s*", "", doi.strip(), flags=re.IGNORECASE)
        doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE).strip().lower()
        if not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
            raise ValueError(f"第 {line_number} 行不是有效 DOI：{raw_line[:80]}")
        if doi in seen:
            continue
        seen.add(doi)
        result.append(f"{doi}\t{title.strip()}" if separator and title.strip() else doi)
    return result


def _publisher_links_for_job(job: dict[str, Any], limit: int = 5000) -> list[dict[str, str]]:
    """Return DOI resolver links for DOI-list items without a valid local PDF."""
    if job.get("kind") != "download_list":
        return []
    try:
        source_file = path_in_data_root(str((job.get("params") or {}).get("path") or ""), must_exist=True)
        raw_lines = source_file.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError, ValueError):
        return []
    dois: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_line in raw_lines:
        raw_doi, separator, title = raw_line.strip().partition("\t")
        doi = re.sub(r"^doi\s*:\s*", "", raw_doi.strip(), flags=re.IGNORECASE)
        doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE).strip().lower()
        if doi and doi not in seen and re.fullmatch(r"10\.\d{4,9}/\S+", doi):
            seen.add(doi)
            dois.append((doi, title.strip() if separator else ""))
    if int((job.get("params") or {}).get("limit") or 0) > 0:
        dois = dois[:int((job.get("params") or {}).get("limit"))]

    rows: dict[str, Any] = {}
    with PaperDatabase(data_path("paperflow.db")) as database:
        values = [doi for doi, _title in dois]
        for start in range(0, len(values), 500):
            chunk = values[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            for row in database.connection.execute(
                f"SELECT doi, title, pdf_path FROM papers WHERE doi IN ({placeholders})", chunk
            ):
                rows[row["doi"]] = row
    found: dict[str, str] = {}
    for doi, title in dois:
        row = rows.get(doi)
        if row and row["pdf_path"]:
            candidate = Path(row["pdf_path"])
            candidate = candidate if candidate.is_absolute() else data_root() / candidate
            try:
                candidate = candidate.resolve()
                candidate.relative_to(data_root())
                with candidate.open("rb") as handle:
                    if handle.read(5) == b"%PDF-":
                        continue
            except (OSError, ValueError):
                pass
        found[doi] = title or (row["title"] if row else doi)
    return [
        {"doi": doi, "title": title, "url": f"https://doi.org/{quote(doi, safe='/')}"}
        for doi, title in list(found.items())[:limit]
    ]


def _format_size(size: int | float | None) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} PB"


def _is_protected_data_path(path: Path) -> bool:
    try:
        relative = path.resolve().relative_to(data_root())
    except ValueError:
        return True
    if not relative.parts:
        return False
    return (
        relative.parts[0] not in PUBLIC_FILE_DIRS
        or any(part.startswith(".") for part in relative.parts)
        or relative.parts[0] in PROTECTED_DATA_DIRS
        or relative.name in PROTECTED_DATA_FILES
    )


def _job_params(kind: str) -> dict[str, Any]:
    common = {
        "email": request.form.get("email", "").strip(),
        "limit": request.form.get("limit", ""),
        "rpm": request.form.get("rpm", ""),
    }
    if kind in {"search", "wos_fetch"}:
        common["keywords"] = _lines(request.form.get("keywords", ""))
    if kind in {"search", "full_run"}:
        common["sources"] = request.form.getlist("sources") or ["WOS", "PubMed", "Europe PMC"]
    if kind == "wos_fetch":
        common.update({
            "max_records": request.form.get("max_records", "0"),
            "interval": request.form.get("interval", "1"),
        })
    elif kind == "download_db":
        common.update({
            "mode": "oa+scihub",
            "status": request.form.get("status", "pending").strip(),
            "keyword": request.form.get("keyword", "").strip(),
            "source": request.form.get("source", "").strip(),
            "min_if": request.form.get("min_if", "").strip(),
            "max_if": request.form.get("max_if", "").strip(),
        })
    elif kind == "download_list":
        common.update({
            "path": request.form.get("path", "").strip(),
            "mode": "oa+scihub",
        })
    elif kind == "preflight":
        common.update({
            "keyword": request.form.get("keyword", "").strip(),
            "source": request.form.get("source", "").strip(),
        })
    elif kind == "impact_factor":
        common.update({
            "path": request.form.get("path", "").strip(),
            "source": request.form.get("metric_source", "JCR").strip(),
            "year": request.form.get("year", "").strip(),
        })
    elif kind == "reconcile":
        common["paths"] = _lines(request.form.get("paths", "downloads\npdf_downloaded"))
    elif kind == "dedupe":
        execute = request.form.get("execute") == "1"
        if execute and request.form.get("confirm", "").strip() != "确认去重":
            raise ValueError("执行去重时请输入“确认去重”")
        common["dry_run"] = not execute
    elif kind == "full_run":
        common.update({
            "path": request.form.get("path", "input.txt").strip(),
            "mode": request.form.get("mode", "oa+scihub").strip(),
        })
    return common


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    ensure_data_directories()
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(
        SECRET_KEY=os.getenv("PAPERFLOW_WEB_SECRET") or secrets.token_hex(32),
        # Public Web uploads are for DOI/keyword lists, not arbitrary disk
        # images; keep the default bounded while allowing an explicit operator
        # override for trusted deployments.
        MAX_CONTENT_LENGTH=int(os.getenv("PAPERFLOW_WEB_MAX_UPLOAD", str(5 * 1024**2))),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if test_config:
        app.config.update(test_config)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]
    app.jinja_env.filters["filesize"] = _format_size
    app.jinja_env.filters["job_message"] = _public_message

    job_store: JobStore | None = None

    def store() -> JobStore:
        nonlocal job_store
        if job_store is None:
            job_store = JobStore()
        return job_store

    @app.before_request
    def protect() -> Any:
        if request.endpoint == "healthz" or request.endpoint == "static":
            return None
        username = os.getenv("PAPERFLOW_WEB_USERNAME", "").strip()
        password = os.getenv("PAPERFLOW_WEB_PASSWORD", "")
        anonymous = os.getenv("PAPERFLOW_WEB_ALLOW_ANONYMOUS", "").strip() == "1"
        if username and password:
            authorization = request.authorization
            valid = bool(
                authorization
                and hmac.compare_digest(authorization.username or "", username)
                and hmac.compare_digest(authorization.password or "", password)
            )
            if not valid:
                return ("需要登录", 401, {"WWW-Authenticate": 'Basic realm="Paperflow"'})
        elif not anonymous and not app.config.get("TESTING"):
            return "Web 登录凭据尚未配置", 503
        machine_json = (
            request.path.startswith("/api/v1/") and request.is_json
            and bool(username and password)
            and not request.headers.get("Origin")
            and not request.headers.get("Sec-Fetch-Site")
        )
        if request.method == "POST" and not machine_json and not app.config.get("WTF_CSRF_DISABLED"):
            expected = session.get("csrf_token", "")
            supplied = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
            if not expected or not supplied or not hmac.compare_digest(expected, supplied):
                abort(400, "CSRF 校验失败，请刷新页面后重试")
        return None

    @app.after_request
    def secure_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.endpoint != "static":
            response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; object-src 'none'",
        )
        return response

    @app.context_processor
    def shared_context() -> dict[str, Any]:
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(24)
            session["csrf_token"] = token
        return {
            "csrf_token": token,
            "job_labels": JOB_LABELS,
            "paperflow_version": __version__,
            "asset_version": os.getenv("PAPERFLOW_ASSET_VERSION", "20260905-search-txt"),
            "data_root_path": str(data_root()),
        }

    @app.get("/healthz")
    def healthz():
        try:
            with PaperDatabase(data_path("paperflow.db")) as database:
                database.stats()
            store().counts()
            return jsonify(status="ok")
        except Exception as exc:
            return jsonify(status="error", error=type(exc).__name__), 503

    @app.get("/ai")
    def ai_portal():
        """Human-readable landing page for AI clients and API integrators."""
        return render_template("ai.html")

    @app.get("/api/v1")
    def api_v1_docs():
        return jsonify({
            "name": "Paperflow AI API",
            "version": "1",
            "authentication": "HTTP Basic Auth",
            "endpoints": {
                "search": "GET /api/v1/papers?q=<text>&limit=20",
                "paper": "GET /api/v1/papers/<id>",
                "create_job": "POST /api/v1/jobs",
                "job": "GET /api/v1/jobs/<id>",
                "download": "GET /api/v1/jobs/<id>/archive",
            },
            "job_examples": {
                "metadata": {"workflow": "metadata", "items": ["Ginkgo biloba"], "limit": 20},
                "download_doi": {"workflow": "download", "mode": "doi", "items": ["10.1000/example"]},
                "download_keyword": {"workflow": "download", "mode": "keyword", "items": ["Panthera tigris"], "limit": 100},
            },
        })

    def _api_paper(row: Any) -> dict[str, Any]:
        import json
        raw = dict(row)
        value = {key: raw.get(key) for key in (
            "id", "title", "abstract", "doi", "pmid", "pmcid", "year", "journal"
        )}
        value["authors"] = json.loads(raw.get("authors_json") or "[]")
        value["sources"] = (raw.get("sources") or "").split(",") if raw.get("sources") else []
        value["has_pdf"] = bool(raw.get("pdf_path"))
        value["pdf_url"] = url_for("api_v1_pdf", paper_id=raw["id"]) if value["has_pdf"] else None
        return value

    @app.get("/api/v1/papers/<int:paper_id>/pdf")
    def api_v1_pdf(paper_id: int):
        with PaperDatabase(data_path("paperflow.db")) as database:
            paper = database.get_paper_detail(paper_id)
        if not paper or not paper.get("pdf_path"):
            abort(404)
        try:
            path = path_in_data_root(paper["pdf_path"], must_exist=True)
            if _is_protected_data_path(path) or not path.is_file():
                abort(404)
            with path.open("rb") as handle:
                if handle.read(5) != b"%PDF-":
                    abort(404)
        except (ValueError, OSError):
            abort(404)
        return send_file(path, as_attachment=True, download_name=path.name)

    @app.get("/api/v1/papers")
    def api_v1_papers():
        query = request.args.get("q", "").strip()
        try:
            limit = min(100, max(1, int(request.args.get("limit", "20"))))
            offset = max(0, int(request.args.get("offset", "0")))
        except ValueError:
            return jsonify(error="limit 和 offset 必须是整数"), 400
        with PaperDatabase(data_path("paperflow.db")) as database:
            rows = database.list_papers(text=query, limit=limit, offset=offset)
        return jsonify({"query": query, "limit": limit, "offset": offset, "results": [_api_paper(row) for row in rows]})

    @app.get("/api/v1/papers/<int:paper_id>")
    def api_v1_paper(paper_id: int):
        with PaperDatabase(data_path("paperflow.db")) as database:
            paper = database.get_paper_detail(paper_id)
        if paper is None:
            abort(404)
        return jsonify(_api_paper(paper))

    @app.post("/api/v1/jobs")
    def api_v1_create_job():
        response = api_create_job()
        return response

    @app.get("/api/v1/jobs/<int:job_id>")
    def api_v1_job(job_id: int):
        return api_job(job_id)

    @app.get("/api/v1/jobs/<int:job_id>/archive")
    def api_v1_archive(job_id: int):
        return download_archive(job_id)

    @app.get("/")
    def dashboard():
        # Render the primary workflow immediately. Task history is loaded in
        # one batched API request so a slow remote data mount cannot block UI.
        return render_template("dashboard.html")

    @app.get("/impact-factors")
    def impact_factors():
        query = request.args.get("q", "").strip()
        try: year = int(request.args.get("year", "0") or 0)
        except ValueError: year = 0
        with PaperDatabase(data_path("paperflow.db")) as database:
            params = [f"%{query}%"] if query else []
            sql = "SELECT journal, year, impact_factor, source FROM journal_metrics"
            if query: sql += " WHERE journal LIKE ?"
            if year: sql += (" AND" if query else " WHERE") + " year = ?"; params.append(year)
            sql += " ORDER BY year DESC, impact_factor DESC LIMIT 200"
            rows = [dict(r) for r in database.connection.execute(sql, params)]
        return render_template("impact_factors.html", rows=rows, query=query, year=year or "")

    @app.get("/api/v1/impact-factors")
    def api_impact_factors():
        query = request.args.get("q", "").strip()
        try: limit = min(200, max(1, int(request.args.get("limit", "50"))))
        except ValueError: return jsonify(error="limit 必须是整数"), 400
        with PaperDatabase(data_path("paperflow.db")) as database:
            if query:
                rows = database.connection.execute("SELECT journal, year, impact_factor, source FROM journal_metrics WHERE journal LIKE ? ORDER BY year DESC LIMIT ?", (f"%{query}%", limit)).fetchall()
            else:
                rows = database.connection.execute("SELECT journal, year, impact_factor, source FROM journal_metrics ORDER BY year DESC, impact_factor DESC LIMIT ?", (limit,)).fetchall()
        return jsonify({"query": query, "results": [dict(r) for r in rows]})

    @app.get("/library")
    def library():
        page = max(1, _number(request.args.get("page", "1"), 1))
        page_size = 100
        filters = {
            "keyword": request.args.get("keyword", "").strip(),
            "source": request.args.get("source", "").strip(),
            "status": request.args.get("status", "").strip(),
            "text": request.args.get("q", "").strip() or request.args.get("keyword", "").strip(),
            "min_if": _optional_float(request.args.get("min_if", "")),
            "max_if": _optional_float(request.args.get("max_if", "")),
        }
        with PaperDatabase(data_path("paperflow.db")) as database:
            rows = database.list_papers(
                source=filters["source"], status=filters["status"],
                min_if=filters["min_if"], max_if=filters["max_if"], text=filters["text"],
                limit=page_size + 1, offset=(page - 1) * page_size,
            )
        return render_template("library.html", papers=rows[:page_size], filters=filters, page=page, has_next=len(rows) > page_size)

    @app.get("/papers/<int:paper_id>")
    def paper_detail(paper_id: int):
        with PaperDatabase(data_path("paperflow.db")) as database:
            paper = database.get_paper_detail(paper_id)
        if paper is None:
            abort(404)
        return render_template("paper.html", paper=paper)

    @app.get("/jobs")
    def jobs():
        listed_jobs = [_public_job(job) for job in store().list(200)]
        for job in listed_jobs:
            job["archive_ready"] = (
                job["kind"] in {"download_list", "download_db", "full_run"} and job["status"] == "succeeded" and download_archive_path(job["id"]).is_file()
            )
        return render_template(
            "jobs.html", jobs=listed_jobs, sources=SOURCE_NAMES
        )

    @app.get("/downloads")
    def downloads():
        return redirect(url_for("dashboard"))

    @app.post("/downloads/import")
    def import_dois():
        chunks = [request.form.get("dois", "")]
        upload = request.files.get("file")
        if upload is not None and upload.filename:
            payload = upload.read(5 * 1024 * 1024 + 1)
            if len(payload) > 5 * 1024 * 1024:
                abort(413, "DOI 文件不能超过 5 MB")
            try:
                chunks.append(payload.decode("utf-8-sig"))
            except UnicodeDecodeError:
                flash("DOI 文件必须是 UTF-8 文本", "error")
                return redirect(url_for("downloads"))
        try:
            lines = _doi_import_lines("\n".join(chunks))
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("downloads"))
        if not lines:
            flash("请粘贴 DOI 或选择 DOI 文本文件", "error")
            return redirect(url_for("downloads"))

        filename = f"doi-import-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}.txt"
        target = data_path("imports", filename)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        job_id = store().enqueue("download_list", {
            "path": str(target.relative_to(data_root())),
            "mode": "oa+scihub",
            "limit": 0,
            "rpm": request.form.get("rpm", "30"),
            "email": request.form.get("email", "").strip(),
        })
        flash(f"已导入 {len(lines)} 个 DOI，下载任务 #{job_id} 已进入队列", "success")
        return redirect(url_for("job_detail", job_id=job_id))

    @app.post("/jobs/new/<kind>")
    def new_job(kind: str):
        if kind not in TASK_KINDS:
            abort(404)
        try:
            job_id = store().enqueue(kind, _job_params(kind))
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("jobs"))
        flash(f"任务 #{job_id} 已进入持久队列", "success")
        return redirect(url_for("job_detail", job_id=job_id))

    @app.get("/jobs/<int:job_id>")
    def job_detail(job_id: int):
        job = store().get(job_id)
        if job is None:
            abort(404)
        archive_ready = job["kind"] in {"download_list", "download_db", "full_run"} and job["status"] == "succeeded" and download_archive_path(job_id).is_file()
        return render_template(
            "job.html", job=_public_job(job), log=_public_log(store().read_log(job)),
            archive_ready=archive_ready,
        )

    @app.get("/jobs/<int:job_id>/results.txt")
    def search_results(job_id: int):
        job = store().get(job_id)
        target = data_path("exports", f"search-{job_id}.txt")
        if not job or job["kind"] != "search" or job["status"] != "succeeded" or not target.is_file():
            abort(404)
        return send_file(target, as_attachment=True, download_name=target.name)

    @app.get("/downloads/<int:job_id>/archive")
    def download_archive(job_id: int):
        job = store().get(job_id)
        archive = download_archive_path(job_id)
        if job is None or job["kind"] not in {"download_list", "download_db", "full_run"} or job["status"] != "succeeded" or not archive.is_file():
            abort(404)
        return send_file(archive, as_attachment=True, download_name=archive.name)

    @app.get("/downloads/<int:job_id>/publisher-links")
    def publisher_links(job_id: int):
        job = store().get(job_id)
        if job is None or job["kind"] != "download_list":
            abort(404)
        links = _publisher_links_for_job(job)
        return render_template("publisher_links.html", job=job, links=links)

    @app.post("/jobs/<int:job_id>/cancel")
    def cancel_job(job_id: int):
        if not store().request_cancel(job_id):
            flash("任务不存在或已结束", "error")
        return redirect(url_for("job_detail", job_id=job_id))

    @app.post("/jobs/<int:job_id>/retry")
    def retry_job(job_id: int):
        if not store().retry(job_id):
            flash("只有失败或已取消任务可以重试", "error")
        return redirect(url_for("job_detail", job_id=job_id))

    @app.get("/api/jobs")
    def api_jobs():
        result = []
        for job in store().list(100):
            public = _public_job(job)
            public["archive_ready"] = (
                job["kind"] in {"download_list", "download_db", "full_run"} and job["status"] == "succeeded" and download_archive_path(job["id"]).is_file()
            )
            result.append(public)
        return jsonify(result)

    @app.post("/api/jobs")
    def api_create_job():
        """Small JSON API used by the quiet homepage.

        The public Web workflow deliberately has one fixed search policy and one
        fixed download policy; advanced/legacy controls remain on /jobs.
        """
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="请求必须是 JSON 对象"), 400
        workflow = str(payload.get("workflow") or "download").strip().casefold()
        mode = str(payload.get("mode") or "keyword").strip().casefold()
        items = payload.get("items") or []
        if not isinstance(items, list):
            return jsonify(error="items 必须是数组"), 400
        items = list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))
        if len(items) > 10000 or sum(len(item) for item in items) > 1_000_000:
            return jsonify(error="单个任务最多 10,000 条内容，文本总量不能超过 1 MB"), 413
        if any(len(item) > 500 for item in items):
            return jsonify(error="单条关键词或 DOI 不能超过 500 个字符"), 400
        if not items:
            return jsonify(error="请至少提供一条关键词或 DOI"), 400
        try:
            limit = _web_limit(payload.get("limit", 0))
            if workflow == "metadata":
                if mode != "keyword":
                    raise ValueError("摘要检索只接受关键词")
                job_id = store().enqueue("search", {
                    "keywords": items,
                    "sources": ["WOS", "PubMed", "Europe PMC"],
                    "limit": limit,
                    "email": str(payload.get("email") or "").strip(),
                })
            elif workflow == "download":
                if mode == "doi":
                    normalized = _doi_import_lines("\n".join(items))
                    if limit > 0:
                        normalized = normalized[:limit]
                    filename = f"web-doi-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}.txt"
                    target = data_path("imports", filename)
                    target.write_text("\n".join(normalized) + "\n", encoding="utf-8")
                    job_id = store().enqueue("download_list", {
                        "path": str(target.relative_to(data_root())),
                        "mode": "oa+scihub",
                        "limit": limit,
                        "rpm": 30,
                        "email": str(payload.get("email") or "").strip(),
                    })
                elif mode == "keyword":
                    filename = f"web-keywords-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}.txt"
                    target = data_path("imports", filename)
                    target.write_text("\n".join(items) + "\n", encoding="utf-8")
                    job_id = store().enqueue("full_run", {
                        "path": str(target.relative_to(data_root())),
                        "keywords": items,
                        "sources": ["WOS", "PubMed", "Europe PMC"],
                        "mode": "oa+scihub",
                        "limit": limit,
                        "rpm": 30,
                        "email": str(payload.get("email") or "").strip(),
                    })
                else:
                    raise ValueError("不支持的输入模式")
            else:
                raise ValueError("不支持的工作流")
        except (ValueError, OSError, UnicodeError) as exc:
            return jsonify(error=str(exc)), 400
        return jsonify({
            "id": job_id,
            "status": "queued",
            "message": "任务已进入队列",
            "url": url_for("job_detail", job_id=job_id),
        }), 202

    @app.get("/api/jobs/<int:job_id>")
    def api_job(job_id: int):
        job = store().get(job_id)
        if job is None:
            abort(404)
        public = _public_job(job, log=store().read_log(job))
        public["archive_url"] = (job.get("archive_url") or url_for("api_v1_archive", job_id=job_id)) if job["kind"] in {"download_list", "download_db", "full_run"} and job["status"] == "succeeded" and download_archive_path(job_id).is_file() else None
        public["archive_ready"] = (
            job["kind"] in {"download_list", "download_db", "full_run"} and job["status"] == "succeeded" and download_archive_path(job_id).is_file()
        )
        return jsonify(public)

    @app.get("/files")
    def files():
        relative = request.args.get("path", "").strip("/")
        try:
            current = path_in_data_root(relative or ".", must_exist=True)
        except ValueError as exc:
            abort(400, str(exc))
        if _is_protected_data_path(current):
            abort(404)
        if current.is_file():
            return redirect(url_for("download_file", relative=str(current.relative_to(data_root()))))
        entries = []
        try:
            children = sorted(current.iterdir(), key=lambda path: (not path.is_dir(), path.name.casefold()))[:500]
        except OSError as exc:
            abort(503, str(exc))
        for child in children:
            try:
                if _is_protected_data_path(child):
                    continue
                stat = child.stat()
                entries.append({
                    "name": child.name,
                    "relative": str(child.relative_to(data_root())),
                    "is_dir": child.is_dir(),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                })
            except OSError:
                continue
        parent = None if current == data_root() else str(current.parent.relative_to(data_root()))
        can_upload = bool(relative and relative.split("/", 1)[0] == "imports")
        return render_template(
            "files.html", entries=entries, relative=relative, parent=parent,
            can_upload=can_upload,
        )

    @app.get("/files/download/<path:relative>")
    def download_file(relative: str):
        try:
            path = path_in_data_root(relative, must_exist=True)
        except ValueError as exc:
            abort(400, str(exc))
        if _is_protected_data_path(path):
            abort(404)
        if not path.is_file():
            abort(404)
        return send_file(path, as_attachment=request.args.get("inline") != "1", download_name=path.name)

    @app.post("/files/upload")
    def upload_file():
        relative = request.form.get("path", "").strip("/")
        try:
            directory = path_in_data_root(relative or ".", must_exist=True)
        except ValueError as exc:
            abort(400, str(exc))
        if _is_protected_data_path(directory):
            abort(404)
        try:
            upload_relative = directory.relative_to(data_root())
        except ValueError:
            abort(404)
        if not upload_relative.parts or upload_relative.parts[0] != "imports":
            abort(404)
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            flash("请选择文件", "error")
            return redirect(url_for("files", path=relative))
        filename = Path(upload.filename).name
        if filename in {"", ".", ".."}:
            abort(400, "文件名无效")
        target = directory / filename
        if target.exists():
            flash("同名文件已存在，未覆盖", "error")
            return redirect(url_for("files", path=relative))
        upload.save(target)
        flash(f"已上传 {filename}", "success")
        return redirect(url_for("files", path=relative))

    @app.get("/settings")
    def settings():
        keys = (
            "WOS_API_KEY", "S2_API_KEY", "UNPAYWALL_EMAIL", "NCBI_EMAIL",
            "NCBI_API_KEY", "PAPERFLOW_PROXIES",
        )
        sessions_dir = data_path("sessions")
        session_files = ["scihub.json"] if (sessions_dir / "scihub.json").is_file() else []
        return render_template(
            "settings.html",
            environment={key: bool(os.getenv(key, "").strip()) for key in keys},
            session_files=session_files,
            database_path=str(data_path("paperflow.db")),
            jobs_path=str(data_path("paperflow-jobs.db")),
        )

    return app


def main() -> int:
    from waitress import serve

    host = os.getenv("PAPERFLOW_WEB_HOST", "127.0.0.1")
    port = _number(os.getenv("PAPERFLOW_WEB_PORT", "8765"), 8765)
    serve(create_app(), host=host, port=port, threads=8)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
