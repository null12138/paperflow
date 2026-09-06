import tempfile
import unittest
from argparse import Namespace
import os
from pathlib import Path
from unittest.mock import Mock, patch

from paperflow.cli import _load_species, citation, cmd_export_wos
from paperflow.models import Paper
from paperflow.sources import SOURCES
from paperflow.sources.wos import WosApiError, WosPage, WosSource, parse_wos_api_record, parse_wos_plain_text
from paperflow.sources.pubmed_crossref_s2 import PubMedSource
from paperflow.workflows import fetch_wos_to_database
from paperflow.database import PaperDatabase


class CliTests(unittest.TestCase):
    def test_load_species_skips_comments_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.txt"
            path.write_text(
                "# comment\nPanthera tigris\n\nPanthera tigris\nGinkgo biloba\n",
                encoding="utf-8",
            )
            self.assertEqual(_load_species(path), ["Panthera tigris", "Ginkgo biloba"])

    def test_citation_does_not_duplicate_title_period(self):
        result = citation(Paper(title="A title.", authors=["A. Author"], year="2025"))
        self.assertIn("A title.", result)
        self.assertNotIn("A title..", result)

    @patch("subprocess.call", return_value=0)
    def test_export_wos_uses_packaged_legacy_script(self, call):
        result = cmd_export_wos(Namespace(input=None, max_records=20))
        command = call.call_args.args[0]
        self.assertEqual(result, 0)
        self.assertTrue(Path(command[1]).is_file())
        self.assertEqual(Path(command[1]).parent.name, "legacy")


class WosParserTests(unittest.TestCase):
    def test_parse_plain_text_record(self):
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

    def test_parse_starter_record_fields(self):
        paper = parse_wos_api_record({
            "title": "A <i>Ginkgo</i> study",
            "identifiers": {"doi": "10.1234/TEST"},
            "names": {"authors": [{"displayName": "Smith, A."}]},
            "source": {"sourceTitle": "TEST JOURNAL", "publishYear": 2025},
        }, "Ginkgo biloba")
        self.assertIsNotNone(paper)
        self.assertEqual(paper.title, "A Ginkgo study")
        self.assertEqual(paper.authors, ["Smith, A."])
        self.assertEqual(paper.doi, "10.1234/test")
        self.assertEqual(paper.year, "2025")


