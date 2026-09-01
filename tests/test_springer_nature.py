import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from paperflow.pdf.springer_nature import SpringerNatureEngine


class SpringerNatureTests(unittest.TestCase):
    @patch.dict(os.environ, {"SPRINGER_NATURE_API_KEY": "test-key"}, clear=True)
    @patch("paperflow.pdf.springer_nature.net.make_session")
    def test_open_access_pdf_is_saved(self, make_session):
        api = Mock(status_code=200)
        api.raise_for_status.return_value = None
        api.json.return_value = {"records": [{"url": [{"format": "pdf", "value": "https://link.springer.com/x.pdf"}]}]}
        pdf = Mock(status_code=200, content=b"%PDF-1.7 test")
        make_session.return_value.get.side_effect = [api, pdf]
        with tempfile.TemporaryDirectory() as tmp:
            ok, detail = SpringerNatureEngine(proxies=[None]).fetch("10.1007/s00122-024-04567-8", Path(tmp) / "x.pdf")
            self.assertTrue(ok)
            self.assertIn("Springer Nature API", detail)

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_key(self):
        ok, detail = SpringerNatureEngine(proxies=[None]).fetch("10.1007/test", Path("unused.pdf"))
        self.assertFalse(ok)
        self.assertIn("SPRINGER_NATURE_API_KEY", detail)

