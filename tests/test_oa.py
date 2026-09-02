import unittest
import os
from unittest.mock import Mock, patch

from paperflow.pdf.oa import OaEngine
from paperflow.pdf.wos_browser import publisher_adapter, WosBrowserEngine


class OpenAlexTests(unittest.TestCase):
    def test_wos_publisher_adapter_recognizes_common_hosts(self):
        self.assertEqual(publisher_adapter("link.springer.com"), "springer")
        self.assertEqual(publisher_adapter("www.sciencedirect.com"), "elsevier")
        self.assertEqual(publisher_adapter("onlinelibrary.wiley.com"), "wiley")
        self.assertEqual(publisher_adapter("unknown.example"), "generic")

    @patch("paperflow.pdf.wos_browser.requests.get")
    def test_wos_uid_resolves_by_official_doi_query(self, get):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {"hits": [{"uid": "WOS:123"}]}
        get.return_value = response
        with patch.dict("os.environ", {"WOS_API_KEY": "test-key"}, clear=False):
            self.assertEqual(WosBrowserEngine._resolve_uid("10.1234/Test"), "WOS:123")
        self.assertIn("DO=(10.1234/Test)", get.call_args.kwargs["params"]["q"])

    @patch("paperflow.pdf.oa.net.make_session")
    def test_bulk_pmc_idconv_returns_only_valid_pmc_records(self, make_session):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {"records": [
            {"doi": "10.1234/FOUND", "pmcid": "PMC123"},
            {"requested-id": "10.1234/missing", "status": "error"},
        ]}
        make_session.return_value.get.return_value = response

        result = OaEngine(proxies=[None]).bulk_pmc_idconv([
            "10.1234/found", "10.1234/missing"
        ])

        self.assertEqual(result, {"10.1234/found": "PMC123"})
        params = make_session.return_value.get.call_args.kwargs["params"]
        self.assertEqual(params["tool"], "paperflow")

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