class _FakeResponse:
    def __init__(self, status, payload, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


class _PagedClient:
    def __init__(self, total=120):
        self.total = total
        self.pages = []

    def get(self, url, **kwargs):
        page = kwargs["params"]["page"]
        self.pages.append(page)
        start = (page - 1) * 50
        hits = [
            {"title": f"Paper {index}", "source": {"publishYear": 2024}}
            for index in range(start, min(start + 50, self.total))
        ]
        return _FakeResponse(200, {"metadata": {"total": self.total}, "hits": hits})


class WosApiTests(unittest.TestCase):
    def test_unlimited_wos_search_is_not_wrapped_in_whole_source_timeout(self):
        from paperflow.workflows import _search_one_source
        source = Mock(name="source")
        source.name = "WOS"
        source.search_species.return_value = ["paper"]
        with patch("paperflow.workflows.signal.setitimer") as timer:
            self.assertEqual(_search_one_source(source, Mock(), "Ginkgo", 0), ["paper"])
        timer.assert_not_called()

    def test_transient_500_is_retried(self):
        source = WosSource()
        client = Mock()
        client.get.side_effect = [
            _FakeResponse(500, {"error": "temporary"}),
            _FakeResponse(200, {"metadata": {"total": 1}, "hits": [{"title": "Recovered"}]}),
        ]
        with patch.dict(os.environ, {"WOS_API_KEY": "local-test-key"}), patch(
            "paperflow.sources.wos.time.sleep"
        ) as pause:
            pages = list(source.iter_species_pages(client, "Ginkgo", 1, request_interval=0))
        self.assertEqual(pages[0].papers[0].title, "Recovered")
        self.assertTrue(pause.called)

    def test_proxy_error_falls_back_to_direct_session(self):
        source = WosSource()
        client = Mock()
        client.get.side_effect = __import__("requests").exceptions.ProxyError("proxy failed")
        response = _FakeResponse(200, {
            "metadata": {"total": 1},
            "hits": [{"title": "Recovered by direct connection"}],
        })
        with patch.dict(os.environ, {"WOS_API_KEY": "local-test-key"}), patch(
            "paperflow.sources.wos.requests.Session"
        ) as session_type:
            session_type.return_value.get.return_value = response
            pages = list(source.iter_species_pages(client, "Ginkgo", 1, request_interval=0))
        self.assertEqual(pages[0].papers[0].title, "Recovered by direct connection")
        self.assertFalse(session_type.return_value.trust_env)

    def test_fixed_pages_support_exact_mid_page_resume(self):
        source = WosSource()
        client = _PagedClient()
        with patch.dict(os.environ, {"WOS_API_KEY": "local-test-key"}):
            first = list(source.iter_species_pages(client, "Ginkgo", 60, request_interval=0))
            resumed = list(source.iter_species_pages(
                client, "Ginkgo", 100, start_record=60, request_interval=0
            ))
        self.assertEqual([len(page.papers) for page in first], [50, 10])
        self.assertEqual([len(page.papers) for page in resumed], [40])
        self.assertEqual(client.pages, [1, 2, 2])
        self.assertEqual(resumed[0].papers[0].title, "Paper 60")
        self.assertEqual(resumed[0].record_end, 100)

    def test_rate_limit_error_never_contains_key_or_response_body(self):
        key = "secret-key-must-not-leak"
        client = type("Client", (), {
            "get": lambda *args, **kwargs: _FakeResponse(
                429, {"error": key}, {"Retry-After": "2"}
            )
        })()
        with patch.dict(os.environ, {"WOS_API_KEY": key}):
            with self.assertRaises(WosApiError) as caught:
                list(WosSource().iter_species_pages(client, "Ginkgo", 1, request_interval=0))
        self.assertNotIn(key, str(caught.exception))
        self.assertIn("429", str(caught.exception))

    def test_bad_query_retries_compatible_query_forms(self):
        source = WosSource()
        client = Mock()
        client.get.side_effect = [
            _FakeResponse(400, {"message": "invalid query syntax"}),
            _FakeResponse(200, {"metadata": {"total": 1}, "hits": [{"title": "Recovered query"}]}),
        ]
        with patch.dict(os.environ, {"WOS_API_KEY": "local-test-key"}):
            pages = list(source.iter_species_pages(client, "p450", 1, request_interval=0))
        self.assertEqual(pages[0].papers[0].title, "Recovered query")
        self.assertEqual(client.get.call_count, 2)

    def test_registry_has_only_official_wos_source(self):
        self.assertIsInstance(SOURCES.get("WOS"), WosSource)

    def test_batch_workflow_saves_page_before_manifest_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "papers.db"
            manifest = root / "manifest.json"
            source = SOURCES.get("WOS")
            page = WosPage(
                papers=[Paper(title="Checkpoint paper", sources={"WOS"}, species={"Ginkgo"})],
                page=1, total=1, record_start=0, record_end=1,
            )
            with patch.object(source, "api_key", return_value="configured"), patch.object(
                source, "iter_species_pages", return_value=iter([page])
            ):
                result = fetch_wos_to_database(
                    ["Ginkgo"], 1, db_path, manifest, request_interval=0
                )
            with PaperDatabase(db_path) as database:
                self.assertEqual(database.stats()["papers"], 1)
            self.assertEqual(result, {"saved": 1, "requests": 1})
            checkpoint = __import__("json").loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["queries"]["Ginkgo"]["fetched"], 1)


class PubMedApiTests(unittest.TestCase):
    def test_blank_limit_ignores_old_total_cap(self):
        search = Mock()
        search.status_code = 200
        search.json.return_value = {"esearchresult": {"idlist": []}}
        client = Mock()
        client.get.return_value = search
        with patch.dict(os.environ, {"PAPERFLOW_PUBMED_PAGE_LIMIT": "300"}):
            PubMedSource().search_species(client, "p450", 0)
        self.assertEqual(client.get.call_args.kwargs["params"]["retmax"], 1000)

    def test_large_result_sets_are_fetched_in_batches(self):
        search = Mock()
        search.status_code = 200
        search.json.return_value = {"esearchresult": {"idlist": [str(i) for i in range(201)]}}
        fetched = Mock()
        fetched.status_code = 200
        fetched.content = b"<PubmedArticleSet/>"
        client = Mock()
        client.get.side_effect = [search, fetched, fetched]
        PubMedSource().search_species(client, "A2A", 0)
        fetch_calls = client.get.call_args_list[1:]
        self.assertEqual([len(call.kwargs["params"]["id"].split(",")) for call in fetch_calls], [200, 1])

    def test_malformed_xml_is_retried_then_parsed(self):
        class Response:
            status_code = 200
            headers = {}
            def __init__(self, content):
                self.content = content
            def json(self):
                return {"esearchresult": {"idlist": ["1"]}}

        client = Mock()
        client.get.side_effect = [Response(b'{"esearchresult":{"idlist":["1"]}}'), Response(b"<broken>"), Response(b"<broken>"), Response(
            b"<PubmedArticleSet><PubmedArticle><MedlineCitation><Article><ArticleTitle>Recovered</ArticleTitle></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"
        )]
        with patch("paperflow.sources.pubmed_crossref_s2.time.sleep"):
            papers = PubMedSource().search_species(client, "Ginkgo", 1)
        self.assertEqual(papers[0].title, "Recovered")
        self.assertEqual(client.get.call_count, 4)


if __name__ == "__main__":
    unittest.main()
