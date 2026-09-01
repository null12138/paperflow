import tempfile
import unittest
from pathlib import Path

from wos_species_downloader import Paper, add_paper, citation, load_species, parse_wos_plain_text, unique_papers


class DownloaderTests(unittest.TestCase):
    def test_merge_same_doi_and_keep_longer_abstract(self):
        collection = {}
        add_paper(collection, Paper(title="A study", doi="https://doi.org/10.1/ABC", abstract="short", sources={"WOS"}))
        add_paper(collection, Paper(title="A study.", doi="10.1/abc", abstract="a much longer abstract", sources={"PubMed"}))
        papers = unique_papers(collection)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].abstract, "a much longer abstract")
        self.assertEqual(papers[0].sources, {"WOS", "PubMed"})

    def test_load_species_skips_comments_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.txt"
            path.write_text("# comment\nPanthera tigris\n\nPanthera tigris\nGinkgo biloba\n", encoding="utf-8")
            self.assertEqual(load_species(path), ["Panthera tigris", "Ginkgo biloba"])

    def test_citation_does_not_duplicate_title_period(self):
        result = citation(Paper(title="A title.", authors=["A. Author"], year="2025"))
        self.assertIn("A title.", result)
        self.assertNotIn("A title..", result)

    def test_parse_wos_plain_text(self):
        text = """FN Clarivate Analytics Web of Science
VR 1.0
PT J
AU Smith, J
   Doe, A
TI A tiger study
   with a continued title
SO TEST JOURNAL
AB Panthera tigris appears here.
PY 2024
DI 10.1234/Test
UT WOS:0001
ER

EF
"""
        papers = parse_wos_plain_text(text, "Panthera tigris")
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].title, "A tiger study with a continued title")
        self.assertEqual(papers[0].authors, ["Smith, J", "Doe, A"])
        self.assertEqual(papers[0].doi, "10.1234/test")
        self.assertEqual(papers[0].sources, {"WOS"})


if __name__ == "__main__":
    unittest.main()
