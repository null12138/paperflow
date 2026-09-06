import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from paperflow.database import PaperDatabase
from paperflow.models import Paper
from paperflow.web.jobs import (
    JobStore,
    build_command,
    create_download_archive,
    path_in_data_root,
    web_download_mode,
)


class JobStoreTests(unittest.TestCase):
    def test_jobs_persist_and_running_jobs_recover(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}
        ):
            store = JobStore()
            job_id = store.enqueue("doctor", {})
            claimed = store.claim_next()
            self.assertEqual(claimed["id"], job_id)
            self.assertEqual(claimed["status"], "running")

            restarted = JobStore()
            self.assertEqual(restarted.recover_interrupted(), 1)
            recovered = restarted.get(job_id)
            self.assertEqual(recovered["status"], "queued")
            self.assertEqual(recovered["resume_count"], 1)

    def test_download_resume_keeps_pending_status_without_redownloading_results(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}
        ):
            job = {
                "id": 7,
                "kind": "download_db",
                "resume_count": 1,
                "params": {"status": "pending", "mode": "oa", "limit": 0},
            }
            command = build_command(job)
            self.assertEqual(command[command.index("--status") + 1], "pending")
            self.assertEqual(command[command.index("--limit") + 1], "0")
            self.assertIn(str(Path(directory).resolve() / "paperflow.db"), command)

    def test_retry_download_job_gets_a_persistent_attempt_cutoff_when_claimed(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}
        ):
            store = JobStore()
            job_id = store.enqueue("download_db", {"status": "failed", "mode": "oa+scihub"})
            claimed = store.claim_next()
            self.assertEqual(claimed["id"], job_id)
            self.assertTrue(claimed["params"]["attempt_before"])
            command = build_command(claimed)
            self.assertEqual(
                command[command.index("--attempt-before") + 1],
                claimed["params"]["attempt_before"],
            )

    def test_paths_cannot_escape_data_root(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}
        ):
            with self.assertRaises(ValueError):
                path_in_data_root("../outside")

    def test_web_download_mode_rejects_browser_channels(self):
        self.assertEqual(web_download_mode("direct+oa+scihub"), "direct+oa+scihub")
        with self.assertRaises(ValueError):
            web_download_mode("oa+publisher")

    def test_web_search_defaults_to_wos_and_pubmed_without_limit(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}
        ):
            command = build_command({
                "id": 8,
                "kind": "search",
                "resume_count": 0,
                "params": {"keywords": ["Ginkgo biloba"]},
            })
            self.assertEqual(command[command.index("--sources") + 1], "WOS,PubMed,Europe PMC")
            self.assertEqual(command[command.index("--limit") + 1], "0")

    def test_web_full_run_always_tries_oa_before_scihub(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}
        ):
            root = Path(directory)
            root.joinpath("input.txt").write_text("Ginkgo biloba\n", encoding="utf-8")
            command = build_command({
                "id": 10, "kind": "full_run", "resume_count": 0,
                "params": {"path": "input.txt", "mode": "scihub"},
            })
            self.assertEqual(command[command.index("--mode") + 1], "oa+scihub")

    def test_resumed_full_run_uses_durable_download_queue(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}
        ):
            root = Path(directory)
            root.joinpath("input.txt").write_text("Ginkgo biloba\n", encoding="utf-8")
            command = build_command({
                "id": 11, "kind": "full_run", "resume_count": 1,
                "params": {"path": "input.txt", "mode": "scihub"},
            })
            self.assertEqual(command[3], "download-db")
            self.assertEqual(command[command.index("--mode") + 1], "oa+scihub")
            self.assertEqual(command[command.index("--status") + 1], "pending")

    def test_web_wos_fetch_defaults_to_all_records(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}
        ):
            command = build_command({
                "id": 9,
                "kind": "wos_fetch",
                "resume_count": 0,
                "params": {"keywords": ["Ginkgo biloba"]},
            })
            self.assertEqual(command[command.index("--max-records") + 1], "0")

    def test_doi_download_archive_contains_only_successful_task_pdfs(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}
        ):
            root = Path(directory)
            source = root / "imports" / "dois.txt"
            source.parent.mkdir(parents=True)
            source.write_text("10.1000/ok\n10.1000/missing\n", encoding="utf-8")
            pdf = root / "downloads" / "paper.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-1.4\ntest")
            with PaperDatabase(root / "paperflow.db") as database:
                paper = Paper(title="Downloaded paper", doi="10.1000/ok", sources={"manual"})
                paper.downloaded_path = str(pdf)
                paper.download_source = "oa"
                database.save_download(paper, True, "ok")
            archive, included = create_download_archive({
                "id": 12,
                "kind": "download_list",
                "params": {"path": "imports/dois.txt", "limit": 0},
            })
            self.assertEqual(included, 1)
            with zipfile.ZipFile(archive) as bundle:
                names = bundle.namelist()
                self.assertTrue(all(n.startswith("task-12/") for n in names))
                summary = bundle.read("task-12/01_DOI清单/summary.txt").decode()
                self.assertIn("10.1000/ok", summary.split("failedDownload:")[0])
                self.assertIn("10.1000/missing", summary.split("failedDownload:")[1])
                self.assertEqual(len([n for n in names if n.endswith(".pdf")]), 1)

    def test_database_download_archive_is_scoped_to_task_attempts(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_DATA_ROOT": directory}
        ):
            root = Path(directory)
            pdf = root / "downloads" / "task-paper.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-1.4\ntask")
            with PaperDatabase(root / "paperflow.db") as database:
                paper = Paper(title="Task paper", doi="10.1000/task", sources={"WOS"})
                paper.downloaded_path = str(pdf)
                paper.download_source = "oa"
                database.save_download(paper, True, "task success")
            archive, included = create_download_archive({
                "id": 13,
                "kind": "download_db",
                "started_at": "2000-01-01 00:00:00",
                "params": {},
            })
            self.assertEqual(included, 1)
            with zipfile.ZipFile(archive) as bundle:
                self.assertTrue(any(n.startswith("task-13/01_未分类/pdf_downloaded/") and n.endswith(".pdf") for n in bundle.namelist()))


if __name__ == "__main__":
    unittest.main()
