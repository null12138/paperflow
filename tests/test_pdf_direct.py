import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from paperflow.pdf import PdfEngine


class DirectPdfRetryTests(unittest.TestCase):
    @patch("paperflow.pdf.time.sleep")
    @patch("paperflow.pdf.net.make_session")
    def test_transient_500_is_retried(self, make_session, sleep):
        failed = Mock(status_code=500)
        error = requests.HTTPError(response=failed)
        first = Mock()
        first.raise_for_status.side_effect = error
        second = Mock(url="https://europepmc.org/paper.pdf")
        second.raise_for_status.return_value = None
        second.iter_content.return_value = [b"%PDF-1.7 retry worked"]
        make_session.return_value.get.side_effect = [first, second]

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "paper.pdf"
            engine = PdfEngine(Path(tmp), proxies=[None], use_scihub=False, use_oa=False)
            ok, detail = engine._fetch_direct_candidate("https://europepmc.org/paper.pdf", target)

        self.assertTrue(ok)
        self.assertIn("直接候选", detail)
        self.assertEqual(make_session.return_value.get.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch("paperflow.pdf.time.sleep")
    @patch("paperflow.pdf.net.make_session")
    def test_404_is_not_retried_on_same_exit(self, make_session, sleep):
        failed = Mock(status_code=404)
        response = Mock()
        response.raise_for_status.side_effect = requests.HTTPError(response=failed)
        make_session.return_value.get.return_value = response

        with tempfile.TemporaryDirectory() as tmp:
            engine = PdfEngine(Path(tmp), proxies=[None], use_scihub=False, use_oa=False)
            ok, detail = engine._fetch_direct_candidate(
                "https://europepmc.org/missing.pdf", Path(tmp) / "paper.pdf"
            )

        self.assertFalse(ok)
        self.assertIn("404", detail)
        self.assertEqual(make_session.return_value.get.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
