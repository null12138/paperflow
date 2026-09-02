"""WebVPN 通道单元测试：URL 转换、学校数据库、会话/引擎行为。"""

import sys
import unittest
from pathlib import Path

# 保证直接运行本文件也能 import paperflow
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paperflow.pdf.webvpn import WebVpnEngine, convert_url, publisher_pdf_url
from paperflow.schools import DEFAULT_KEY, get_school, search_schools


class WebVpnUrlTests(unittest.TestCase):
    def test_convert_url_default_key(self):
        # 北京大学 wpn.pku.edu.cn，默认密钥 wrdvpnisthebest!
        out = convert_url("https://pubs.acs.org/doi/pdf/10.1021/x",
                          "https://wpn.pku.edu.cn")
        self.assertTrue(out.startswith("https://wpn.pku.edu.cn/https/"))
        self.assertTrue("77726476706e69737468656265737421" in out)  # hex(wrdvpnisthebest!)
        self.assertTrue(out.endswith("/doi/pdf/10.1021/x"))

    def test_convert_url_preserves_path_query(self):
        out = convert_url("https://www.nature.com/articles/x.pdf?a=1&b=2",
                          "https://webvpn.sdu.edu.cn")
        self.assertTrue(out.endswith("/articles/x.pdf?a=1&b=2"))
        self.assertTrue(out.startswith("https://webvpn.sdu.edu.cn/https/"))

    def test_convert_url_scheme_and_token(self):
        out = convert_url("https://example.com/", "https://webvpn.x.edu.cn")
        head = out.split("https://webvpn.x.edu.cn/")[1]
        scheme, token = head.split("/", 1)
        token = token.rstrip("/")
        self.assertEqual(scheme, "https")
        # token = hex(iv) + hex(encrypted_hostname)，iv 为 16 字节 → 32 hex
        iv_hex, rest = token[:32], token[32:]
        self.assertEqual(iv_hex, "77726476706e69737468656265737421")
        self.assertTrue(rest and len(rest) % 2 == 0)

    def test_publisher_pdf_url(self):
        self.assertEqual(
            publisher_pdf_url("10.1021/acs.jcim.1c00799",
                              "https://pubs.acs.org/doi/10.1021/acs.jcim.1c00799"),
            "https://pubs.acs.org/doi/pdf/10.1021/acs.jcim.1c00799")
        self.assertEqual(
            publisher_pdf_url("10.1111/nph.18685",
                              "https://onlinelibrary.wiley.com/doi/10.1111/nph.18685"),
            "https://onlinelibrary.wiley.com/doi/pdfdirect/10.1111/nph.18685")
        m = publisher_pdf_url("10.1016/j.tree.2013.11.003",
                              "https://www.sciencedirect.com/science/article/pii/S0169534713002621")
        self.assertEqual(m, "https://www.sciencedirect.com/science/article/pii/S0169534713002621/pdfft")
        self.assertIsNone(publisher_pdf_url("10.1234/a", "https://unknown.example.org/x"))


class SchoolDbTests(unittest.TestCase):
    def test_get_school(self):
        s = get_school("北京大学")
        self.assertEqual(s.host, "https://wpn.pku.edu.cn")
        self.assertEqual(s.key, DEFAULT_KEY)

    def test_get_school_fuzzy(self):
        s = get_school("清华")
        self.assertIn("清华", s.name)

    def test_search_schools(self):
        hits = search_schools("北京理工")
        self.assertTrue(hits)
        self.assertTrue(all("北京理工" in e.name for e in hits))

    def test_school_not_found(self):
        with self.assertRaises(ValueError):
            get_school("不存在的大学xyz")


class WebVpnEngineTests(unittest.TestCase):
    def test_fetch_without_session(self):
        eng = WebVpnEngine(session_file=Path("/tmp/nonexist_webvpn.json"))
        ok, info = eng.fetch("10.1021/x", Path("/tmp/t.pdf"))
        self.assertFalse(ok)
        self.assertIn("未登录", info)

    def test_session_status_none(self):
        eng = WebVpnEngine(session_file=Path("/tmp/nonexist_webvpn.json"))
        self.assertEqual(eng.session_status(), "none")


if __name__ == "__main__":
    unittest.main()