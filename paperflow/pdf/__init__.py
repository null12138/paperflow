"""PDF 下载引擎：支持 CNKI 授权、Sci-Hub、开放获取、出版社订阅与本地复用。"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import requests

from .. import net
from ..models import Paper
from .scihub import SciHubEngine
from .oa import OaEngine
from .publisher import PublisherEngine


def safe_slug(title: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", title).strip("._")[:160] or "paper"


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
        use_cnki: bool = False,
    ) -> None:
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.email = email
        self.proxies = proxies or net.DEFAULT_PROXIES
        self.limiter = net.RateLimiter(max_per_minute)
        self.cookie_file = cookie_file
        self.scihub = SciHubEngine(proxies=self.proxies, cookie_file=cookie_file) if use_scihub else None
        self.oa = OaEngine(email=email, proxies=self.proxies) if use_oa else None
        self.use_direct_candidates = use_oa
        self.publisher = PublisherEngine(proxies=self.proxies) if use_publisher else None
        if use_cnki:
            from .cnki import CnkiPdfEngine
            self.cnki = CnkiPdfEngine()
        else:
            self.cnki = None

    def _target_path(self, paper: Paper) -> Path:
        return self.out / (safe_slug(paper.title or paper.doi) + ".pdf")

    def _fetch_direct_candidate(self, url: str, target: Path) -> tuple[bool, str]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False, "候选 URL 不是有效的 HTTP(S) 地址"
        partial = target.with_suffix(target.suffix + ".part")
        partial.unlink(missing_ok=True)
        last_error = ""
        try:
            for proxy in self.proxies:
                try:
                    session = net.make_session(proxy, email=self.email)
                    response = session.get(url, timeout=30, allow_redirects=True, stream=True)
                    response.raise_for_status()
                    with partial.open("wb") as handle:
                        for chunk in response.iter_content(chunk_size=1024 * 128):
                            if chunk:
                                handle.write(chunk)
                    if pdf_ok(partial):
                        partial.replace(target)
                        return True, f"直接候选 → {response.url[:120]}"
                    last_error = "候选返回内容不是 PDF"
                except requests.RequestException as exc:
                    last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
                finally:
                    partial.unlink(missing_ok=True)
            return False, last_error or "直接候选下载失败"
        finally:
            partial.unlink(missing_ok=True)

    def fetch(self, paper: Paper) -> tuple[bool, str]:
        """下载单篇论文全文；返回 (成功?, 说明)。"""
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
                if name != "cnki":
                    strategies.append((name, lambda url=candidate.url: self._fetch_direct_candidate(url, target)))
        # 既有三种策略都以 DOI 为入口；无 DOI 的 CNKI 论文不应向它们传空字符串。
        if paper.doi:
            if self.scihub:
                strategies.append(("scihub", lambda: self.scihub.fetch(paper.doi, target)))
            if self.oa:
                strategies.append(("oa", lambda: self.oa.fetch(paper.doi, target)))
            if self.publisher:
                strategies.append(("publisher", lambda: self.publisher.fetch(paper.doi, target)))

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
