import unittest
import os
from unittest.mock import Mock, patch

from paperflow.pdf.oa import OaEngine


class OpenAlexTests(unittest.TestCase):
    @patch("paperflow.pdf.oa.net.make_session")
    def test_bulk_openalex_reconstructs_abstract_and_candidates(self, make_session):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"results": [{
            "doi": "https://doi.org/10.1234/Test",
            "abstract_inverted_index": {"Hello": [0], "world": [1]},
            "best_oa_location": {"pdf_url": "https://example.org/paper.pdf", "is_oa": True},
            "locations": [],
        }]}
        make_session.return_value.get.return_value = response
        result = OaEngine(proxies=[None]).bulk_openalex(["10.1234/Test"])
        self.assertEqual(result["10.1234/test"]["abstract"], "Hello world")
        self.assertEqual(result["10.1234/test"]["candidates"], ["https://example.org/paper.pdf"])

    @patch.dict(os.environ, {"S2_API_KEY": "test-key"}, clear=False)
    @patch("paperflow.pdf.oa.net.make_session")
    def test_bulk_s2_extracts_pdf_pmc_and_abstract(self, make_session):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = [{
            "abstract": "An abstract",
            "openAccessPdf": {"url": "https://example.org/paper.pdf"},
            "externalIds": {"DOI": "10.1234/Test", "PubMedCentral": "PMC123"},
        }]
        make_session.return_value.post.return_value = response
        result = OaEngine(proxies=[None]).bulk_s2(["10.1234/Test"])
        self.assertEqual(result["10.1234/test"]["abstract"], "An abstract")
        self.assertEqual(len(result["10.1234/test"]["candidates"]), 2)
