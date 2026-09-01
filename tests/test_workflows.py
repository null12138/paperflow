import tempfile
import unittest
from pathlib import Path

from paperflow.database import PaperDatabase
from paperflow.models import Paper
from paperflow.workflows import reconcile_existing_pdfs


class ReconcilePdfTests(unittest.TestCase):
    def test_existing_pdf_is_matched_copied_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "old"
            source_dir.mkdir()
            title = "A sufficiently long example paper title for matching"
            source = source_dir / "A_sufficiently_long_example_paper_title_for_matching.pdf"
            source.write_bytes(b"%PDF-1.7 test")
            db = root / "papers.db"
            with PaperDatabase(db) as database:
                database.save_papers([Paper(title=title, doi="10.1234/test")])
            result = reconcile_existing_pdfs(db, [source_dir], root / "pdf_downloaded")
            self.assertEqual(result["matched"], 1)
            with PaperDatabase(db) as database:
                papers = database.load_papers_for_download(status="downloaded", limit=0)
                self.assertEqual(len(papers), 1)
                self.assertTrue(Path(papers[0].downloaded_path).is_file())

