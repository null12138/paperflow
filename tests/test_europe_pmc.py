import unittest
from unittest.mock import Mock

from paperflow.sources.pubmed_crossref_s2 import EuropePmcSource


class EuropePmcTests(unittest.TestCase):
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
        self.assertIn("?pdf=render", papers[0].pdf_candidates[0].url)

