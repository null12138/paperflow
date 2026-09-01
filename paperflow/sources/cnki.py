"""CNKI 数据源：Playwright + 用户主动保存的站点会话。

使用前可运行 ``paperflow auth login cnki`` 保存校园机构登录态。检索器不读取
系统浏览器资料，也不绕过验证码；登录失效或出现安全验证时会停止并提示用户处理。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable

from .. import auth, net
from ..models import Paper, author_names, clean_text, normalize_doi
from . import SOURCES


SEARCH_URL = "https://oversea.cnki.net/kns8s/defaultresult/index"
RESULT_ROWS_JS = """() => {
  const rows = [];
  for (const tr of document.querySelectorAll('tbody tr')) {
    const cells = [...tr.querySelectorAll('td')];
    if (cells.length < 6) continue;
    const titleCell = cells[1];
    const link = titleCell.querySelector('a[href]');
    const title = (link?.innerText || titleCell.innerText || '').trim();
    if (!title) continue;
    rows.push({
      title,
      url: link?.href || '',
      authors: (cells[2]?.innerText || '').trim(),
      journal: (cells[3]?.innerText || '').trim(),
      date: (cells[4]?.innerText || '').trim(),
      database: (cells[5]?.innerText || '').trim()
    });
  }
  return rows;
}"""
DETAIL_JS = """() => {
  const meta = name => document.querySelector(`meta[name="${name}"]`)?.content || '';
  const text = selectors => {
    for (const selector of selectors) {
      const element = document.querySelector(selector);
      if (element?.innerText?.trim()) return element.innerText.trim();
    }
    return '';
  };
  const pdfLinks = [...document.querySelectorAll('a[href]')]
    .filter(element => (element.innerText || '').trim() === 'PDF Download');
  const visiblePdf = pdfLinks.find(element => {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  }) || pdfLinks[0];
  return {
    abstract: meta('citation_abstract') || text([
      '#ChDivSummary', '#abstract_text', '.abstract-text', '.row-abstract',
      '.brief .abstract', '[class*=abstract] .text', '[class*=abstract]'
    ]),
    doi: meta('citation_doi') || meta('DC.Identifier'),
    journal: meta('citation_journal_title'),
    date: meta('citation_publication_date') || meta('citation_date'),
    pdf_url: visiblePdf?.href || '',
    authors: [...document.querySelectorAll('meta[name="citation_author"]')]
      .map(element => element.content).filter(Boolean)
  };
}"""


class CnkiError(RuntimeError):
    """CNKI 登录、验证或页面结构错误。"""


def _cookie_for_playwright(cookie: dict[str, Any]) -> dict[str, Any] | None:
    if not cookie.get("name") or cookie.get("value") is None:
        return None
    allowed = {"name", "value", "url", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
    normalized = {key: value for key, value in cookie.items() if key in allowed}
    if "url" not in normalized and "domain" not in normalized:
        return None
    if "domain" in normalized:
        normalized["path"] = normalized.get("path", "/")
    return normalized


def _split_authors(value: str) -> list[str]:
    return author_names(item for item in re.split(r"[;；、]+", value or "") if item.strip())


class CnkiSource:
    name = "CNKI"

    def __init__(
        self,
        headless: bool | None = None,
        fetch_abstracts: bool | None = None,
        browser_factory: Callable[[bool], tuple[Any, Any, Any]] | None = None,
    ) -> None:
        self.headless = (
            os.getenv("CNKI_HEADLESS", "0").strip().lower() in {"1", "true", "yes"}
            if headless is None else headless
        )
        self.fetch_abstracts = (
            os.getenv("CNKI_FETCH_ABSTRACTS", "1").strip().lower() not in {"0", "false", "no"}
            if fetch_abstracts is None else fetch_abstracts
        )
        self.browser_factory = browser_factory or auth._launch_browser
        self.detail_limiter = net.RateLimiter(max_per_minute=60)

    def _launch(self):
        playwright, browser, context = self.browser_factory(not self.headless)
        session = auth.load_site_session("cnki")
        cookies = [item for item in (_cookie_for_playwright(c) for c in session.get("cookies", [])) if item]
        if cookies:
            context.add_cookies(cookies)
        storage = session.get("storage") or {}
        if storage:
            payload = json.dumps(storage, ensure_ascii=False)
            context.add_init_script(
                f"""(() => {{
                  const values = {payload};
                  try {{ for (const [key, value] of Object.entries(values)) localStorage.setItem(key, value); }}
                  catch (_) {{}}
                }})();"""
            )
        return playwright, browser, context

    @staticmethod
    def _page_problem(body_text: str) -> str:
        text = clean_text(body_text).casefold()
        if any(marker in text for marker in ("验证码", "安全验证", "访问过于频繁", "verify you are human", "captcha")):
            return "CNKI 触发安全验证；请在可见浏览器中完成验证后重试，程序不会绕过验证码"
        if any(marker in text for marker in ("机构登录", "登录后查看", "请登录", "用户登录")):
            return "CNKI 会话未登录或已失效；请运行: paperflow auth login cnki"
        return ""

    @staticmethod
    def _wait_rows(page, timeout_ms: int = 45000) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            rows = page.evaluate(RESULT_ROWS_JS) or []
            if rows:
                return rows
            body = page.locator("body").inner_text(timeout=5000)
            problem = CnkiSource._page_problem(body)
            if problem:
                raise CnkiError(problem)
            if any(marker in body for marker in ("暂无数据", "未检索到", "没有找到相关结果")):
                return []
            time.sleep(1)
        body = page.locator("body").inner_text(timeout=5000)
        problem = CnkiSource._page_problem(body)
        if problem:
            raise CnkiError(problem)
        raise CnkiError("CNKI 结果页等待超时，页面结构可能已变化")

    @staticmethod
    def _next_page(page, previous_title: str) -> bool:
        clicked = page.evaluate("""() => {
          const candidates = [document.getElementById('PageNext'), ...document.querySelectorAll('a, button')]
            .filter(Boolean);
          const next = candidates.find(element => {
            const label = (element.innerText || element.getAttribute('aria-label') || '').trim();
            return element.id === 'PageNext' || /^(下一页|下页|next)$/i.test(label);
          });
          if (!next || next.classList.contains('disabled') || next.getAttribute('aria-disabled') === 'true') return false;
          next.click();
          return true;
        }""")
        if not clicked:
            return False
        try:
            page.wait_for_function(
                """previous => {
                  const cells = [...document.querySelectorAll('tbody tr td:nth-child(2)')];
                  return cells.length && (cells[0].innerText || '').trim() !== previous;
                }""",
                arg=previous_title,
                timeout=15000,
            )
        except Exception:
            return False
        time.sleep(2)
        return True

    def _fetch_detail(self, context, row: dict[str, Any]) -> None:
        url = row.get("url") or ""
        if not url:
            return
        self.detail_limiter.wait()
        page = context.new_page()
        try:
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            detail = page.evaluate(DETAIL_JS) or {}
            problem = self._page_problem(page.locator("body").inner_text(timeout=5000))
            if problem:
                raise CnkiError(problem)
            for field in ("abstract", "doi", "journal", "date", "pdf_url"):
                if detail.get(field):
                    row[field] = detail[field]
            if detail.get("authors"):
                row["author_list"] = detail["authors"]
        finally:
            page.close()

    @staticmethod
    def _save_refreshed_session(context, page) -> None:
        try:
            storage = page.evaluate(
                "() => { const out={}; for(let i=0;i<localStorage.length;i++){"
                "const key=localStorage.key(i); out[key]=localStorage.getItem(key);} return out; }"
            )
            auth.save_site_session("cnki", context.cookies(), storage, "oversea.cnki.net")
        except Exception:
            pass

    def _search_rows(self, species: str, limit: int) -> list[dict[str, Any]]:
        playwright = browser = context = page = None
        try:
            playwright, browser, context = self._launch()
            page = context.new_page()
            try:
                page.goto(SEARCH_URL, timeout=60000, wait_until="domcontentloaded")
            except Exception as exc:
                message = clean_text(exc)
                if "CERT_" in message or "certificate" in message.casefold():
                    raise CnkiError("CNKI TLS 证书异常，可能是 DNS/代理劫持；请切换网络或关闭异常系统代理") from exc
                raise CnkiError(f"无法打开 CNKI: {message[:160]}") from exc

            try:
                search_input = page.locator("#txt_search")
                search_input.wait_for(state="visible", timeout=30000)
                search_input.fill(species)
                page.locator(".search-btn").click(timeout=10000)
            except Exception as exc:
                problem = self._page_problem(page.locator("body").inner_text(timeout=5000))
                if problem:
                    raise CnkiError(problem) from exc
                raise CnkiError(f"CNKI 搜索框或提交按钮不可用: {clean_text(exc)[:140]}") from exc

            rows: list[dict[str, Any]] = []
            seen: set[str] = set()
            while not limit or len(rows) < limit:
                page_rows = self._wait_rows(page)
                for row in page_rows:
                    key = clean_text(row.get("title")).casefold()
                    if key and key not in seen:
                        seen.add(key)
                        rows.append(row)
                        if limit and len(rows) >= limit:
                            break
                if limit and len(rows) >= limit:
                    break
                previous = clean_text(page_rows[0].get("title")) if page_rows else ""
                if not previous or not self._next_page(page, previous):
                    break

            selected = rows[:limit] if limit else rows
            if self.fetch_abstracts:
                for row in selected:
                    self._fetch_detail(context, row)
            self._save_refreshed_session(context, page)
            return selected
        finally:
            for obj, method in ((page, "close"), (browser, "close"), (playwright, "stop")):
                try:
                    if obj:
                        getattr(obj, method)()
                except Exception:
                    pass

    @staticmethod
    def _to_paper(row: dict[str, Any], species: str) -> Paper:
        authors = row.get("author_list") or _split_authors(row.get("authors") or "")
        date = clean_text(row.get("date"))
        year_match = re.search(r"(?:19|20)\d{2}", date)
        paper = Paper(
            title=clean_text(row.get("title")),
            abstract=clean_text(row.get("abstract")),
            authors=author_names(authors),
            journal=clean_text(row.get("journal")),
            year=year_match.group(0) if year_match else "",
            doi=normalize_doi(row.get("doi") or ""),
            sources={"CNKI"},
            species={species},
        )
        # CNKI 下载必须从详情页点击可见的 PDF Download，不能直接请求下载 URL。
        if row.get("pdf_url") and row.get("url"):
            paper.add_candidate(clean_text(row["url"]), "cnki", priority=1)
        return paper

    def search_species(self, client, species: str, limit: int) -> list[Paper]:
        rows = self._search_rows(species, limit)
        papers = [self._to_paper(row, species) for row in rows]
        return [paper for paper in papers if paper.title]

    def search_doi(self, client, doi: str) -> list[Paper]:
        return []


SOURCES.register(CnkiSource())
