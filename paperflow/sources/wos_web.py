"""WOS 网页数据源（Edge/WebBridge 优先 + Playwright 备选）。

- 主路径：Kimi WebBridge 控制用户真实 Edge（机构会话已配置，无需再登录）
  新版 WOS(Nextgen)：检索框 aria-label="Search box 1 Topic..." 存在；
  提交用「回车」而非 Search 按钮（新版按钮无可见文本）。
- 备选：Playwright 自弹 Chromium（需窗口可见/人工登录）。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request

from ..models import Paper, author_names, clean_text, normalize_doi
from . import SOURCES

BRIDGE = "http://127.0.0.1:10086/command"
SESSION = "wos"
BRIDGE_URL = "https://webofscience.clarivate.cn/wos/woscc/basic-search"
STEP = 2.0


def _bridge(action: str, args: dict | None = None, retries: int = 3) -> dict:
    body = json.dumps({"action": action, "args": args or {}, "session": SESSION}).encode()
    req = urllib.request.Request(BRIDGE, data=body, headers={"Content-Type": "application/json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code != 502 or attempt == retries - 1:
                raise
            import subprocess
            from pathlib import Path as _P
            subprocess.run([str(_P.home() / ".kimi-webbridge/bin/kimi-webbridge"), "restart"],
                           capture_output=True, timeout=30)
            time.sleep(4)


FILL_ENTER_TMPL = """(() => {
  const i = document.querySelector('input[aria-label^="Search box 1 Topic"]');
  if (!i) return 'NO-INPUT';
  const p = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
  p.set.call(i, {species});
  i.dispatchEvent(new Event('input', {{bubbles: true}}));
  i.dispatchEvent(new Event('change', {{bubbles: true}}));
  i.focus();
  i.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', code: 'Enter', bubbles: true}}));
  return 'OK';
}})()"""

ROWS_JS = """(() => JSON.stringify([...document.querySelectorAll(
  '#records-holder tbody tr, tr.record, .search-results-row, [class*=search-results] tr')]
  .map(r => r.innerText.replace(/\s+/g, ' ¦ ')).slice(0, 100)))()"""
class WosWebSource:
    name = "WOS"

    def __init__(self) -> None:
        self._p = None
        self._browser = None
        self._ctx = None

    # ---------- 会话 ----------
    def _save_session(self, page) -> None:
        try:
            cookies = [c for c in self._ctx.cookies() if c["name"] and c.get("value")]
            storage = page.evaluate(
                "() => { const o={}; for (let i=0;i<localStorage.length;i++){"
                "const k=localStorage.key(i); o[k]=localStorage.getItem(k);} return o; }")
            auth.save_site_session("wos", cookies, storage, "webofscience.clarivate.cn")
        except Exception:
            pass

    def _ensure_browser(self):
        if self._browser and self._ctx:
            return
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
        self._p = sync_playwright().start()
        self._browser = self._p.chromium.launch(
            channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-first-run"])
        self._ctx = self._browser.new_context(
            user_agent=UA, viewport={"width": 1440, "height": 900}, locale="zh-CN")
        Stealth().apply_stealth_sync(self._ctx)
        cookies = auth.load_site_cookies("wos")
        if cookies:
            try:
                norm = []
                for c in cookies:
                    n = {k: c[k] for k in ("name", "value", "domain", "path") if k in c}
                    n["domain"] = n["domain"] if n["domain"].startswith(".") else "." + n["domain"]
                    n["path"] = n.get("path", "/")
                    norm.append(n)
                self._ctx.add_cookies(norm)
            except Exception:
                pass

    def _close(self):
        try:
            self._browser.close()
        except Exception:
            pass
        try:
            self._p.stop()
        except Exception:
            pass
        self._browser = None

    # ---------- 检索流程 ----------
    def search_species(self, client, species: str, limit: int) -> list[Paper]:
        try:
            self._ensure_browser()
            page = self._ctx.new_page()
            page.goto(SEARCH_URL, timeout=90000, wait_until="domcontentloaded")

            def has_box():
                return page.evaluate(
                    "!!document.querySelector(`input[aria-label^=\"Search box 1 Topic\"]`)")

            def has_login_modal():
                try:
                    return page.evaluate(
                        "!!document.querySelector('cdx-login-modal, [class*=login-ck-modal], "
                        "mat-dialog-container')")
                except Exception:
                    return False

            # —— 阶段1：登录（若弹窗出现则引导用户）——
            logged_in = has_box()
            if not logged_in:
                if has_login_modal():
                    print("  WOS: 检测到登录弹窗。请在浏览器窗口中点击", flush=True)
                    print("  WOS: 「登录 / 机构登录 / Log in via your institution」，", flush=True)
                    print("  WOS: 选择『首都师范大学 / Capital Normal University』完成 SSO。", flush=True)
                else:
                    print("  WOS: 浏览器窗口已打开（若要求登录请完成登录）…", flush=True)
                for _ in range(90):  # 最多 3 分钟等登录
                    time.sleep(STEP)
                    try:
                        if has_box():
                            logged_in = True
                            break
                    except Exception:
                        pass
                if logged_in:
                    self._save_session(page)
                    print("  WOS: ✅ 登录成功，会话已缓存 → sessions/wos.json", flush=True)
                else:
                    print("  WOS: ⏰ 等待登录超时（3 分钟）。可再运行一次。", flush=True)
                    try:
                        page.close()
                    except Exception:
                        pass
                    return []

            # —— 阶段2：填检索词 + 回车提交 ——
            page.evaluate(
                "({species}) => { const i=document.querySelector(`input[aria-label^=\"Search box 1 Topic\"]`);"
                "const p=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');"
                "p.set.call(i, species); i.dispatchEvent(new Event('input',{bubbles:true}));"
                "i.dispatchEvent(new Event('change',{bubbles:true})); }", species)
            time.sleep(0.5)
            page.keyboard.press("Enter")
            print(f"  WOS: 已提交检索 '{species}'，等待结果…", flush=True)

            # —— 阶段3：等结果页 ——
            for _ in range(60):
                time.sleep(STEP)
                try:
                    if "/summary/" in page.url:
                        self._save_session(page)
                        break
                except Exception:
                    pass
            # —— 阶段4：抓结果 ——
            rows = []
            for _ in range(40):
                time.sleep(STEP)
                try:
                    val = page.evaluate(
                        "() => JSON.stringify([...document.querySelectorAll("
                        "'#records-holder tbody tr, tr.record, .search-results-row, [class*=search-results] tr')]"
                        ".map(r=>r.innerText.replace(/\\s+/g,' ¦ ')).slice(0,100))")
                    rows = json.loads(val) if isinstance(val, str) else (val or [])
                except Exception:
                    rows = []
                if rows and rows[0].strip():
                    self._save_session(page)
                    break
                # 兜底：整页文本（新版 WOS 结果可能不用 tr）
                try:
                    text = page.evaluate("document.querySelector('.search-results, #records-holder, main')?.innerText?.slice(0,60) || ''")
                    if text.strip():
                        rows = ["PAGE:" + text]
                        break
                except Exception:
                    pass
            try:
                page.close()
            except Exception:
                pass
            papers = self._parse_wos_rows(rows)
            print(f"  WOS: 获得 {len(papers)} 条结果", flush=True)
            return papers
        finally:
            self._close()

    @staticmethod
    def _parse_wos_rows(rows: list[str]) -> list[Paper]:
        papers: list[Paper] = []
        seen = set()
        for text in rows:
            cells = [c.strip() for c in text.split("¦") if c.strip()]
            if not cells:
                continue
            title = cells[1] if len(cells) > 1 else cells[0]
            key = title.casefold()[:80]
            if not key or key in seen:
                continue
            seen.add(key)
            doi = next((m.group(1).rstrip(".") for c in cells
                        for m in [re.search(r"(10\.\d{4,}/[^\s¦]+)", c)] if m), "")
            year = next((m.group(1) for c in cells
                         for m in [re.search(r"\b(19\d{2}|20\d{2})\b", c)] if m), "")
            papers.append(Paper(
                title=clean_text(title), year=year,
                journal=clean_text(cells[3] if len(cells) >= 4 else ""),
                authors=author_names([cells[2]] if len(cells) >= 3 else []),
                doi=normalize_doi(doi), sources={"WOS"},
            ))
        return papers





class WosBridgeSource:
    """WOS via 真实 Edge（Kimi WebBridge）：机构会话已就绪，检索用回车提交。"""

    name = "WOS"

    def _parse_rows(self, rows):
        papers = []
        seen = set()
        for text in rows:
            cells = [c.strip() for c in text.split("¦") if c.strip()]
            if not cells:
                continue
            title = cells[1] if len(cells) > 1 else cells[0]
            key = title.casefold()[:80]
            if not key or key in seen:
                continue
            seen.add(key)
            doi = next((m.group(1).rstrip(".") for c in cells
                        for m in [re.search(r"(10\.\d{4,}/[^\s¦]+)", c)] if m), "")
            year = next((m.group(1) for c in cells
                         for m in [re.search(r"\b(19\d{2}|20\d{2})\b", c)] if m), "")
            papers.append(Paper(
                title=clean_text(title), year=year,
                journal=clean_text(cells[3] if len(cells) >= 4 else ""),
                authors=author_names([cells[2]] if len(cells) >= 3 else []),
                doi=normalize_doi(doi), sources={"WOS"},
            ))
        return papers

    def search_species(self, client, species: str, limit: int) -> list[Paper]:
        try:
            _bridge("navigate", {"url": BRIDGE_URL, "newTab": True, "group_title": "wos"})
        except Exception:
            try:
                _bridge("navigate", {"url": BRIDGE_URL, "newTab": True, "group_title": "wos"})
            except Exception:
                raise RuntimeError("无法打开 Edge 中 WOS（检查 Kimi WebBridge 是否连接）")
        # 等检索框出现（新版 Nextgen；机构会话就绪通常秒开）
        for _ in range(40):
            time.sleep(STEP)
            try:
                has = _bridge("evaluate", {"code":
                    "(() => !!document.querySelector(`input[aria-label^=\"Search box 1 Topic\"]`))()"})
                if (has.get("data") or {}).get("value"):
                    break
            except Exception:
                pass
        # 填词 + 回车提交
        fill_js = FILL_ENTER_TMPL.replace("{species}", json.dumps(species))
        try:
            _bridge("evaluate", {"code": fill_js})
        except Exception:
            pass
        # 等结果页
        for _ in range(60):
            time.sleep(STEP)
            try:
                r = _bridge("evaluate", {"code": "(() => location.href)()"})
                url = str((r.get("data") or {}).get("value") or "")
                if "/summary/" in url:
                    break
            except Exception:
                pass
        # 抓结果
        rows = []
        for _ in range(40):
            time.sleep(STEP)
            try:
                r = _bridge("evaluate", {"code": ROWS_JS})
                val = (r.get("data") or {}).get("value")
                rows = json.loads(val) if isinstance(val, str) else (val or [])
            except Exception:
                rows = []
            if rows and rows[0].strip():
                break
        try:
            _bridge("close_tab")
        except Exception:
            pass
        papers = self._parse_rows(rows)
        print(f"  WOS(Edge): {len(papers)} 条", flush=True)
        return papers


SOURCES.register(WosBridgeSource())
