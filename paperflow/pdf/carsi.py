"""CARSI 高校身份认证通道：校外免 VPN，用学校 CAS 统一认证直接登录出版社。

借鉴 https://github.com/Rimagination/scansci-pdf （Apache-2.0）的 CARSI
（Shibboleth/SAML 联邦认证）实现思路，按 paperflow 架构重写：

  1. 打开文章页（让 Cloudflare 放行），检测是否需要机构登录；
  2. 从页面找 "Access through your institution" SSO 链接，进入
     WAYF 机构选择页，按学校名检索并点击匹配机构；
  3. 跳转学校 CAS 统一认证 —— 用户在浏览器里输入学号/密码（工具不碰密码）；
  4. 认证回跳成功后保存 publisher 会话 cookie 到 sessions/carsi_<publisher>.json；
  5. 经授权会话抓取出版社 PDF 直链（网络响应捕获 / 直接构造 / 点击 PDF 按钮），
     校验 %PDF- 头后落盘。

支持出版社：wiley / acs / sciencedirect / springer / nature / tandfonline /
ieee / oxford / royalsociety / sage / asce。
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
from pathlib import Path

import requests

log = logging.getLogger(__name__)
SESSIONS_DIR = Path(__file__).resolve().parent.parent / "sessions"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

# 学校中文名 → WAYF 搜索用英文名（更易命中；未收录的学校直接用中文名搜索）
IDP_MAP = {
    "清华大学": "Tsinghua", "北京大学": "Peking", "浙江大学": "Zhejiang",
    "复旦大学": "Fudan", "上海交通大学": "Shanghai Jiao Tong",
    "南京大学": "Nanjing", "中国科学技术大学": "USTC",
    "武汉大学": "Wuhan", "中山大学": "Sun Yat-sen",
    "华中科技大学": "Huazhong", "哈尔滨工业大学": "Harbin Institute",
    "西安交通大学": "Xi'an Jiaotong", "同济大学": "Tongji",
    "北京师范大学": "Beijing Normal", "南开大学": "Nankai",
    "四川大学": "Sichuan", "天津大学": "Tianjin",
    "中国人民大学": "Renmin", "北京航空航天大学": "Beihang",
    "山东大学": "Shandong", "吉林大学": "Jilin",
    "首都师范大学": "Capital Normal",
}

# 登录页特征：URL 关键词 / 标题关键词
_AUTH_URL_KEYWORDS = ("cas", "idp", "saml", "wayf", "sso", "passport",
                      "accounts", "oauth", "/login", "/signin")
_AUTH_TITLES = ("登录", "身份", "二次认证", "Login", "Sign in", "Log in")

_INSTITUTION_SEARCH_SELECTORS = [
    "#searchInstitution", "#bdd-email", 'input[name="institution"]',
    "#institution-search", '#idp-search', "input[name='idpSearch']",
    'input[placeholder*="institution"]', 'input[placeholder*="University"]',
    'input[placeholder*="Type the name"]', "input[name='query']",
]

_SSO_LINK_FINDER_JS = (
    "() => {"
    "  const links = [...document.querySelectorAll('a')];"
    "  const sso = links.find(a => a.href &&"
    "    (a.href.includes('ssostart') || a.href.includes('shibboleth')"
    "     || a.href.includes('saml') || a.href.includes('institutional-login')"
    "     || a.href.includes('federation') || a.href.includes('/action/showLogin')"
    "     || a.href.includes('/institutional-access') || a.href.includes('wayf')));"
    "  if (sso) { return sso.href; }"
    "  return false;"
    "}"
)

_INSTITUTION_CLICK_JS = (
    "(name) => {"
    "  const items = document.querySelectorAll("
    "    '[class*=\"result\"], [class*=\"suggestion\"], [class*=\"federation\"], li, a, button');"
    "  for (const el of items) {"
    "    const text = el.textContent || '';"
    "    if (text.includes(name) && el.offsetParent !== null) {"
    "      el.click();"
    "      return text.trim().substring(0, 60);"
    "    }"
    "  }"
    "  return null;"
    "}"
)

# 每家出版社：找 SSO 链接的 href 特征 + 可用 PDF 路径模板（用 {doi}/{suffix}）
PUBLISHER_SSO = {
    "wiley": {"sso": ("ssostart",), "pdfs": ("/doi/pdfdirect/{doi}", "/doi/pdf/{doi}")},
    "acs": {"sso": ("shibboleth", "institutional"), "pdfs": ("/doi/pdf/{doi}",)},
    "sciencedirect": {"sso": ("shibboleth", "institutional"),
                      "pdfs": ("/science/article/pii/{suffix}/pdfft",
                               "/doi/pdfdirect/{doi}")},
    "springer": {"sso": ("shibboleth", "institutional-login"),
                 "pdfs": ("/content/pdf/{doi}.pdf",)},
    "nature": {"sso": ("shibboleth", "institutional"),
               "pdfs": ("/articles/{suffix}.pdf",)},
    "tandfonline": {"sso": ("ssostart", "shibboleth", "institutional"),
                    "pdfs": ("/doi/pdf/{doi}?needAccess=true",)},
    "ieee": {"sso": ("shibboleth", "institutional"), "pdfs": ()},
    "oxford": {"sso": ("shibboleth", "institutional"), "pdfs": ()},
    "royalsociety": {"sso": ("shibboleth", "institutional"), "pdfs": ()},
    "sage": {"sso": ("shibboleth", "institutional"), "pdfs": ()},
    "asce": {"sso": ("shibboleth", "institutional"), "pdfs": ()},
}

_HOST_PUBLISHER = [
    ("onlinelibrary.wiley.com", "wiley"), ("pubs.acs.org", "acs"),
    ("sciencedirect.com", "sciencedirect"), ("elsevier.com", "sciencedirect"),
    ("link.springer.com", "springer"), ("springer.com", "springer"),
    ("nature.com", "nature"), ("tandfonline.com", "tandfonline"),
    ("ieeexplore.ieee.org", "ieee"), ("academic.oup.com", "oxford"),
    ("royalsocietypublishing.org", "royalsociety"),
    ("journals.sagepub.com", "sage"), ("ascelibrary.org", "asce"),
]


def detect_publisher(url: str) -> str | None:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    for dom, pub in _HOST_PUBLISHER:
        if dom in host:
            return pub
    return None


def cookie_path(publisher: str) -> Path:
    return SESSIONS_DIR / f"carsi_{publisher}.json"


def load_cookies(publisher: str) -> list[dict]:
    p = cookie_path(publisher)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else data.get("cookies", [])
    except Exception:
        return []


def save_cookies(publisher: str, cookies: list[dict]) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    cookie_path(publisher).write_text(json.dumps(cookies, ensure_ascii=False),
                                      encoding="utf-8")


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


class CarsiEngine:
    def __init__(self, idp_name: str = ""):
        self.idp_name = idp_name  # 学校中文名，如 首都师范大学

    # ------------------------------------------------------------------
    def fetch(self, doi: str, target: Path) -> tuple[bool, str]:
        resolved = _resolve_doi(doi) or f"https://doi.org/{doi}"
        publisher = detect_publisher(resolved)
        if not publisher:
            return False, f"CARSI: 无法识别出版社 ({resolved[:60]})"
        if not self.idp_name:
            return False, ("CARSI: 未指定学校（--idp 首都师范大学 / "
                           "auth login carsi --school 首都师范大学）")
        try:
            return self._browser(doi, resolved, publisher, target)
        except Exception as exc:
            return False, f"CARSI: {type(exc).__name__}: {str(exc)[:120]}"

    # ------------------------------------------------------------------
    def _browser(self, doi: str, article_url: str, publisher: str,
                 target: Path) -> tuple[bool, str]:
        from playwright.sync_api import sync_playwright

        cfg = PUBLISHER_SSO[publisher]
        saved = load_cookies(publisher)
        captured: list[bytes] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, args=[
                "--disable-blink-features=AutomationControlled", "--no-first-run"])
            ctx = browser.new_context(user_agent=UA, locale="zh-CN",
                                      viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            try:
                if saved:
                    try:
                        pw = [{"name": c["name"], "value": c["value"],
                               "domain": c.get("domain", ""), "path": c.get("path", "/")}
                              for c in saved if c.get("domain")]
                        if pw:
                            ctx.add_cookies(pw)
                    except Exception:
                        pass

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
                        if len(body) > 5000 and body[:4] == b"%PDF-":
                            captured.append(body)
                    except Exception:
                        pass

                page.on("response", on_response)

                # 1) 打开文章页（等 Cloudflare 自动放行）
                page.goto(article_url, wait_until="domcontentloaded", timeout=60000)
                time.sleep(5)
                for _ in range(12):
                    if "just a moment" in (page.title() or "").lower():
                        time.sleep(5)
                    else:
                        break

                # 2) 判断是否需要机构登录（付费墙检测）
                needs_login = True
                if saved:
                    try:
                        needs_login = page.evaluate(
                            "() => { if(!document.body) return true;"
                            " const b=(document.body.innerText||'').toLowerCase();"
                            " const pay=b.includes('purchase')||b.includes('subscribe')"
                            "||b.includes('access through your institution')"
                            "||b.includes('sign in to access')||b.includes('buy this article');"
                            " const hasPdf=!!document.querySelector("
                            "'a[href*=\"pdf\"],a[href*=\"download\"],iframe[src*=\"pdf\"]');"
                            " return pay && !hasPdf; }")
                    except Exception:
                        needs_login = True

                if needs_login:
                    # 3) 找 SSO 链接 → WAYF → 搜学校 → CAS
                    sso_href = page.evaluate(_SSO_LINK_FINDER_JS)
                    if sso_href:
                        page.goto(sso_href, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(6)

                    input_el = None
                    for sel in _INSTITUTION_SEARCH_SELECTORS:
                        input_el = page.query_selector(sel)
                        if input_el:
                            break
                    idp_en = IDP_MAP.get(self.idp_name, self.idp_name)
                    if input_el:
                        input_el.fill(idp_en)
                        time.sleep(3)
                        clicked = page.evaluate(_INSTITUTION_CLICK_JS, idp_en)
                        if not clicked:
                            input_el.press("Enter")
                            time.sleep(3)

                    # 4) 等待用户在 CAS 页完成登录（最长 5 分钟）
                    url = page.url.lower()
                    title = page.title() or ""
                    if any(x in url for x in _AUTH_URL_KEYWORDS) or any(x in title for x in _AUTH_TITLES):
                        print(f"\n  [CARSI] 请在浏览器中完成 {self.idp_name} 的 CAS 登录"
                              "（学号+密码，工具不接触密码）…")
                        for _ in range(100):
                            time.sleep(3)
                            try:
                                title = page.title() or ""
                                url = page.url.lower()
                            except Exception:
                                break
                            if not any(x in title for x in _AUTH_TITLES) and \
                               not any(x in url for x in _AUTH_URL_KEYWORDS):
                                cookies = ctx.cookies()
                                save_cookies(publisher, cookies)
                                print(f"  [CARSI] 登录成功，已保存 {len(cookies)} cookies")
                                break
                        else:
                            print("  [CARSI] CAS 登录超时")
                            return False, "CAS 登录超时"
                    else:
                        cookies = ctx.cookies()
                        if len(cookies) > 3:
                            save_cookies(publisher, cookies)

                    time.sleep(2)
                    page.goto(article_url, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(5)

                # 5) 取 PDF：捕获响应 → 直链 → 验证
                target.parent.mkdir(parents=True, exist_ok=True)
                saved_f = self._save_captured(captured, target)
                if saved_f:
                    return True, f"carsi({publisher}) 响应捕获 → {target.name}"

                suffix = doi.split("/", 1)[-1] if "/" in doi else doi
                for tpl in cfg.get("pdfs", ()):
                    path = tpl.format(doi=doi, suffix=suffix)
                    if not path.startswith("http"):
                        host = urllib.parse.urlparse(article_url).netloc
                        url = f"https://{host}{path}"
                    else:
                        url = path
                    captured.clear()
                    try:
                        page.goto(url, wait_until="commit", timeout=30000)
                        time.sleep(5)
                    except Exception:
                        continue
                    if self._save_captured(captured, target):
                        return True, f"carsi({publisher}) {url[:60]}"

                # 6) 页面找 PDF 链接 / 点击 PDF 按钮
                html = page.content()
                found = self._find_pdf_link(html, page.url)
                if found:
                    captured.clear()
                    try:
                        page.goto(found, wait_until="commit", timeout=30000)
                        time.sleep(5)
                    except Exception:
                        pass
                    if self._save_captured(captured, target):
                        return True, f"carsi({publisher}) {found[:60]}"

                click_ok = page.evaluate(
                    "() => { const links=[...document.querySelectorAll('a')];"
                    " for(const a of links){ const h=(a.href||'').toLowerCase();"
                    " const t=(a.innerText||'').toLowerCase();"
                    " if((h.includes('pdf')||h.includes('download'))&&!h.includes('supplement')"
                    " &&(t.includes('pdf')||t.includes('download'))){ a.click(); return a.href; }"
                    " } return null; }")
                if click_ok:
                    time.sleep(8)
                    if self._save_captured(captured, target):
                        return True, f"carsi({publisher}) 点击 PDF 按钮"

                return False, "CARSI: 页面未出现 PDF（检查机构是否有该刊订阅权限）"
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    def _save_captured(self, captured: list[bytes], target: Path) -> bool:
        if captured and captured[-1][:4] == b"%PDF-":
            target.write_bytes(captured[-1])
            return True
        return False

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
            return meta["content"] if meta["content"].startswith("http") else base + meta["content"]
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True).lower()
            classes = " ".join(a.get("class", []))
            if any(k in text for k in ("pdf", "download pdf", "full text pdf", "view pdf")):
                return href if href.startswith("http") else base + href
            if "/doi/pdf/" in href or "/doi/pdfdirect/" in href or href.endswith(".pdf"):
                return href if href.startswith("http") else base + href
        return None


def carsi_login(publisher: str, school: str) -> bool:
    """独立 CARSI 登录步骤：抓到一个目标 URL 即触发完整登录流程。"""
    resolved = _resolve_doi("10.1038/nature12373") or "https://www.nature.com"
    engine = CarsiEngine(idp_name=school)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, args=[
                "--disable-blink-features=AutomationControlled", "--no-first-run"])
            ctx = browser.new_context(user_agent=UA, locale="zh-CN",
                                      viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            saved = load_cookies(publisher)
            if saved:
                pw = [{"name": c["name"], "value": c["value"],
                       "domain": c.get("domain", ""), "path": c.get("path", "/")}
                      for c in saved if c.get("domain")]
                try:
                    if pw:
                        ctx.add_cookies(pw)
                except Exception:
                    pass
            page.goto(resolved, wait_until="domcontentloaded", timeout=60000)
            time.sleep(5)
            sso = page.evaluate(_SSO_LINK_FINDER_JS)
            if sso:
                page.goto(sso, wait_until="domcontentloaded", timeout=30000)
            time.sleep(6)
            for sel in _INSTITUTION_SEARCH_SELECTORS:
                el = page.query_selector(sel)
                if el:
                    idp_en = IDP_MAP.get(school, school)
                    el.fill(idp_en)
                    time.sleep(3)
                    page.evaluate(_INSTITUTION_CLICK_JS, idp_en)
                    break
            print(f"\n  [CARSI] 已打开 {school} 的机构认证页，请在浏览器完成 CAS 登录…")
            ok = False
            for _ in range(100):
                time.sleep(3)
                try:
                    title = page.title() or ""
                    url = page.url.lower()
                except Exception:
                    break
                if not any(x in title for x in _AUTH_TITLES) and \
                   not any(x in url for x in _AUTH_URL_KEYWORDS):
                    cookies = ctx.cookies()
                    save_cookies(publisher, cookies)
                    print(f"  ✓ CARSI 登录成功，已保存 {len(cookies)} cookies")
                    ok = True
                    break
            browser.close()
            return ok
    except Exception as exc:
        print(f"  CARSI 登录失败: {exc}")
        return False