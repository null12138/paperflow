import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from paperflow.pdf.elsevier import ElsevierEngine


class ElsevierTests(unittest.TestCase):
    @patch.dict(os.environ, {"ELSEVIER_API_KEY": "test-key"}, clear=False)
    @patch("paperflow.pdf.elsevier.net.make_session")
    def test_pdf_response_is_saved(self, make_session):
        response = Mock(status_code=200, content=b"%PDF-1.7 test", headers={})
        response.raise_for_status.return_value = None
        make_session.return_value.get.return_value = response
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "paper.pdf"
            ok, detail = ElsevierEngine(proxies=[None]).fetch("10.1016/S0001-2345(24)00001-2", target)
            self.assertTrue(ok)
            self.assertTrue(target.read_bytes().startswith(b"%PDF-"))
            self.assertIn("Elsevier API", detail)

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_key_is_actionable(self):
        ok, detail = ElsevierEngine(proxies=[None]).fetch("10.1016/test", Path("unused.pdf"))
        self.assertFalse(ok)
        self.assertIn("ELSEVIER_API_KEY", detail)

    @patch.dict(os.environ, {"ELSEVIER_API_KEY": "test-key"}, clear=False)
    @patch("paperflow.pdf.elsevier.net.make_session")
    def test_non_pdf_response_is_not_saved(self, make_session):
        response = Mock(status_code=200, content=b"{\"error\":\"denied\"}", headers={"content-type": "application/json"})
        response.raise_for_status.return_value = None
        make_session.return_value.get.return_value = response
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "paper.pdf"
            ok, detail = ElsevierEngine(proxies=[None]).fetch("10.1016/test", target)
            self.assertFalse(ok)
            self.assertFalse(target.exists())
            self.assertIn("未返回 PDF", detail)

