import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from paperflow.database import PaperDatabase
from paperflow.models import Paper
from paperflow.pdf import PdfEngine, safe_slug


class DatabaseTests(unittest.TestCase):
    def test_clear_all_removes_business_data_and_keeps_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "papers.db"
            with PaperDatabase(db_path) as database:
                database.save_papers([Paper(title="To clear", sources={"WOS"}, species={"x"})])
                counts = database.clear_all()
                self.assertEqual(counts["papers"], 1)
                self.assertEqual(database.stats()["papers"], 0)
                self.assertEqual(database.connection.execute("SELECT name FROM sqlite_master WHERE name='papers'").fetchone()[0], "papers")
    def test_upsert_relations_and_download_history(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "papers.db"
            with PaperDatabase(db_path) as database:
                database.save_papers([
                    Paper(
                        title="A tiger study",
                        abstract="short",
                        doi="https://doi.org/10.1234/ABC",
                        sources={"WOS"},
                        species={"Panthera tigris"},
                    )
                ])
                paper = Paper(
                    title="A tiger study.",
                    abstract="a much longer abstract",
                    doi="10.1234/abc",
                    sources={"PubMed"},
                    species={"tiger conservation"},
                    downloaded_path="downloads/a.pdf",
                    download_source="oa",
                )
                database.save_download(paper, True, "oa: Unpaywall")

                stats = database.stats()
                self.assertEqual(stats["papers"], 1)
                self.assertEqual(stats["keywords"], 2)
                self.assertEqual(stats["sources"], 2)
                self.assertEqual(stats["downloaded"], 1)
                self.assertEqual(stats["download_attempts"], 1)

                rows = database.list_papers("Panthera tigris")
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["abstract"], "a much longer abstract")
                self.assertEqual(rows[0]["download_source"], "oa")
                self.assertEqual(set(rows[0]["sources"].split(",")), {"WOS", "PubMed"})

                database.save_download(
                    Paper(title="10.1234/abc", doi="10.1234/abc", failure_reason="network failed"),
                    False,
                    "network failed",
                )
                row = database.list_papers("Panthera tigris")[0]
                self.assertEqual(row["title"], "A tiger study.")

    def test_chinese_title_is_a_stable_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            with PaperDatabase(Path(directory) / "papers.db") as database:
                database.save_papers([Paper(title="银杏叶研究", sources={"CNKI"})])
                database.save_papers([Paper(title="银杏叶研究", abstract="摘要", sources={"CNKI"})])
                self.assertEqual(database.stats()["papers"], 1)

    def test_pdf_candidates_survive_search_then_database_download(self):
        with tempfile.TemporaryDirectory() as directory:
            with PaperDatabase(Path(directory) / "papers.db") as database:
                paper = Paper(
                    title="无 DOI 的知网论文", journal="测试期刊",
                    sources={"CNKI"}, species={"银杏"},
                )
                paper.add_candidate(
                    "https://oversea.cnki.net/kcms2/article/abstract?id=1", "cnki", 1
                )
                database.save_papers([paper])

                queue = database.load_papers_for_download(
                    keyword="银杏", source="CNKI", status="pending"
                )
                self.assertEqual(len(queue), 1)
                self.assertEqual(queue[0].pdf_candidates[0].source, "cnki")
                self.assertEqual(queue[0].doi, "")

                queue[0].failure_reason = "network failed"
                database.save_download(queue[0], False, "network failed")
                self.assertEqual(database.load_papers_for_download(status="pending"), [])
                self.assertEqual(len(database.load_papers_for_download(status="failed")), 1)

    def test_impact_factor_import_uses_latest_year_and_filters_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metrics = root / "jif.csv"
            metrics.write_text(
                "journal,impact_factor,year\nTest Journal,3.2,2023\nTest Journal,4.5,2024\n",
                encoding="utf-8",
            )
            with PaperDatabase(root / "papers.db") as database:
                database.save_papers([
                    Paper(title="Matched", journal="TEST JOURNAL", sources={"WOS"}),
                    Paper(title="Unmatched", journal="Other Journal", sources={"WOS"}),
                ])
                result = database.import_impact_factors(metrics, source="JCR")
                self.assertEqual(result, {"imported": 2, "skipped": 0, "matched_papers": 1})
                rows = database.list_papers(min_if=4.0)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["title"], "Matched")
                self.assertEqual(rows[0]["impact_factor"], 4.5)
                self.assertEqual(rows[0]["impact_factor_year"], 2024)
                self.assertEqual(len(database.load_papers_for_download(min_if=4.0)), 1)

    def test_legacy_import_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            abstracts = root / "abstracts.txt"
            summary = root / "summary.txt"
            abstracts.write_text(
                """[1] A tiger study
Authors: Smith, J
Year: 2024
Journal: TEST JOURNAL
DOI: 10.1234/test
Species: Panthera tigris
Sources: WOS, PubMed
Abstract: A useful abstract.

[2] 银杏叶研究
Authors: N/A
Year: 2023
Journal: 中文期刊
DOI: N/A
Species: Ginkgo biloba
Sources: CNKI
Abstract: 中文摘要。
""",
                encoding="utf-8",
            )
            summary.write_text(
                "- Smith, J (2024). A tiger study. TEST JOURNAL. doi:10.1234/test. "
                "[PDF: pdf_downloaded/a.pdf]\n"
                "- 作者不详 (2023). 银杏叶研究. 中文期刊. [PDF: pdf_downloaded/b.pdf]\n",
                encoding="utf-8",
            )

            with PaperDatabase(root / "papers.db") as database:
                first = database.import_legacy(abstracts, summary)
                second = database.import_legacy(abstracts, summary)
                self.assertEqual(first, {"papers": 2, "pdf_paths": 2})
                self.assertEqual(second, {"papers": 2, "pdf_paths": 2})
                self.assertEqual(database.stats()["papers"], 2)
                self.assertEqual(database.stats()["download_attempts"], 2)

    def test_deduplicate_merges_relations_and_skips_conflicting_dois(self):
        with tempfile.TemporaryDirectory() as directory:
            with PaperDatabase(Path(directory) / "papers.db") as database:
                connection = database.connection
                connection.execute(
                    """INSERT INTO papers(identity_key, normalized_title, title, abstract)
                       VALUES ('title:samestudy', 'samestudy', 'Same study', 'short')"""
                )
                first_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
                connection.execute(
                    """INSERT INTO papers(identity_key, doi, normalized_title, title, abstract, pdf_path, download_source)
                       VALUES ('doi:10.1/same', '10.1/same', 'samestudy', 'Same study.',
                               'a longer abstract', 'downloads/same.pdf', 'oa')"""
                )
                second_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
                connection.execute("INSERT INTO keywords(keyword) VALUES ('tiger')")
                connection.execute("INSERT INTO sources(name) VALUES ('WOS')")
                connection.execute(
                    "INSERT INTO paper_keywords(paper_id, keyword_id) VALUES (?, 1)", (first_id,)
                )
                connection.execute(
                    "INSERT INTO paper_sources(paper_id, source_id) VALUES (?, 1)", (second_id,)
                )
                connection.execute(
                    """INSERT INTO download_attempts(paper_id, success, download_source, pdf_path)
                       VALUES (?, 1, 'oa', 'downloads/same.pdf')""",
                    (second_id,),
                )

                connection.execute(
                    """INSERT INTO papers(identity_key, doi, normalized_title, title)
                       VALUES ('doi:10.1/a', '10.1/a', 'conflict', 'Conflict')"""
                )
                connection.execute(
                    """INSERT INTO papers(identity_key, doi, normalized_title, title)
                       VALUES ('doi:10.1/b', '10.1/b', 'conflict', 'Conflict')"""
                )
                connection.commit()

                preview = database.deduplicate(dry_run=True)
                self.assertEqual(preview, {"groups": 1, "removed": 1, "skipped_conflicts": 1})
                self.assertEqual(database.stats()["papers"], 4)

                result = database.deduplicate()
                self.assertEqual(result, preview)
                self.assertEqual(database.stats()["papers"], 3)
                merged = database.list_papers("tiger")
                self.assertEqual(len(merged), 1)
                self.assertEqual(merged[0]["doi"], "10.1/same")
                self.assertEqual(merged[0]["pdf_path"], "downloads/same.pdf")
                self.assertEqual(database.stats()["download_attempts"], 1)


