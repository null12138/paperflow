import unittest
from unittest.mock import Mock
import requests

from paperflow.sources.pubmed_crossref_s2 import EuropePmcSource


class EuropePmcTests(unittest.TestCase):
    def test_transient_page_timeout_is_retried_without_losing_cursor(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "nextCursorMark": "done",
            "resultList": {"result": [{"title": "Recovered"}]},
        }
        empty = Mock(status_code=200)
        empty.json.return_value = {"nextCursorMark": "done", "resultList": {"result": []}}
        client = Mock()
        client.get.side_effect = [requests.exceptions.ReadTimeout("temporary"), response, empty]
        with unittest.mock.patch("paperflow.sources.pubmed_crossref_s2.time.sleep"):
            papers = EuropePmcSource().search_species(client, "Ginkgo", 0)
        self.assertEqual([paper.title for paper in papers], ["Recovered"])
        self.assertEqual(client.get.call_count, 3)
        self.assertEqual(client.get.call_args_list[1].kwargs["params"]["cursorMark"], "*")

    def test_zero_limit_follows_cursor_pages(self):
        first = Mock()
        first.json.return_value = {
            "nextCursorMark": "next",
            "resultList": {"result": [{
                "title": "First", "doi": "10.1/first", "pmcid": "PMC1", "isOpenAccess": "Y"
            }]},
        }
        second = Mock()
        second.json.return_value = {
            "nextCursorMark": "next",
            "resultList": {"result": [{
                "title": "Second", "doi": "10.1/second", "pmcid": "PMC2", "isOpenAccess": "Y"
            }]},
        }
        client = Mock()
        client.get.side_effect = [first, second]
        papers = EuropePmcSource().search_species(client, "Panthera tigris", 0)
        self.assertEqual([paper.title for paper in papers], ["First", "Second"])
        self.assertEqual(client.get.call_count, 2)
        query = client.get.call_args_list[0].kwargs["params"]["query"]
        self.assertIn('BODY:"Panthera tigris"', query)
        self.assertIn('BACK:"Panthera tigris"', query)
        self.assertNotIn('OPEN_ACCESS:Y', query)
        self.assertIn("?pdf=render", papers[0].pdf_candidates[0].url)
