import base64
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paperflow.database import PaperDatabase
from paperflow.models import Paper
from paperflow.web.jobs import JobStore


class WebAppTests(unittest.TestCase):
    def test_ai_api_search_and_docs(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}, clear=False
        ):
            from paperflow.web.app import create_app
            with PaperDatabase(Path(directory) / "paperflow.db") as database:
                database.save_papers([Paper(title="AI searchable paper", doi="10.1/ai", abstract="climate")])
            client = create_app({"TESTING": True, "WTF_CSRF_DISABLED": True, "SECRET_KEY": "test"}).test_client()
            self.assertEqual(client.get("/ai").status_code, 200)
            self.assertEqual(client.get("/api/v1").status_code, 200)
            result = client.get("/api/v1/papers?q=climate").get_json()
            self.assertEqual(result["results"][0]["title"], "AI searchable paper")
    def test_job_api_hides_internal_channels_paths_and_parameters(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}, clear=False
        ):
            from paperflow.web.app import create_app

            store = JobStore()
            job_id = store.enqueue("download_db", {"mode": "oa+scihub", "path": "/secret"})
            store.touch(
                job_id,
                "[3/10] ✓ A useful paper | scihub: scihub → https://internal.example/file.pdf",
            )
            app = create_app({"TESTING": True, "WTF_CSRF_DISABLED": True, "SECRET_KEY": "test"})
            payload = app.test_client().get(f"/api/jobs/{job_id}").get_json()
            self.assertEqual(payload["message"], "[3/10] 已获取 · A useful paper")
            self.assertNotIn("params", payload)
            self.assertNotIn("pid", payload)
            self.assertNotIn("log_path", payload)
            from paperflow.web.app import _public_message
            self.assertNotIn(
                "/mnt/", _public_message(
                    "完成: 检索到 21 篇。输出: /mnt/openlist/Hermes/exports/search.txt；数据库: /mnt/openlist/Hermes/paperflow.db"
                )
            )

    def test_dashboard_library_and_persistent_job(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}, clear=False
        ):
            from paperflow.web.app import create_app

            with PaperDatabase(Path(directory) / "paperflow.db") as database:
                database.save_papers([
                    Paper(title="A searchable Ginkgo paper", doi="10.1/web", sources={"WOS"})
                ])
            app = create_app({"TESTING": True, "WTF_CSRF_DISABLED": True, "SECRET_KEY": "test"})
            client = app.test_client()
            self.assertEqual(client.get("/").status_code, 200)
            response = client.get("/library?q=Ginkgo")
            self.assertIn(b"A searchable Ginkgo paper", response.data)
            response = client.post(
                "/jobs/new/search",
                data={"keywords": "Ginkgo biloba", "sources": "Crossref", "limit": "2"},
            )
            self.assertEqual(response.status_code, 302)
            self.assertTrue((Path(directory) / "paperflow-jobs.db").is_file())

    @patch("paperflow.web.app.JobStore")
    @patch("paperflow.web.app.PaperDatabase")
    def test_dashboard_does_not_open_remote_databases_for_initial_render(self, database, jobs):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}, clear=False
        ):
            from paperflow.web.app import create_app

            response = create_app({
                "TESTING": True, "WTF_CSRF_DISABLED": True, "SECRET_KEY": "test"
            }).test_client().get("/")
            self.assertEqual(response.status_code, 200)
            database.assert_not_called()
            jobs.assert_not_called()
            self.assertIn(b"recent-tasks", response.data)

    def test_basic_auth_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "PAPERFLOW_DATA_ROOT": directory,
                "PAPERFLOW_WEB_USERNAME": "paperflow",
                "PAPERFLOW_WEB_PASSWORD": "secret",
            },
            clear=False,
        ):
            from paperflow.web.app import create_app

            app = create_app({"TESTING": False, "WTF_CSRF_DISABLED": True, "SECRET_KEY": "test"})
            client = app.test_client()
            self.assertEqual(client.get("/").status_code, 401)
            token = base64.b64encode(b"paperflow:secret").decode()
            self.assertEqual(client.get("/", headers={"Authorization": f"Basic {token}"}).status_code, 200)
            self.assertEqual(client.get("/healthz").status_code, 200)

    def test_download_page_imports_dois_and_enqueues_unlimited_oa_scihub(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}, clear=False
        ):
            from paperflow.web.app import create_app

            app = create_app({"TESTING": True, "WTF_CSRF_DISABLED": True, "SECRET_KEY": "test"})
            client = app.test_client()
            page = client.get("/downloads")
            self.assertEqual(page.status_code, 302)
            self.assertEqual(page.headers["Location"], "/")
            response = client.post(
                "/downloads/import",
                data={
                    "dois": "DOI: 10.1000/ABC",
                    "rpm": "45",
                    "file": (io.BytesIO(b"https://doi.org/10.1000/abc\n10.2000/DEF\tA title\n"), "dois.txt"),
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 302)
            imports = list((Path(directory) / "imports").glob("doi-import-*.txt"))
            self.assertEqual(len(imports), 1)
            self.assertEqual(imports[0].read_text().splitlines(), ["10.1000/abc", "10.2000/def\tA title"])
            job = JobStore().list(1)[0]
            self.assertEqual(job["kind"], "download_list")
            self.assertEqual(job["params"]["mode"], "oa+scihub")
            self.assertEqual(job["params"]["limit"], 0)
            self.assertEqual(job["params"]["rpm"], "45")

    @patch("paperflow.web.app.PaperDatabase")
    def test_downloads_initial_render_does_not_open_paper_database(self, database):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}, clear=False
        ):
            from paperflow.web.app import create_app

            response = create_app({
                "TESTING": True, "WTF_CSRF_DISABLED": True, "SECRET_KEY": "test"
            }).test_client().get("/downloads")
            self.assertEqual(response.status_code, 302)
            database.assert_not_called()

    def test_quiet_homepage_hides_download_implementation_and_links_core_pages(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}, clear=False
        ):
            from paperflow.web.app import create_app

            app = create_app({"TESTING": True, "WTF_CSRF_DISABLED": True, "SECRET_KEY": "test"})
            page = app.test_client().get("/").get_data(as_text=True)
            self.assertNotIn("Sci-Hub", page)
            self.assertNotIn('href="/downloads"', page)
            self.assertIn('href="/jobs"', page)
            script_response = app.test_client().get("/static/app.js")
            script = script_response.get_data(as_text=True)
            script_response.close()
            self.assertIn("任务 #${created.id} 已创建", script)
            self.assertIn("finally { button.disabled = false; }", script)

    def test_public_keyword_api_uses_hidden_server_source_limit(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}, clear=False
        ):
            from paperflow.web.app import create_app
            client = create_app({"TESTING": True, "WTF_CSRF_DISABLED": True, "SECRET_KEY": "test"}).test_client()
            response = client.post("/api/jobs", json={"workflow": "metadata", "mode": "keyword", "items": ["p450"], "limit": 25})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(JobStore().list(1)[0]["params"]["limit"], 0)
            page = client.get("/").get_data(as_text=True)
            self.assertNotIn('type="number"', page)

    def test_file_browser_is_confined_to_paperflow_output_directories(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}, clear=False
        ):
            from paperflow.web.app import create_app

            root = Path(directory)
            (root / "private-project").mkdir()
            (root / "private-project" / "secret.txt").write_text("secret")
            (root / "exports").mkdir(exist_ok=True)
            (root / "exports" / "report.txt").write_text("public report")
            app = create_app({"TESTING": True, "WTF_CSRF_DISABLED": True, "SECRET_KEY": "test"})
            client = app.test_client()

            listing = client.get("/files").get_data(as_text=True)
            self.assertIn("exports", listing)
            self.assertNotIn("private-project", listing)
            self.assertEqual(client.get("/files?path=private-project").status_code, 404)
            self.assertEqual(
                client.get("/files/download/private-project/secret.txt").status_code, 404
            )
            download = client.get("/files/download/exports/report.txt")
            self.assertEqual(download.status_code, 200)
            download.close()
            self.assertEqual(
                client.post(
                    "/files/upload",
                    data={"path": "", "file": (io.BytesIO(b"no"), "root.txt")},
                    content_type="multipart/form-data",
                ).status_code,
                404,
            )

    def test_secure_response_headers_and_cookie_flags(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}, clear=False
        ):
            from paperflow.web.app import create_app

            response = create_app({"TESTING": True, "SECRET_KEY": "test"}).test_client().get("/")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertIn("max-age=31536000", response.headers["Strict-Transport-Security"])
            self.assertIn("camera=()", response.headers["Permissions-Policy"])
            cookie = response.headers.get("Set-Cookie", "")
            self.assertIn("Secure", cookie)
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=Lax", cookie)

    def test_search_form_defaults_to_wos_and_pubmed_and_server_enforces_download_mode(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}, clear=False
        ):
            from paperflow.web.app import create_app

            app = create_app({"TESTING": True, "WTF_CSRF_DISABLED": True, "SECRET_KEY": "test"})
            client = app.test_client()
            jobs_page = client.get("/jobs").get_data(as_text=True)
            self.assertIn('value="WOS" checked', jobs_page)
            self.assertIn('value="PubMed" checked', jobs_page)
            client.post("/jobs/new/search", data={"keywords": "Ginkgo biloba"})
            search_job = JobStore().list(1)[0]
            self.assertEqual(search_job["params"]["sources"], ["WOS", "PubMed", "Europe PMC"])
            client.post("/jobs/new/download_db", data={"mode": "publisher", "limit": ""})
            download_job = JobStore().list(1)[0]
            self.assertEqual(download_job["params"]["mode"], "oa+scihub")

    def test_failed_doi_publisher_links_page(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}, clear=False
        ):
            from paperflow.web.app import create_app

            root = Path(directory)
            (root / "imports").mkdir(parents=True)
            (root / "imports" / "dois.txt").write_text(
                "10.1000/missing\n10.1000/downloaded\n", encoding="utf-8"
            )
            pdf = root / "downloads" / "downloaded.pdf"
            pdf.parent.mkdir()
            pdf.write_bytes(b"%PDF-1.4\ntest")
            with PaperDatabase(root / "paperflow.db") as database:
                paper = Paper(title="Downloaded", doi="10.1000/downloaded", sources={"manual"})
                paper.downloaded_path = str(pdf)
                database.save_download(paper, True, "ok")
            store = JobStore()
            job_id = store.enqueue("download_list", {"path": "imports/dois.txt", "limit": 0})
            app = create_app({"TESTING": True, "WTF_CSRF_DISABLED": True, "SECRET_KEY": "test"})
            response = app.test_client().get(f"/downloads/{job_id}/publisher-links")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"10.1000/missing", response.data)
            self.assertNotIn(b"10.1000/downloaded", response.data)
            self.assertIn(b"https://doi.org/10.1000/missing", response.data)


if __name__ == "__main__":
    unittest.main()
