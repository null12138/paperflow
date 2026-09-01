import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from paperflow.models import Paper
from paperflow.pdf.cnki import CnkiPdfEngine
from paperflow.sources.cnki import CnkiSource, _cookie_for_playwright, _split_authors


class FakeContext:
    def __init__(self):
        self.cookies_added = []
        self.scripts = []

    def add_cookies(self, cookies):
        self.cookies_added.extend(cookies)

    def add_init_script(self, script):
        self.scripts.append(script)


class CnkiTests(unittest.TestCase):
    def test_cookie_normalization_drops_unknown_fields(self):
        cookie = _cookie_for_playwright({
            "name": "session",
            "value": "value",
            "domain": ".cnki.net",
            "path": "/",
            "priority": "High",
        })
        self.assertEqual(cookie, {
            "name": "session", "value": "value", "domain": ".cnki.net", "path": "/"
        })

    def test_launch_reuses_saved_cookie_and_local_storage(self):
        context = FakeContext()
        factory = Mock(return_value=("playwright", "browser", context))
        source = CnkiSource(headless=False, browser_factory=factory)
        session = {
            "cookies": [{"name": "session", "value": "v", "domain": ".cnki.net", "path": "/"}],
            "storage": {"institution": "Capital Normal University"},
        }
        with patch("paperflow.sources.cnki.auth.load_site_session", return_value=session):
            result = source._launch()
        self.assertEqual(result, ("playwright", "browser", context))
        factory.assert_called_once_with(True)
        self.assertEqual(len(context.cookies_added), 1)
        self.assertIn("institution", context.scripts[0])

    def test_page_problems_are_actionable(self):
        self.assertIn("安全验证", CnkiSource._page_problem("请输入验证码完成安全验证"))
        self.assertIn("auth login cnki", CnkiSource._page_problem("请登录后查看全文"))
        self.assertEqual(CnkiSource._page_problem("检索结果 共 10 条"), "")

    def test_result_is_converted_to_paper_with_abstract_and_doi(self):
        paper = CnkiSource._to_paper({
            "title": "银杏叶研究",
            "authors": "张三；李四",
            "journal": "中国中药杂志",
            "date": "2025-06-01",
            "abstract": "一段摘要",
            "doi": "https://doi.org/10.1234/CNKI.1",
        }, "Ginkgo biloba")
        self.assertEqual(paper.title, "银杏叶研究")
        self.assertEqual(paper.authors, ["张三", "李四"])
        self.assertEqual(paper.year, "2025")
        self.assertEqual(paper.abstract, "一段摘要")
        self.assertEqual(paper.doi, "10.1234/cnki.1")
        self.assertEqual(paper.sources, {"CNKI"})
        self.assertEqual(paper.species, {"Ginkgo biloba"})

    def test_pdf_detail_is_preserved_as_cnki_candidate(self):
        paper = CnkiSource._to_paper({
            "title": "银杏叶研究",
            "url": "https://oversea.cnki.net/kcms2/article/abstract?id=1",
            "pdf_url": "https://o.oversea.cnki.net/barnew/download/order?id=1",
        }, "银杏")
        self.assertEqual(len(paper.pdf_candidates), 1)
        self.assertEqual(paper.pdf_candidates[0].source, "cnki")
        self.assertIn("article/abstract", paper.pdf_candidates[0].url)

    def test_detail_requests_are_limited_to_one_per_second(self):
        source = CnkiSource()
        self.assertEqual(source.detail_limiter.interval, 1.0)

    def test_author_split_preserves_names_with_spaces(self):
        self.assertEqual(_split_authors("Zhang San；Li Si"), ["Zhang San", "Li Si"])


class _FakeLink:
    def __init__(self, visible):
        self.visible = visible
        self.clicked = False

    def is_visible(self):
        return self.visible

    def click(self, **kwargs):
        self.clicked = True


class _FakeLinks:
    def __init__(self, links):
        self.links = links

    def count(self):
        return len(self.links)

    def nth(self, index):
        return self.links[index]


class _FakeDownload:
    suggested_filename = "article.pdf"

    def failure(self):
        return None

    def save_as(self, path):
        Path(path).write_bytes(b"%PDF-1.7\n")


class _DownloadEvent:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @property
    def value(self):
        return _FakeDownload()


class _FakeBody:
    def inner_text(self, **kwargs):
        return "中国知网 文章详情 PDF Download"


class _FakePage:
    def __init__(self, link_visible=True):
        self.hidden = _FakeLink(False)
        self.visible = _FakeLink(link_visible)
        self.navigations = []

    def goto(self, *args, **kwargs):
        self.navigations.append(args[0])
        return None

    def locator(self, selector):
        return _FakeBody()

    def get_by_text(self, text, exact=False):
        return _FakeLinks([self.hidden, self.visible])

    def expect_download(self, **kwargs):
        return _DownloadEvent()

    def evaluate(self, script):
        if "PDF Download" in script and "?.href" in script:
            return "https://o.oversea.cnki.net/barnew/download/order?id=1"
        return {}

    def close(self):
        return None


class _FakeBrowser:
    def close(self):
        return None


class _FakePlaywright:
    def stop(self):
        return None


class _FakeDownloadContext(FakeContext):
    def __init__(self, page):
        super().__init__()
        self.page = page

    def new_page(self):
        return self.page

    def cookies(self):
        return []


class CnkiPdfTests(unittest.TestCase):
    def test_visible_duplicate_download_link_is_selected(self):
        page = _FakePage()
        selected = CnkiPdfEngine._visible_pdf_link(page)
        self.assertIs(selected, page.visible)

    def test_authorized_browser_download_must_be_valid_pdf(self):
        page = _FakePage()
        context = _FakeDownloadContext(page)
        factory = Mock(return_value=(_FakePlaywright(), _FakeBrowser(), context))
        engine = CnkiPdfEngine(headless=True, browser_factory=factory)
        paper = Paper(title="银杏叶研究", sources={"CNKI"})
        paper.add_candidate("https://oversea.cnki.net/kcms2/article/abstract?id=1", "cnki", 1)
        with tempfile.TemporaryDirectory() as directory, patch(
            "paperflow.pdf.cnki.auth.load_site_session", return_value={}
        ):
            target = Path(directory) / "article.pdf"
            ok, detail = engine.fetch(paper, target)
            self.assertTrue(ok)
            self.assertTrue(target.exists())
            self.assertIn("CNKI 授权下载", detail)
            self.assertFalse(page.hidden.clicked)
            self.assertTrue(page.visible.clicked)

    def test_zero_size_link_uses_current_authorized_url_navigation(self):
        page = _FakePage(link_visible=False)
        context = _FakeDownloadContext(page)
        factory = Mock(return_value=(_FakePlaywright(), _FakeBrowser(), context))
        engine = CnkiPdfEngine(headless=True, browser_factory=factory)
        paper = Paper(title="零尺寸按钮文章", sources={"CNKI"})
        paper.add_candidate("https://oversea.cnki.net/kcms2/article/abstract?id=2", "cnki", 1)
        with tempfile.TemporaryDirectory() as directory, patch(
            "paperflow.pdf.cnki.auth.load_site_session", return_value={}
        ):
            ok, _ = engine.fetch(paper, Path(directory) / "article.pdf")
        self.assertTrue(ok)
        self.assertTrue(any("barnew/download/order" in url for url in page.navigations))


if __name__ == "__main__":
    unittest.main()
