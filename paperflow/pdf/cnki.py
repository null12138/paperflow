"""使用用户主动保存的 CNKI 机构会话下载授权 PDF。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from .. import auth, net
from ..models import Paper, clean_text
from ..sources.cnki import CnkiSource, _cookie_for_playwright
from . import pdf_ok


class CnkiPdfEngine:
    """打开 CNKI 详情页，点击可见下载按钮并捕获 Playwright 下载事件。"""

    def __init__(
        self,
        headless: bool | None = None,
        browser_factory: Callable[[bool], tuple[Any, Any, Any]] | None = None,
    ) -> None:
        self.headless = (
            os.getenv("CNKI_HEADLESS", "0").strip().lower() in {"1", "true", "yes"}
            if headless is None else headless
        )
        self.browser_factory = browser_factory or auth._launch_browser
        self.limiter = net.RateLimiter(max_per_minute=60)

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
    def _visible_pdf_link(page):
        """规避 CNKI 页面重复 id，只选择真正可见的 PDF Download。"""
        links = page.get_by_text("PDF Download", exact=True)
        for index in range(links.count()):
            link = links.nth(index)
            if link.is_visible():
                return link
        return None

    @staticmethod
    def _pdf_href(page) -> str:
        """读取当前详情页实时生成的授权链接；该链接不能跨会话缓存。"""
        return clean_text(page.evaluate("""() => [...document.querySelectorAll('a[href]')]
          .find(element => (element.innerText || '').trim() === 'PDF Download')?.href || ''"""))

    @staticmethod
    def _download_problem(body_text: str) -> str:
        problem = CnkiSource._page_problem(body_text)
        if problem:
            return problem
        text = clean_text(body_text).casefold()
        if any(marker in text for marker in ("无下载权限", "购买后下载", "立即购买", "支付后下载")):
            return "当前 CNKI 机构账号没有这篇文章的 PDF 下载权限"
        return ""

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

    def fetch(self, paper: Paper, target: Path) -> tuple[bool, str]:
        candidates = [candidate for candidate in paper.pdf_candidates if candidate.source.casefold() == "cnki"]
        if not candidates:
            return False, "没有 CNKI PDF 下载候选；请先用 CNKI 检索并保留详情信息"

        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(target.suffix + ".part")
        playwright = browser = context = page = None
        errors: list[str] = []
        try:
            playwright, browser, context = self._launch()
            page = context.new_page()
            for candidate in candidates:
                self.limiter.wait()
                try:
                    page.goto(candidate.url, timeout=60000, wait_until="domcontentloaded")
                    body = page.locator("body").inner_text(timeout=5000)
                    problem = self._download_problem(body)
                    if problem:
                        errors.append(problem)
                        continue
                    link = self._visible_pdf_link(page)
                    href = self._pdf_href(page)
                    if link is None and not href:
                        errors.append("详情页没有 PDF Download，可能无权限或页面结构已变化")
                        continue
                    if partial.exists():
                        partial.unlink()
                    with page.expect_download(timeout=60000) as download_info:
                        if link is not None:
                            link.click(force=True, no_wait_after=True, timeout=15000)
                        else:
                            try:
                                page.goto(href, timeout=60000, wait_until="commit")
                            except Exception as exc:
                                message = clean_text(exc)
                                # Playwright 可能用 ERR_ABORTED 表示导航已转为浏览器下载。
                                if "ERR_ABORTED" not in message and "Download is starting" not in message:
                                    raise
                    download = download_info.value
                    failure = download.failure()
                    if failure:
                        errors.append(f"CNKI 下载失败: {clean_text(failure)[:160]}")
                        continue
                    download.save_as(str(partial))
                    if not pdf_ok(partial):
                        partial.unlink(missing_ok=True)
                        errors.append("CNKI 返回的内容不是有效 PDF，可能是登录页、提示页或 CAJ 文件")
                        continue
                    partial.replace(target)
                    self._save_refreshed_session(context, page)
                    return True, f"CNKI 授权下载: {download.suggested_filename or target.name}"
                except Exception as exc:
                    partial.unlink(missing_ok=True)
                    message = clean_text(exc)
                    if any(marker in message for marker in ("ERR_CONNECTION_CLOSED", "SSL_ERROR", "TLS")):
                        errors.append(
                            "CNKI 下载服务器 o.oversea.cnki.net 连接被关闭；请检查本机代理/DNS分流，"
                            "将该官方域名设为可用线路后重试"
                        )
                    else:
                        errors.append(f"{type(exc).__name__}: {message[:180]}")
            return False, "; ".join(errors)[:600] or "CNKI PDF 下载失败"
        finally:
            partial.unlink(missing_ok=True)
            for obj, method in ((page, "close"), (browser, "close"), (playwright, "stop")):
                try:
                    if obj:
                        getattr(obj, method)()
                except Exception:
                    pass
