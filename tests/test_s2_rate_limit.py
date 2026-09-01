import os
import unittest
from unittest.mock import Mock, patch

from paperflow.sources.pubmed_crossref_s2 import SemanticScholarSource


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class RateLimitedResponse(FakeResponse):
    status_code = 429

    def raise_for_status(self):
        raise AssertionError("429 should be classified before raise_for_status")


class S2RateLimitTests(unittest.TestCase):
    def test_429_message_is_actionable_without_url(self):
        source = SemanticScholarSource()
        source._limiter = Mock()
        client = Mock()
        client.get.return_value = RateLimitedResponse({"message": "rate limit"})
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "S2 匿名接口配额已限流") as caught:
                source.search_species(client, "Panthera tigris", 1)
        self.assertNotIn("https://", str(caught.exception))

    def test_all_requests_share_one_request_per_second_limiter_and_key(self):
        source = SemanticScholarSource()
        self.assertEqual(source._limiter.interval, 1.0)
        source._limiter = Mock()
        client = Mock()
        client.get.side_effect = [FakeResponse({"data": []}), FakeResponse({})]

        with patch.dict(os.environ, {"S2_API_KEY": "test-key"}):
            source.search_species(client, "Panthera tigris", 1)
            source.search_doi(client, "10.1/example")

        self.assertEqual(source._limiter.wait.call_count, 2)
        self.assertEqual(client.get.call_count, 2)
        for call in client.get.call_args_list:
            self.assertEqual(call.kwargs["headers"], {"x-api-key": "test-key"})


if __name__ == "__main__":
    unittest.main()
