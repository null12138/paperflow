import unittest
import os
from unittest.mock import Mock, patch
import requests

from paperflow.pdf.oa import OaEngine
from paperflow.pdf.wos_browser import publisher_adapter, WosBrowserEngine


class OpenAlexTests(unittest.TestCase):
    def test_pmc_landing_url_is_normalized_to_public_pdf_endpoint(self):
        self.assertEqual(
            OaEngine._normalize_candidate("https://www.ncbi.nlm.nih.gov/pmc/articles/1817752"),
            "https://europepmc.org/articles/PMC1817752?pdf=render",
        )

    def test_direct_pmc_pdf_is_not_replaced_with_landing_endpoint(self):
        url = "https://pmc.ncbi.nlm.nih.gov/articles/PMC123/pdf/paper.pdf"
        self.assertEqual(OaEngine._normalize_candidate(url), url)

    @patch("paperflow.pdf.oa.net.make_session")
    def test_mdpi_static_pdf_candidates_are_derived_from_crossref(self, make_session):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"message": {
            "container-title": ["Forests"],
            "link": [{"URL": "https://www.mdpi.com/1999-4907/12/8/1079/pdf"}],
        }}
        make_session.return_value.get.return_value = response
        engine = OaEngine(proxies=[None])

        result = engine._mdpi_asset_candidates("10.3390/f12081079", [])

        self.assertEqual(
            result[0],
            "https://mdpi-res.com/d_attachment/forests/forests-12-01079/"
            "article_deploy/forests-12-01079.pdf",
        )

    @patch("paperflow.pdf.oa.net.make_session")
    def test_unpaywall_failure_keeps_existing_openalex_candidates(self, make_session):
        make_session.return_value.get.side_effect = requests.ConnectionError("offline")
        existing = ["https://repository.example/paper.pdf"]
        self.assertEqual(
            OaEngine(email="test@example.org", proxies=[None])._unpaywall_candidates(
                "10.1234/test", existing
            ),
            existing,
        )

    @patch("paperflow.pdf.oa.net.make_session")
    def test_crossref_title_match_requires_high_confidence(self, make_session):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"message": {"items": [{
            "DOI": "10.1234/FOUND",
            "title": ["A precise and complete research article title"],
            "published": {"date-parts": [[2024]]},
            "link": [{"URL": "https://example.org/paper.pdf", "content-type": "application/pdf"}],
        }]}}
        make_session.return_value.get.return_value = response
        engine = OaEngine(proxies=[None])

        result = engine.crossref_title_match(
            "A precise and complete research article title", "2024"
        )

        self.assertEqual(result["doi"], "10.1234/found")
        self.assertEqual(result["candidates"], ["https://example.org/paper.pdf"])
        self.assertEqual(
            engine.crossref_title_match("A clearly unrelated article title", "2024"), {}
        )

    @patch.dict(os.environ, {"S2_API_KEY": "test-key", "UNPAYWALL_EMAIL": "test@example.org"}, clear=False)
    @patch("paperflow.pdf.oa.net.make_session")
    def test_single_doi_falls_back_from_openalex_to_s2_then_unpaywall(self, make_session):
        make_session.return_value.get.side_effect = requests.ConnectionError("openalex down")
        engine = OaEngine(proxies=[None])
        with patch.object(engine, "bulk_s2", return_value={
            "10.1234/test": {"candidates": ["https://example.org/s2.pdf"]}
        }), patch.object(
            engine, "_unpaywall_candidates",
            side_effect=lambda _doi, initial: [*initial, "https://example.org/unpaywall.pdf"],
        ):
            self.assertEqual(engine._candidates_for_doi("10.1234/test"), [
                "https://example.org/s2.pdf", "https://example.org/unpaywall.pdf"
            ])

    @patch.dict(os.environ, {"S2_API_KEY": "test-key", "UNPAYWALL_EMAIL": "test@example.org"}, clear=False)
    @patch("paperflow.pdf.oa.net.make_session")
    def test_openalex_429_opens_circuit_for_following_dois(self, make_session):
        response = Mock(status_code=429, headers={})
        response.json.return_value = {"retryAfter": 3600}
        make_session.return_value.get.return_value = response
        engine = OaEngine(proxies=[None])
        with patch.object(engine, "bulk_s2", return_value={}), patch.object(
            engine, "_unpaywall_candidates", side_effect=lambda _doi, initial: initial
        ):
            engine._candidates_for_doi("10.1234/one")
            engine._candidates_for_doi("10.1234/two")
        self.assertEqual(make_session.return_value.get.call_count, 1)

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
