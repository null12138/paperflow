import unittest

from paperflow.models import Paper, add_paper, clean_text, normalize_doi, unique_papers


class ModelTests(unittest.TestCase):
    def test_normalize_doi(self):
        self.assertEqual(normalize_doi("https://doi.org/10.1/ABC"), "10.1/abc")

    def test_clean_text_removes_invisible_format_characters(self):
        self.assertEqual(clean_text("Eff\u2060ect\u200bs"), "Effects")

    def test_merge_same_doi_and_keep_longer_abstract(self):
        collection = {}
        add_paper(
            collection,
            Paper(title="A study", doi="https://doi.org/10.1/ABC", abstract="short", sources={"WOS"}),
        )
        add_paper(
            collection,
            Paper(title="A study.", doi="10.1/abc", abstract="a much longer abstract", sources={"PubMed"}),
        )

        papers = unique_papers(collection)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].abstract, "a much longer abstract")
        self.assertEqual(papers[0].sources, {"WOS", "PubMed"})


if __name__ == "__main__":
    unittest.main()
