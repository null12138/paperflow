import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from paperflow.pdf.io import save_streamed_pdf


class StreamedPdfTests(unittest.TestCase):
    def test_valid_pdf_is_atomically_saved(self):
        response = Mock()
        response.iter_content.return_value = [b"%P", b"DF-1.7\n", b"content"]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "paper.pdf"
            self.assertTrue(save_streamed_pdf(response, target))
            self.assertEqual(target.read_bytes(), b"%PDF-1.7\ncontent")
            self.assertFalse(target.with_suffix(".pdf.part").exists())

    def test_html_and_oversized_responses_leave_no_partial_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PAPERFLOW_MAX_PDF_MB": "10"}
        ):
            target = Path(directory) / "paper.pdf"
            html = Mock()
            html.iter_content.return_value = [b"<html>not a pdf</html>"]
            self.assertFalse(save_streamed_pdf(html, target))
            huge = Mock()
            huge.iter_content.return_value = [b"%PDF-", b"x" * (10 * 1024 * 1024 + 1)]
            self.assertFalse(save_streamed_pdf(huge, target))
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".pdf.part").exists())


if __name__ == "__main__":
    unittest.main()
