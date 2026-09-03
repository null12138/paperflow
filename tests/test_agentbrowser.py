"""AgentBrowser 通道单元测试（离线部分）：URL/DOI 提取、PDF 链接发现、空输入。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paperflow.pdf.agentbrowser import AgentBrowserEngine


class ExtractTests(unittest.TestCase):
    def test_extract_doi_from_doi_org(self):
        eng = AgentBrowserEngine()
        self.assertEqual(
            eng._extract_doi("https://doi.org/10.1021/acs.jcim.1c00799"),
            "10.1021/acs.jcim.1c00799")

    def test_extract_doi_with_query(self):
        eng = AgentBrowserEngine()
        self.assertEqual(
            eng._extract_doi("https://doi.org/10.1111/nph.18685?foo=1"),
            "10.1111/nph.18685")

    def test_extract_doi_from_direct_publisher_url(self):
        eng = AgentBrowserEngine()
        self.assertEqual(
            eng._extract_doi("https://pubs.acs.org/doi/10.1021/acs.jcim.1c00799"),
            "")

    def test_no_input(self):
        eng = AgentBrowserEngine()
        ok, info = eng.fetch("", Path("/tmp/t.pdf"))
        self.assertFalse(ok)
        self.assertIn("空输入", info)
        ok, info = eng.fetch("   ", Path("/tmp/t.pdf"))
        self.assertFalse(ok)


class PdfLinkTests(unittest.TestCase):
    def setUp(self):
        self.eng = AgentBrowserEngine()

    def test_citation_pdf_url_meta(self):
        html = ('<html><head><meta name="citation_pdf_url" '
                'content="https://pubs.acs.org/doi/pdf/10.1021/x"></head></html>')
        got = self.eng._find_pdf_link(html, "https://pubs.acs.org/doi/10.1021/x")
        self.assertEqual(got, "https://pubs.acs.org/doi/pdf/10.1021/x")

    def test_relative_meta_resolved(self):
        html = ('<html><head><meta name="citation_pdf_url" '
                'content="/content/pdf/10.1007/x.pdf"></head></html>')
        got = self.eng._find_pdf_link(html, "https://link.springer.com/article/10.1007/x")
        self.assertEqual(got, "https://link.springer.com/content/pdf/10.1007/x.pdf")

    def test_anchor_text_pdf(self):
        html = '<html><body><a href="/doi/pdfdirect/10.1111/nph.18685">PDF</a></body></html>'
        got = self.eng._find_pdf_link(html, "https://onlinelibrary.wiley.com/doi/10.1111/nph.18685")
        self.assertEqual(got, "https://onlinelibrary.wiley.com/doi/pdfdirect/10.1111/nph.18685")

    def test_no_pdf_link(self):
        html = "<html><body><a href='/abc'>page</a></body></html>"
        self.assertIsNone(self.eng._find_pdf_link(html, "https://example.org/"))

    def test_publisher_pdf_url_shared(self):
        from paperflow.pdf.agentbrowser import publisher_pdf_url
        self.assertEqual(
            publisher_pdf_url("10.1021/acs.jcim.1c00799",
                              "https://pubs.acs.org/doi/10.1021/acs.jcim.1c00799"),
            "https://pubs.acs.org/doi/pdf/10.1021/acs.jcim.1c00799")


if __name__ == "__main__":
    unittest.main()