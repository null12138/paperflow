"""WOS -> publisher full-text downloader using the user's real browser session.

This intentionally drives the visible WOS UI and publisher link (rather than
calling a hidden endpoint).  The browser retains the institution's existing
authorization; Paperflow only moves a PDF that the browser downloaded itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import requests

BRIDGE = "http://127.0.0.1:10086/command"
SESSION = "wos"
WOS_URL = "https://webofscience.clarivate.cn/wos/woscc/basic-search"
WOS_API_URL = "https://api.clarivate.com/apis/wos-starter/v1/documents"
DOWNLOADS = Path.home() / "Downloads"


# Publisher-specific selectors.  WOS only supplies the authenticated hand-off;
# the final PDF control belongs to the publisher and must be selected there.
PUBLISHER_ADAPTERS = {
    "springer": ("springer", ("link.springer.com", "www.springernature.com"), ("/content/pdf/",)),
    "elsevier": ("elsevier", ("sciencedirect.com", "www.sciencedirect.com"), ("/pdfft", "/pdf/")),
    "wiley": ("wiley", ("onlinelibrary.wiley.com",), ("/doi/pdf", "/pdfdirect/")),
    "oxford": ("oxford", ("academic.oup.com",), ("article-pdf", "/pdf/")),
    "nature": ("nature", ("nature.com",), (".pdf", "/articles/")),
    "taylor_francis": ("taylor_francis", ("tandfonline.com",), ("/doi/pdf",)),
    "sage": ("sage", ("journals.sagepub.com",), ("/doi/pdf",)),
    "mdpi": ("mdpi", ("mdpi.com", "www.mdpi.com"), ("/pdf",)),
    "plos": ("plos", ("journals.plos.org",), ("article/file",)),
    "ieee": ("ieee", ("ieeexplore.ieee.org",), ("stamp.jsp", "/pdf/")),
    "acs": ("acs", ("pubs.acs.org",), ("/doi/pdf",)),
    "rsc": ("rsc", ("pubs.rsc.org",), ("articlepdf",)),
    "bmj": ("bmj", ("bmj.com",), ("content/", ".full.pdf", "/doi/pdf")),
    "aps": ("aps", ("journals.aps.org",), ("/pdf/",)),
    "aip": ("aip", ("pubs.aip.org",), ("/doi/pdf/",)),
    "acm": ("acm", ("dl.acm.org",), ("doi/pdf",)),
    "cambridge": ("cambridge", ("cambridge.org",), ("/core/services/aop-cambridge-core/content/view/",)),
    "cell": ("cell", ("cell.com",), ("pdf",)),
    "frontiers": ("frontiers", ("frontiersin.org",), ("pdf",)),
    "hindawi": ("hindawi", ("hindawi.com",), ("pdf",)),
    "thieme": ("thieme", ("thieme-connect.de",), ("pdf",)),
    "emerald": ("emerald", ("emerald.com",), ("pdf",)),
    "de_gruyter": ("de_gruyter", ("degruyter.com",), ("pdf",)),
    "karger": ("karger", ("karger.com",), ("pdf",)),
    "lww": ("lww", ("lww.com", "journals.lww.com"), ("pdf",)),
    "jstor": ("jstor", ("jstor.org",), ("stable/pdf",)),
    "bioone": ("bioone", ("bioone.org",), ("pdf",)),
    "scielo": ("scielo", ("scielo.br", "scielo.org"), ("pdf",)),
}


def publisher_adapter(host: str) -> str:
    host = (host or "").casefold()
    for name, (_, domains, _) in PUBLISHER_ADAPTERS.items():
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return name
    return "generic"


class WosBrowserEngine:
    def __init__(self, downloads_dir: Path | None = None, timeout: float = 90) -> None:
        self.downloads_dir = Path(downloads_dir or DOWNLOADS)
        self.timeout = timeout

    def _bridge(self, action: str, args: dict | None = None) -> dict:
        payload = json.dumps({
            "action": action, "args": args or {}, "session": SESSION,
        }).encode()
        request = urllib.request.Request(
            BRIDGE, data=payload, headers={"Content-Type": "application/json"}
        )
        # The local extension bridge occasionally answers 502 while Chrome is
        # switching tabs.  Retry the idempotent command briefly; without this
        # one transient hand-off failure aborts every item in a batch.
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    result = json.loads(response.read().decode())
                break
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in {502, 503, 504} or attempt == 3:
                    break
                time.sleep(0.5 * (attempt + 1))
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt == 3:
                    break
                time.sleep(0.5 * (attempt + 1))
        # On macOS some Python HTTP stacks intermittently receive an empty 502
        # from the local daemon while curl succeeds.  Use curl as a transport
        # fallback; it still talks only to localhost and does not alter the
        # browser session or credentials.
        try:
            raw = subprocess.check_output(
                ["curl", "-sS", "--max-time", "20", "-X", "POST", BRIDGE,
                 "-H", "Content-Type: application/json", "--data-binary", payload],
                text=True,
            )
            result = json.loads(raw)
        except (OSError, subprocess.CalledProcessError, ValueError):
            if last_error:
                raise last_error
            raise RuntimeError("WebBridge 请求失败")
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or result))
        return result.get("data") or {}

    def _evaluate(self, code: str):
        return (self._bridge("evaluate", {"code": code}).get("value"))

    def _open_wos(self) -> None:
        # The bridge operates on the session's selected tab.  Closing stale
        # WOS/publisher tabs prevents a previous hand-off from stealing focus
        # between DOI requests; browser login state itself is retained.
        try:
            self._bridge("close_session")
        except Exception:
            pass
        # WebBridge requires find_tab before opening a page.  After closing a
        # stale session this normally returns no tab, but keeping the lookup
        # here also lets a user-opened WOS tab be reused safely.
        try:
            found = self._bridge("find_tab", {"url": "https://webofscience.clarivate.cn", "active": False})
        except Exception:
            found = {}
        if not found:
            self._bridge("navigate", {
                "url": WOS_URL, "newTab": True, "group_title": SESSION,
            })
        # Let the WOS SPA finish bootstrapping before a direct UID navigation;
        # otherwise its initial route can overwrite the requested record URL.
        self._wait("!!document.querySelector('input[aria-label^=\"Search box 1\"]')", 45)

    def _require_login(self) -> None:
        """Fail early with an actionable message when the bridge tab is logged out."""
        logged_out = self._evaluate(
            "!![...document.querySelectorAll('button,a')].find(e => /^(Sign In|登录)$/i.test((e.innerText || '').trim()))"
        )
        if logged_out:
            raise RuntimeError("WOS 浏览器未登录，请在 WebBridge 使用的浏览器标签中先完成机构登录")

    @staticmethod
    def _resolve_uid(doi: str) -> str:
        """Resolve DOI to WOS UID through the user's official API key."""
        key = os.getenv("WOS_API_KEY", "").strip()
        if not key:
            return ""
        try:
            response = requests.get(
                WOS_API_URL,
                headers={"X-ApiKey": key, "Accept": "application/json"},
                params={"db": "WOS", "q": f"DO=({doi})", "limit": 1, "page": 1},
                timeout=(6, 18),
            )
            response.raise_for_status()
            hits = response.json().get("hits") or []
            uid = str(hits[0].get("uid") or "") if hits else ""
            return uid if uid.startswith("WOS:") else ""
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            return ""

    def _wait(self, predicate: str, timeout: float | None = None) -> bool:
        deadline = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < deadline:
            try:
                if self._evaluate(predicate):
                    return True
            except Exception:
                pass
            time.sleep(1.0)
        return False

    def _search_doi(self, doi: str) -> None:
        if not self._wait(
            "!!document.querySelector('input[aria-label^=\"Search box 1\"]')", 45
        ):
            raise RuntimeError("WOS 检索框未出现，浏览器可能未登录或仍在 Robot 验证")
        # The WOS field selector opens a menu; choosing DOI is a real UI click.
        selected = self._evaluate("""(() => {
          const current = document.querySelector('input[aria-label^="Search box 1"]');
          if (current && /\\bDOI\\b/i.test(current.getAttribute('aria-label') || '')) {
            document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}));
            return true;
          }
          const button = [...document.querySelectorAll('button')].find(
            e => (e.innerText || '').trim() === 'Topic');
          if (button) button.click();
          const option = [...document.querySelectorAll(
            '[role="option"],mat-option,.cdk-option,[data-value]')].find(
            e => (e.innerText || '').trim() === 'DOI' && e.offsetParent);
          if (option) { option.click(); return true; }
          return false;
        })()""")
        if not selected:
            raise RuntimeError("WOS 未能切换到 DOI 检索字段")
        script = """(() => {
          const i = document.querySelector('input[aria-label^="Search box 1"]');
          if (!i) return false;
          const p = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
          p.set.call(i, %s);
          i.dispatchEvent(new Event('input', {bubbles:true}));
          i.dispatchEvent(new Event('change', {bubbles:true}));
          return true;
        })()""" % json.dumps(doi)
        if not self._evaluate(script):
            raise RuntimeError("WOS DOI 检索提交失败")
        # Use the bridge's click primitive for the Angular submit control; a
        # page-context click can be ignored by WOS even though it reports OK.
        try:
            self._bridge("click", {"selector": "button[type=\"submit\"]"})
        except Exception:
            self._evaluate("document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true}))")
        if not self._wait("location.href.includes('/summary/')", self.timeout):
            raise RuntimeError("WOS DOI 检索未返回结果页")
        # The result list is a lazy tab in the current WOS UI; explicitly
        # activate Documents before looking for the record link.
        try:
            self._bridge("click", {"selector": "[role=\"tab\"]"})
        except Exception:
            pass
        time.sleep(2)

    def _click_full_text(self) -> None:
        # Open the WOS full record first, then click its authorized full-text
        # link.  target=_self keeps this operation in the bridge-controlled tab.
        current_url = str(self._evaluate("location.href") or "")
        if "/full-record/" not in current_url and not self._wait(
            "!!document.querySelector('a[href*=\"/full-record/\"]')", self.timeout
        ):
            raise RuntimeError("WOS 结果页没有加载单篇记录链接")
        self._wait(
            "!![...document.querySelectorAll('a')].find(e => /Full Text/i.test(e.innerText || ''))",
            self.timeout,
        )
        if not self._evaluate("""(() => {
          const a = [...document.querySelectorAll('a')].find(
            e => e.href.includes('/full-record/'));
          if (!a) return false;
          a.target = '_self'; a.click(); return true;
        })()"""):
            raise RuntimeError("WOS 结果页没有找到单篇记录链接")
        if not self._wait("location.href.includes('/full-record/')", self.timeout):
            raise RuntimeError("WOS 单篇记录页加载超时")
        if not self._evaluate("""(() => {
          const a = [...document.querySelectorAll('a')].find(
            e => /Free Full Text|Full Text from Publisher/i.test(e.innerText || ''));
          if (!a) return false;
          a.target = '_self'; a.click(); return true;
        })()"""):
            raise RuntimeError("WOS 单篇没有可用的 Full Text 授权链接")
        if not self._wait(
            "!location.hostname.toLowerCase().includes('webofscience.')",
            self.timeout,
        ):
            raise RuntimeError("WOS 全文链接未完成出版社跳转")

    def _click_pdf(self) -> Path:
        before = {p for p in self.downloads_dir.glob("*.pdf") if p.is_file()}
        if not self._wait(
            "!location.hostname.toLowerCase().includes('webofscience.')",
            45,
        ):
            raise RuntimeError("出版社全文页面未加载")
        if not self._wait(
            "!![...document.querySelectorAll('a[href]')].find(e => /Download PDF|Article PDF|View PDF/i.test(e.innerText || e.getAttribute('aria-label') || ''))",
            30,
        ):
            # Some publishers expose only an unlabeled PDF URL; the selector
            # below still gets a chance to match it.
            pass
        clicked = self._evaluate("""(() => {
          const host = location.hostname.toLowerCase();
          const rules = [
            [['link.springer.com','www.springernature.com'], ['/content/pdf/']],
            [['sciencedirect.com','www.sciencedirect.com'], ['/pdfft','/pdf/']],
            [['onlinelibrary.wiley.com'], ['/doi/pdf','/pdfdirect/']],
            [['academic.oup.com'], ['article-pdf','/pdf/']],
            [['nature.com'], ['.pdf','/articles/']],
            [['tandfonline.com'], ['/doi/pdf']],
            [['journals.sagepub.com'], ['/doi/pdf']],
            [['mdpi.com','www.mdpi.com'], ['/pdf']],
            [['journals.plos.org'], ['article/file']],
            [['ieeexplore.ieee.org'], ['stamp.jsp','/pdf/']],
            [['pubs.acs.org'], ['/doi/pdf']],
            [['pubs.rsc.org'], ['articlepdf']],
            [['bmj.com'], ['content/','.full.pdf','/doi/pdf']],
            [['journals.aps.org'], ['/pdf/']],
            [['pubs.aip.org'], ['/doi/pdf/']],
            [['dl.acm.org'], ['doi/pdf']],
            [['cambridge.org'], ['/core/services/aop-cambridge-core/content/view/']],
            [['cell.com'], ['pdf']],
            [['frontiersin.org'], ['pdf']],
            [['hindawi.com'], ['pdf']],
            [['thieme-connect.de'], ['pdf']],
            [['emerald.com'], ['pdf']],
            [['degruyter.com'], ['pdf']],
            [['karger.com'], ['pdf']],
            [['lww.com','journals.lww.com'], ['pdf']],
            [['jstor.org'], ['stable/pdf']],
            [['bioone.org'], ['pdf']],
            [['scielo.br','scielo.org'], ['pdf']]
          ];
          const rule = rules.find(([domains]) => domains.some(
            d => host === d || host.endsWith('.' + d)));
          const links = [...document.querySelectorAll('a[href]')];
          const preferred = rule ? links.find(e => rule[1].some(
            token => e.href.toLowerCase().includes(token))) : null;
          const labeled = links.find(e => /Download PDF|Article PDF|View PDF/i.test(
            e.innerText || e.getAttribute('aria-label') || ''));
          const a = preferred || labeled;
          if (!a) return false;
          a.target = '_self'; a.click(); return true;
        })()""")
        if not clicked:
            raise RuntimeError("出版社页面没有找到 Download PDF 链接")
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            candidates = [
                p for p in self.downloads_dir.glob("*.pdf")
                if p.is_file() and p not in before and p.stat().st_size > 0
            ]
            if candidates:
                return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]
            time.sleep(1.0)
        raise RuntimeError("浏览器点击后未发现下载文件")

    @staticmethod
    def _valid_pdf(path: Path) -> bool:
        try:
            return path.read_bytes()[:5] == b"%PDF-"
        except OSError:
            return False

    def fetch(self, doi: str, target: Path) -> tuple[bool, str]:
        if not doi.strip():
            return False, "WOS 浏览器下载需要 DOI"
        try:
            self._open_wos()
            self._require_login()
            clean_doi = doi.strip()
            uid = self._resolve_uid(clean_doi)
            if uid:
                record_url = f"https://webofscience.clarivate.cn/wos/woscc/full-record/{uid}"
                try:
                    self._bridge("find_tab", {"url": record_url, "active": False})
                except Exception:
                    self._bridge("navigate", {
                        "url": record_url, "newTab": False, "group_title": SESSION,
                    })
                if not self._wait("location.href.includes('/full-record/')", self.timeout):
                    uid = ""
            if not uid:
                self._search_doi(clean_doi)
                self._click_full_text()
            else:
                try:
                    self._click_full_text()
                except RuntimeError:
                    # Some WOS deployments render Full Text Links only after a
                    # normal DOI search, even though the direct UID page works.
                    try:
                        self._bridge("find_tab", {"url": WOS_URL, "active": False})
                    except Exception:
                        self._bridge("navigate", {
                            "url": WOS_URL, "newTab": False, "group_title": SESSION,
                        })
                    self._search_doi(clean_doi)
                    self._click_full_text()
            source = self._click_pdf()
            if not self._valid_pdf(source):
                return False, "WOS 浏览器下载结果不是有效 PDF"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            return True, f"授权下载模式（WOS）→ {doi}"
        except (OSError, urllib.error.URLError, RuntimeError) as exc:
            return False, f"WOS 浏览器失败：{str(exc)[:180]}"