class PdfSourceTests(unittest.TestCase):
    def test_local_pdf_reuse_records_download_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = Paper(title="Existing paper", doi="10.1/existing")
            (root / f"{safe_slug(paper.title)}.pdf").write_bytes(b"%PDF-1.4\n")
            engine = PdfEngine(root, use_scihub=False, use_oa=False, use_publisher=False)
            ok, _ = engine.fetch(paper)
            self.assertTrue(ok)
            self.assertEqual(paper.download_source, "local")
            self.assertTrue(paper.downloaded_path.endswith("Existing_paper.pdf"))

    def test_persisted_direct_candidate_downloads_without_doi(self):
        response = Mock()
        response.url = "https://example.org/paper.pdf"
        response.iter_content.return_value = [b"%PDF-1.7\ncontent"]
        response.raise_for_status.return_value = None
        session = Mock()
        session.get.return_value = response
        with tempfile.TemporaryDirectory() as directory, patch(
            "paperflow.pdf.net.make_session", return_value=session
        ):
            paper = Paper(title="Direct OA candidate")
            paper.add_candidate(response.url, "europepmc", 1)
            engine = PdfEngine(
                Path(directory), use_scihub=False, use_oa=True,
                use_publisher=False, use_cnki=False,
            )
            ok, _ = engine.fetch(paper)
            self.assertTrue(ok)
            self.assertEqual(paper.download_source, "europepmc")


if __name__ == "__main__":
    unittest.main()
