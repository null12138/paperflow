"""AgentBrowser 通道：给出版社文章 URL（或 DOI），用真实浏览器打开并取 PDF。

适用场景：**校园网/机构网络内**——出口 IP 自带机构订阅授权，无需登录。
AI/脚本把出版社文章页地址交给本引擎，Playwright 打开页面（等待 Cloudflare
自动放行）→ 网络响应捕获 PDF / 构造出版社 PDF 直链 / 页面找 PDF 链接或点击
PDF 按钮 → 校验 %PDF- 后落盘。

凭用户已有机构网络权限下载，不绕过验证码、付费墙或访问控制；
CF 交互式挑战无法自动通过时如实提示。
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from pathlib import Path

import requests

log = logging.getLogger(__name__)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

_IUCr = None


def _aes():  # pragma: no cover - 仅为兼容导入预留
    return None


def publisher_pdf_url(doi: str, resolved_url: str) -> str | None:
    """按出版社构造标准 PDF 直链（与 webvpn/carsi 通道共享同一套规则）。"""
    from .webvpn import publisher_pdf_url as _ppu
    return _ppu(doi, resolved_url)


def _resolve_doi(doi: str) -> str | None:
    try:
        r = requests.get(f"https://doi.org/{doi}", allow_redirects=True,
                         timeout=20, headers={"User-Agent": UA}, stream=True)
        r.close()
        if r.url and r.url != f"https://doi.org/{doi}":
            return r.url
    except Exception:
        pass
    return None


class AgentBrowserEngine:
    """浏览器操作通道：输入出版社 URL 或 DOI，输出 %PDF- 文件。"""

    def __init__(self, headful: bool = True, timeout: float = 90):
        self.headful = headful
        self.timeout = timeout

    def fetch(self, raw: str, target: Path) -> tuple[bool, str]:
        """raw 可为完整 URL 或 DOI。"""
        raw = raw.strip()
        if not raw:
            return False, "空输入"
        if raw.lower().startswith("http"):
            article_url = raw
            doi = self._extract_doi(raw) or ""
        else:
            doi = raw
            article_url = _resolve_doi(doi) or f"https://doi.org/{doi}"
        try:
            return self._browse(article_url, doi, target)
        except Exception as exc:
            return False, f"browser: {type(exc).__name__}: {str(exc)[:120]}"

    @staticmethod
    def _extract_doi(url: str) -> str:
        # https://doi.org/10.xxxx/yyy 或含 doi 查询参数
        if "doi.org/" in url:
            return url.split("doi.org/", 1)[1].split("?")[0]
        m = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("doi")
        return m[0] if m else ""

    # ------------------------------------------------------------------
    def _browse(self, article_url: str, doi: str, target: Path) -> tuple[bool, str]:
        from .. import auth as _auth

        captured: list[bytes] = []
        _p, browser, ctx = _auth._launch_browser(headful=self.headful)
        try:
            page = ctx.new_page()

            def on_response(response):
                try:
                    ct = response.headers.get("content-type", "")
                    u = response.url.lower()
                    if not ("pdf" in ct.lower() or "octet-stream" in ct.lower()
                            or u.endswith(".pdf") or "/pdfdirect/" in u or "/doi/pdf/" in u):
                        return
                    if response.status >= 400:
                        return
                    body = response.body()
                    if body[:4] == b"%PDF-":
                        captured.append(body)
                except Exception:
                    pass

            page.on("response", on_response)

            # 1) 打开文章页；等待 Cloudflare 自动放行（最多 ~60s）
            page.goto(article_url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(4)
            for _ in range(10):
                t = (page.title() or "").lower()
                if "just a moment" in t or "请稍候" in t or "安全验证" in t:
                    log.info("CF challenge… waiting")
                    time.sleep(6)
                else:
                    break

            target.parent.mkdir(parents=True, exist_ok=True)

            # 2) 中性：若爬取过程中已捕获 PDF
            if captured:
                target.write_bytes(captured[-1])
                return True, f"browser 响应捕获 → {target.name}"

            # 3) 直链尝试：已知出版社构造 pdf 路径
            if doi:
                resolved_for_pdf = _resolve_doi(doi) or article_url
                pdf_url = publisher_pdf_url(doi, resolved_for_pdf)
                if pdf_url:
                    captured.clear()
                    self._goto_capture(page, pdf_url, captured)
                    if captured:
                        target.write_bytes(captured[-1])
                        return True, f"browser {pdf_url[:66]}"
                # 若页面本身是内嵌 PDF（URL 以 .pdf 结尾或 embed/iframe）
            body_txt = ""
            try:
                body_txt = page.evaluate("document.body ? document.body.innerText : ''") or ""
            except Exception:
                pass
            # 内嵌 PDF 检查
            self._try_inline_pdf(page, captured)
            if captured:
                target.write_bytes(captured[-1])
                return True, "browser 内嵌 PDF"

            # 4) 页面 HTML 找 PDF 链接
            html = ""
            try:
                html = page.content()
            except Exception:
                pass
            found = self._find_pdf_link(html, page.url)
            if found:
                captured.clear()
                self._goto_capture(page, found, captured)
                if captured:
                    target.write_bytes(captured[-1])
                    return True, f"browser {found[:66]}"

            # 5) 点击 "PDF / Download PDF" 按钮
            click_ok = self._click_pdf_button(page)
            if click_ok:
                time.sleep(6)
                if captured:
                    target.write_bytes(captured[-1])
                    return True, "browser 点击 PDF 按钮"
                # 下载事件兜底
                self._try_download_after_click(page, target)

            if target.exists() and target.read_bytes()[:4] == b"%PDF-":
                return True, "browser 下载事件"

            return False, ("browser: 页面已打开但未出现 PDF"
                           "（CF 拦截/无此刊权限/页面结构特殊）")
        finally:
            try:
                browser.close()
            except Exception:
                pass
            try:
                _p.stop()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _goto_capture(self, page, url: str, captured: list[bytes]) -> None:
        try:
            page.goto(url, wait_until="commit", timeout=30000)
            time.sleep(5)
        except Exception:
            return
        if captured and captured[-1][:4] == b"%PDF-":
            return
        # 等待捕获完成
        for _ in range(8):
            if captured and captured[-1][:4] == b"%PDF-":
                return
            time.sleep(2)

    def _try_inline_pdf(self, page, captured: list[bytes]) -> None:
        try:
            result = page.evaluate(
                "(async () => {"
                " const el = document.querySelector('embed[type=\"application/pdf\"], "
                "   object[type=\"application/pdf\"], iframe[src*=\".pdf\"]');"
                " const src = el ? (el.src || el.data || window.location.href) : window.location.href;"
                " if (!src.endsWith('.pdf')) return null;"
                " try { const r = await fetch(src); if (!r.ok) return null;"
                " const b = await r.arrayBuffer(); const u = new Uint8Array(b);"
                " if (u[0]!==0x25||u[1]!==0x50||u[2]!==0x44||u[3]!==0x46) return null;"
                " let bin=''; for (let i=0;i<u.length;i++) bin += String.fromCharCode(u[i]);"
                " return btoa(bin); } catch(e) { return null; } })()")
            if result:
                import base64
                captured.append(base64.b64decode(result))
        except Exception:
            pass

    def _find_pdf_link(self, html: str, base_url: str) -> str | None:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
        parsed = urllib.parse.urlparse(base_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
        if meta and meta.get("content"):
            c = meta["content"]
            return c if c.startswith("http") else base + c
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True).lower()
            classes = " ".join(a.get("class", []))
            if any(k in text for k in ("pdf", "download pdf", "full text pdf", "view pdf", "get pdf")):
                return href if href.startswith("http") else base + href
            if any(k in classes for k in ("pdf", "download-pdf")):
                return href if href.startswith("http") else base + href
            if href.endswith(".pdf") or "/doi/pdf/" in href or "/doi/pdfdirect/" in href:
                return href if href.startswith("http") else base + href
        return None

    def _click_pdf_button(self, page) -> str | None:
        try:
            return page.evaluate(
                "() => { const links=[...document.querySelectorAll('a, button')];"
                " for (const a of links){ const h=(a.href||'').toLowerCase();"
                " const t=(a.innerText||a.value||'').toLowerCase();"
                " if ((h.includes('pdf')||h.includes('download'))&&!h.includes('supplement')"
                "  && (t.includes('pdf')||t.includes('download')||t.includes('全文'))) {"
                "  a.click(); return a.href||t.slice(0,40); }"
                " } return null; }")
        except Exception:
            return None

    def _try_download_after_click(self, page, target: Path) -> None:
        try:
            with page.expect_download(timeout=15000) as dl:
                pass  # 已在 _click_pdf_button 中触发
        except Exception:
            pass