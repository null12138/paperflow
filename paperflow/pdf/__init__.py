"""PDF 下载引擎：支持 CNKI 授权、Sci-Hub、开放获取、出版社订阅与本地复用。"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests

from .. import net
from ..models import Paper
from .scihub import SciHubEngine
from .oa import OaEngine
from .publisher import PublisherEngine
from .wos_browser import WosBrowserEngine
from .wos_selenium import WosSeleniumEngine


def safe_slug(title: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", title).strip("._")[:160] or "paper"


def paper_filename(paper: Paper) -> str:
    """Return a stable, collision-resistant filename for a paper.

    Titles alone are not identities: corrections and their original articles can
    share the same first 160 characters.  Reusing a title-only path can therefore
    silently attach one paper's PDF to another database row.  Keep a readable
    prefix and add a digest of the strongest available scholarly identifier.
    """
    identity = paper.doi or paper.pmid or paper.pmcid or paper.title
    digest = hashlib.sha256(identity.casefold().encode("utf-8")).hexdigest()[:12]
    readable = safe_slug(paper.title or identity)[:145]
    return f"{readable}_{digest}.pdf"


def pdf_ok(path: Path) -> bool:
    try:
        return path.read_bytes()[:5] == b"%PDF-"
    except OSError:
        return False


class PdfEngine:
    def __init__(
        self,
        out_dir: Path,
        email: str = "",
        proxies: list[str | None] | None = None,
        max_per_minute: int = 30,
        cookie_file: Path | None = None,
        use_scihub: bool = True,
        use_oa: bool = True,
        use_publisher: bool = False,
        use_wos: bool = False,
        use_cnki: bool = False,
        use_direct_candidates: bool | None = None,
        pmc_only: bool = False,
    ) -> None:
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.email = email
        self.proxies = proxies or net.DEFAULT_PROXIES
        self.limiter = net.RateLimiter(max_per_minute)
        self.cookie_file = cookie_file
        self.scihub = SciHubEngine(proxies=self.proxies, cookie_file=cookie_file) if use_scihub else None
        self.oa = OaEngine(email=email, proxies=self.proxies) if use_oa else None
        self.use_direct_candidates = use_oa if use_direct_candidates is None else use_direct_candidates
        self.pmc_only = pmc_only
        self.publisher = PublisherEngine(proxies=self.proxies) if use_publisher else None
        # Authorized mode defaults to a visible local Selenium browser.  The
        # WebBridge implementation remains available only when explicitly
        # requested for backwards compatibility.
        browser_mode = os.getenv("PAPERFLOW_WOS_BROWSER", "selenium").strip().lower()
        self.wos = (WosBrowserEngine() if browser_mode in {"webbridge", "kimi"} else WosSeleniumEngine()) if use_wos else None
        if use_cnki:
            from .cnki import CnkiPdfEngine
            self.cnki = CnkiPdfEngine()
        else:
            self.cnki = None

    def _target_path(self, paper: Paper) -> Path:
        return self.out / paper_filename(paper)

    def _fetch_direct_candidate(self, url: str, target: Path) -> tuple[bool, str]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False, "候选 URL 不是有效的 HTTP(S) 地址"
        if parsed.hostname.casefold() in {"doi.org", "dx.doi.org", "linkinghub.elsevier.com"}:
            return False, "候选是 DOI 落地页，不是直接 PDF"
        hostname = parsed.hostname.casefold()
        if hostname in {"www.mdpi.com", "mdpi.com"} and not parsed.path.rstrip("/").endswith("/pdf"):
            parsed = parsed._replace(path=parsed.path.rstrip("/") + "/pdf")
            url = urlunparse(parsed)
        if hostname == "pmc.ncbi.nlm.nih.gov":
            match = re.search(r"/articles/(PMC)?(\d+)", parsed.path, flags=re.I)
            if match:
                url = f"https://europepmc.org/articles/PMC{match.group(2)}?pdf=render"
        partial = target.with_suffix(target.suffix + ".part")
        partial.unlink(missing_ok=True)
        last_error = ""
        non_pdf = False
        try:
            for proxy in self.proxies:
                session = net.make_session(proxy, email=self.email)
                # Europe PMC 偶发 500/超时；每个出口有限重试一次，避免把临时错误
                # 永久记为失败。普通 4xx 和非 PDF 页面不在同一出口反复请求。
                for attempt in range(2):
                    try:
                        response = session.get(url, timeout=15, allow_redirects=True, stream=True)
                        response.raise_for_status()
                        with partial.open("wb") as handle:
                            for chunk in response.iter_content(chunk_size=1024 * 128):
                                if chunk:
                                    handle.write(chunk)
                        if pdf_ok(partial):
                            partial.replace(target)
                            return True, f"直接候选 → {response.url[:120]}"
                        non_pdf = True
                        break
                    except requests.HTTPError as exc:
                        status = exc.response.status_code if exc.response is not None else 0
                        last_error = f"HTTPError: HTTP {status or 'unknown'}"
                        if attempt == 0 and (status == 429 or status >= 500):
                            time.sleep(1)
                            continue
                        break
                    except requests.RequestException as exc:
                        last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
                        if attempt == 0:
                            time.sleep(1)
                            continue
                        break
                    finally:
                        partial.unlink(missing_ok=True)
            if non_pdf:
                return False, "候选返回内容不是 PDF"
            return False, last_error or "直接候选下载失败"
        finally:
            partial.unlink(missing_ok=True)

    def fetch(self, paper: Paper) -> tuple[bool, str]:
        """下载单篇论文全文；返回 (成功?, 说明)。"""
        try:
            minimum_free = max(0.5, float(os.getenv("PAPERFLOW_MIN_FREE_GB", "3")))
        except ValueError:
            minimum_free = 3.0
        if shutil.disk_usage(self.out).free < minimum_free * 1024 ** 3:
            paper.failure_reason = f"磁盘剩余空间不足 {minimum_free:g} GB，已停止写入"
            paper.download_detail = paper.failure_reason
            return False, paper.failure_reason
        target = self._target_path(paper)
        if pdf_ok(target):
            paper.downloaded_path = str(target)
            paper.download_source = "local"
            paper.download_detail = "已有有效PDF，复用"
            return True, paper.download_detail

        strategies = []
        if self.cnki and any(candidate.source.casefold() == "cnki" for candidate in paper.pdf_candidates):
            strategies.append(("cnki", lambda: self.cnki.fetch(paper, target)))
        if self.use_direct_candidates:
            for candidate in paper.pdf_candidates:
                name = candidate.source.casefold()
                lowered_url = candidate.url.casefold()
                if self.pmc_only and not any(host in lowered_url for host in (
                    "europepmc.org", "pmc.ncbi.nlm.nih.gov"
                )):
                    continue
                # publisher 候选通常是落地页/付费端点，需要 PublisherEngine
                # 注入 API Key 或机构会话，不能作为普通 OA URL 直接抓取。
                if name not in {"cnki", "publisher"}:
                    strategies.append((name, lambda url=candidate.url: self._fetch_direct_candidate(url, target)))
        # 既有三种策略都以 DOI 为入口；无 DOI 的 CNKI 论文不应向它们传空字符串。
        if paper.doi:
            if self.oa:
                strategies.append(("oa", lambda: self.oa.fetch(paper.doi, target)))
            if self.scihub:
                strategies.append(("scihub", lambda: self.scihub.fetch(paper.doi, target)))
            if self.publisher:
                strategies.append(("publisher", lambda: self.publisher.fetch(paper.doi, target)))
            if self.wos:
                strategies.append(("wos", lambda: self.wos.fetch(paper.doi, target)))

        if not strategies:
            paper.failure_reason = "没有可用下载候选（CNKI 需检索详情候选，其他通道需 DOI）"
            paper.download_source = ""
            paper.download_detail = paper.failure_reason
            return False, paper.failure_reason

        errors = []
        for name, fn in strategies:
            self.limiter.wait()
            try:
                ok, data = fn()
                if ok:
                    paper.downloaded_path = str(target)
                    paper.download_source = name
                    paper.download_detail = f"{name}: {data}"
                    return True, paper.download_detail
                errors.append(f"{name}: {data}")
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:80]}")
        paper.failure_reason = "; ".join(errors)[:600]
        paper.download_source = ""
        paper.download_detail = paper.failure_reason
        return False, paper.failure_reason
