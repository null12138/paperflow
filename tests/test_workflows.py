import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paperflow.database import PaperDatabase
from paperflow.models import Paper
from paperflow.workflows import preflight_download_candidates, reconcile_existing_pdfs, search_to_database


class SearchLimitTests(unittest.TestCase):
    def test_limit_caps_combined_deduplicated_results(self):
        class Source:
            def __init__(self, name, prefix):
                self.name = name
                self.prefix = prefix

            def search_species(self, _client, species, _limit):
                return [Paper(title=f"{self.prefix} {index}", sources={self.name}, species={species}) for index in range(4)]

        with tempfile.TemporaryDirectory() as directory, patch(
            "paperflow.workflows.SOURCES.ordered",
            return_value=[Source("WOS", "W"), Source("PubMed", "P")],
        ):
            papers = search_to_database(
                ["p450"], ["WOS", "PubMed"], 3, "", Path(directory) / "papers.db"
            )
        self.assertEqual(len(papers), 3)


class ReconcilePdfTests(unittest.TestCase):
    @patch("paperflow.pdf.oa.OaEngine.bulk_openalex")
    @patch("paperflow.pdf.oa.OaEngine.bulk_pmc_idconv", return_value={})
    @patch("paperflow.pdf.oa.OaEngine.crossref_title_match")
    def test_preflight_recovers_missing_doi_before_oa_batches(
        self, title_match, _pmc, openalex
    ):
        title_match.return_value = {"doi": "10.1234/recovered", "candidates": []}
        openalex.return_value = {
            "10.1234/recovered": {
                "abstract": "A recovered abstract",
                "candidates": ["https://example.org/recovered.pdf"],
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "papers.db"
            with PaperDatabase(db) as database:
                database.save_papers([Paper(
                    title="A sufficiently distinctive article title for DOI recovery",
                    year="2024",
                )])

            result = preflight_download_candidates(db, limit=0)

            self.assertEqual(result["recovered_dois"], 1)
            with PaperDatabase(db) as database:
                row = database.list_papers()[0]
                self.assertEqual(row["doi"], "10.1234/recovered")
                self.assertEqual(database.stats()["pdf_candidates"], 1)

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
