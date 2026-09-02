"""CARSI 通道单元测试：出版社探测、PDF 路径模板、cookie 会话、空跑行为。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paperflow.pdf.carsi import (CarsiEngine, detect_publisher, load_cookies,
                                 save_cookies, cookie_path, PUBLISHER_SSO)
from paperflow.pdf.carsi import IDP_MAP


class CarsiDetectTests(unittest.TestCase):
    def test_detect_publisher(self):
        self.assertEqual(detect_publisher("https://pubs.acs.org/doi/10.1021/x"), "acs")
        self.assertEqual(detect_publisher("https://onlinelibrary.wiley.com/doi/10.1111/x"), "wiley")
        self.assertEqual(detect_publisher("https://www.sciencedirect.com/science/article/pii/X"), "sciencedirect")
        self.assertEqual(detect_publisher("https://link.springer.com/article/10.1007/x"), "springer")
        self.assertEqual(detect_publisher("https://www.nature.com/articles/x"), "nature")
        self.assertEqual(detect_publisher("https://academic.oup.com/nar/article/x"), "oxford")
        self.assertIsNone(detect_publisher("https://example.org/x"))

    def test_publisher_configs_cover_targets(self):
        # 之前失败清单涉及的关键出版社必须有 CARSI 配置
        for pub in ("wiley", "acs", "sciencedirect", "springer", "nature",
                    "tandfonline", "ieee", "oxford"):
            self.assertIn(pub, PUBLISHER_SSO)

    def test_pdf_template(self):
        suffix = "nph.18685"
        tpl = PUBLISHER_SSO["wiley"]["pdfs"][0]
        self.assertEqual(tpl.format(doi="10.1111/nph.18685", suffix=suffix),
                         "/doi/pdfdirect/10.1111/nph.18685")
        tpl2 = PUBLISHER_SSO["sciencedirect"]["pdfs"][0]
        self.assertEqual(
            tpl2.format(doi="10.1016/j.x", suffix="j.x"),
            "/science/article/pii/j.x/pdfft")

    def test_idp_map_has_capital_normal(self):
        self.assertEqual(IDP_MAP.get("首都师范大学"), "Capital Normal")


class CarsiCookieTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path("/tmp/carsi_test_wiley.json")
        self._orig = cookie_path("wiley")
        import shutil
        if self._orig.exists():
            shutil.copy2(self._orig, self._tmp)
        else:
            self._tmp.write_text("[]")

    def tearDown(self):
        pass

    def test_save_load_roundtrip(self):
        save_cookies("wiley", [{"name": "a", "value": "1", "domain": ".wiley.com"}])
        got = load_cookies("wiley")
        self.assertTrue(any(c["name"] == "a" for c in got))
        # 恢复现场
        import shutil
        shutil.copy2(self._tmp, cookie_path("wiley"))

    def test_load_missing(self):
        self.assertEqual(load_cookies("carsi_nonexistent_pub_zzz"), [])


class CarsiEngineTests(unittest.TestCase):
    def test_fetch_scihub_unrelated(self):
        # 无学校配置时应给出明确提示，而不是崩溃
        eng = CarsiEngine(idp_name="")
        ok, info = eng.fetch("10.1021/acs.jcim.1c00799", Path("/tmp/t.pdf"))
        self.assertFalse(ok)
        self.assertIn("未指定学校", info)

    def test_fetch_unknown_publisher(self):
        eng = CarsiEngine(idp_name="首都师范大学")
        ok, info = eng.fetch("10.1234/abc", Path("/tmp/t.pdf"))
        self.assertFalse(ok)
        self.assertIn("无法识别出版社", info)


if __name__ == "__main__":
    unittest.main()