"""WebVPN 机构通道：通过高校 WebVPN + CAS 认证下载出版社全文。

借鉴 https://github.com/Rimagination/scansci-pdf （Apache-2.0）的 WebVPN
实现思路，按 paperflow 的架构重写：

  * ``convert_url``：AES-128-CFB 加密目标 hostname，生成 WebVPN 转发 URL；
  * ``login``：弹出可见浏览器完成机构 CAS/SSO 登录，自动检测登录态并保存
    会话到 ``sessions/webvpn.json``；
  * ``fetch``：复用会话 cookie，经 WebVPN 抓取出版社 PDF 直链；失败时用
    浏览器兜底（监听网络响应 / 触发下载 / 提取内嵌 PDF）。

会话文件格式（sessions/webvpn.json）：
  {"school": "...", "host": "https://webvpn.xxx.edu.cn",
   "key": "...", "iv": "...", "cookies": [...], "storage": {}}
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import time
import urllib.parse
from pathlib import Path

import requests

from ..schools import DEFAULT_KEY, get_school, search_schools

log = logging.getLogger(__name__)
SESSIONS_DIR = Path(__file__).resolve().parent.parent.parent / "sessions"
SESSION_FILE = SESSIONS_DIR / "webvpn.json"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def _aes():
    try:
        from Crypto.Cipher import AES
        return AES
    except ImportError:
        try:
            from Cryptodome.Cipher import AES
            return AES
        except ImportError:
            raise RuntimeError("WebVPN 需要 pycryptodome：pip install pycryptodome")


def convert_url(url: str, webvpn_base: str, key: bytes = DEFAULT_KEY,
                iv: bytes = DEFAULT_KEY, port: int | None = None) -> str:
    """把普通 URL 转成 WebVPN 转发 URL（仅加密 hostname，path/query 原样保留）。

    >>> convert_url("https://pubs.acs.org/doi/pdf/10.1021/x",
    ...             "https://webvpn.xxx.edu.cn")
    'https://webvpn.xxx.edu.cn/https/<hex(iv)+hex(enc(hostname))>/doi/pdf/10.1021/x'
    """
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return url
    scheme = parsed.scheme.lower()
    if port is None:
        port = parsed.port

    AES = _aes()
    cipher = AES.new(key, AES.MODE_CFB, iv, segment_size=128)
    encrypted = cipher.encrypt(hostname.encode("utf-8"))
    token = binascii.hexlify(iv).decode() + binascii.hexlify(encrypted).decode()

    scheme_part = f"{scheme}-{port}" if port else scheme
    result = f"{webvpn_base.rstrip('/')}/{scheme_part}/{token}{parsed.path}"
    if parsed.query:
        result += f"?{parsed.query}"
    return result


def convert_url_cn(url: str, webvpn_base: str) -> str:
    """Rails 型 WebVPN（/users/sign_in 登录）的明文转发 URL。

    常见格式：<base>/https/<目标host>/<path>，scheme 跟随 url。
    登录 cookie 由会话文件携带，不依赖 AES 密钥。
    """
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return url
    scheme = parsed.scheme.lower()
    result = f"{webvpn_base.rstrip('/')}/{scheme}/{hostname}{parsed.path}"
    if parsed.query:
        result += f"?{parsed.query}"
    return result


def detect_webvpn_type(session: dict) -> str:
    """从会话信息判断 WebVPN 类型：AES 加密式 vs Rails 明文式。"""
    return (session.get("type") or "webvpn").lower()


# --------------------------------------------------------------------------
# 会话读写（沿用 auth 模块的 sessions/ 约定）
# --------------------------------------------------------------------------

def load_session() -> dict:
    if not SESSION_FILE.exists():
        return {}
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        if isinstance(data, list):  # 仅 cookie 列表的旧格式
            return {"cookies": data}
        return {}
    except Exception:
        return {}


def save_session(school: str, host: str, key: str, iv: str,
                 cookies: list[dict], school_type: str = "webvpn") -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    data = {"school": school, "host": host, "key": key, "iv": iv,
            "type": school_type, "cookies": cookies, "storage": {},
            "saved_at": time.strftime("%Y-%m-%d %H:%M")}
    SESSION_FILE.write_text(json.dumps(data, indent=1, ensure_ascii=False),
                            encoding="utf-8")


def _cookie_jar(session: requests.Session, cookies: list[dict]) -> None:
    for c in cookies:
        try:
            session.cookies.set(c["name"], c["value"],
                                domain=(c.get("domain") or "").lstrip("."),
                                path=c.get("path", "/"))
        except Exception:
            continue


# --------------------------------------------------------------------------
# 出版社 PDF 直链构造（与 scansci-pdf 的 publisher 路由一致）
# --------------------------------------------------------------------------

def _resolve_doi(doi: str) -> str | None:
    try:
        r = requests.get(f"https://doi.org/{doi}", allow_redirects=True,
                         timeout=15, headers={"User-Agent": UA}, stream=True)
        r.close()
        if r.url and r.url != f"https://doi.org/{doi}":
            return r.url
    except Exception:
        pass
    return None


def publisher_pdf_url(doi: str, resolved_url: str) -> str | None:
    """按出版社构造标准 PDF 直链；无法确定时返回 None。"""
    parsed = urllib.parse.urlparse(resolved_url)
    host = parsed.netloc.lower()
    suffix = doi.split("/", 1)[-1] if "/" in doi else doi
    if "pubs.acs.org" in host:
        return f"https://pubs.acs.org/doi/pdf/{doi}"
    if "onlinelibrary.wiley.com" in host:
        return f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}"
    if "tandfonline.com" in host:
        return f"https://www.tandfonline.com/doi/pdf/{doi}?needAccess=true"
    if "nature.com" in host:
        return f"https://www.nature.com/articles/{suffix}.pdf"
    if "link.springer.com" in host:
        return f"https://link.springer.com/content/pdf/{doi}.pdf"
    if "pubs.rsc.org" in host:
        pdf = resolved_url.replace("/articlelanding/", "/articlepdf/")
        return pdf if pdf != resolved_url else None
    if "pnas.org" in host:
        return f"https://www.pnas.org/doi/pdf/{doi}"
    if "science.org" in host or "sciencemag.org" in host:
        return f"https://www.science.org/doi/pdf/{doi}"
    if "elsevier.com" in host or "sciencedirect.com" in host:
        m = re.search(r"pii/([A-Z0-9]+)", resolved_url)
        if m:
            return f"https://www.sciencedirect.com/science/article/pii/{m.group(1)}/pdfft"
    return None


# --------------------------------------------------------------------------
# 核心引擎
# --------------------------------------------------------------------------

class WebVpnEngine:
    def __init__(self, session_file: Path | None = None, timeout: float = 30):
        self.session_file = Path(session_file or SESSION_FILE)
        self.timeout = timeout

    # -- 会话 ---------------------------------------------------------------

    def _read_session(self) -> dict:
        if not self.session_file.exists():
            return {}
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def session_status(self) -> str:
        """none/valid/expired/unreachable：探测 WebVPN 会话是否仍有效。"""
        data = self._read_session()
        cookies = data.get("cookies") or []
        host = (data.get("host") or "").rstrip("/")
        if not cookies or not host:
            return "none"
        if detect_webvpn_type(data) == "webvpn_cn":
            probe = convert_url_cn("https://www.nature.com", host)
        else:
            key = (data.get("key") or "wrdvpnisthebest!").encode("utf-8")
            iv = (data.get("iv") or key).encode("utf-8") if data.get("iv") else key
            probe = convert_url("https://www.nature.com", host, key, iv)
        try:
            s = requests.Session()
            s.trust_env = False
            s.headers["User-Agent"] = UA
            _cookie_jar(s, cookies)
            r = s.get(probe, timeout=15, allow_redirects=True)
            low = (r.url or "").lower()
            if "cas" in low or "login" in low or "/authserver" in low:
                return "expired"
            return "valid" if r.status_code == 200 else "unreachable"
        except Exception:
            return "unreachable"

    # -- 下载 ---------------------------------------------------------------

    def fetch(self, doi: str, target: Path) -> tuple[bool, str]:
        """经 WebVPN 下载 DOI 全文；返回 (是否成功, 说明)。"""
        data = self._read_session()
        host = (data.get("host") or "").rstrip("/")
        cookies = data.get("cookies") or []
        if not host or not cookies:
            return False, "WebVPN 未登录：先运行 `paperflow auth login webvpn --school <学校>`"

        key = (data.get("key") or "wrdvpnisthebest!").encode("utf-8")
        iv = (data.get("iv") or "wrdvpnisthebest!").encode("utf-8")
        vpn_type = detect_webvpn_type(data)

        resolved = _resolve_doi(doi)
        if not resolved:
            resolved = f"https://doi.org/{doi}"
        pdf_url = publisher_pdf_url(doi, resolved)

        # 1) HTTP 通道：直接经 WebVPN 抓取出版社 PDF 直链
        candidates = [pdf_url, resolved] if pdf_url else [resolved]
        for url in candidates:
            if not url:
                continue
            if vpn_type == "webvpn_cn":
                ok, detail = self._http_get_pdf_cn(url, target, host, cookies)
            else:
                ok, detail = self._http_get_pdf(url, target, host, key, iv, cookies)
            if ok:
                return True, f"webvpn → {detail}"
            log.debug("webvpn http fail: %s", detail)

        # 2) 浏览器兜底：可见浏览器打开 WebVPN 目标页，捕获 PDF 响应
        try:
            ok, detail = self._browser_get_pdf(doi, resolved, target, host, key, iv)
            if ok:
                return True, f"webvpn(浏览器) → {detail}"
        except Exception as exc:
            log.debug("webvpn browser fail: %s", exc)

        return False, "webvpn: HTTP 与浏览器通道均未取得 PDF（检查会话是否过期/机构是否有权限）"

    def _http_get_pdf_cn(self, url: str, target: Path, host: str,
                         cookies: list[dict]) -> tuple[bool, str]:
        """Rails 型 WebVPN：明文转发 + 会话 cookie 直取。"""
        proxied = convert_url_cn(url, host)
        s = requests.Session()
        s.trust_env = False
        s.headers["User-Agent"] = UA
        _cookie_jar(s, cookies)
        try:
            r = s.get(proxied, timeout=self.timeout, allow_redirects=True, stream=True)
        except requests.RequestException as exc:
            return False, f"HTTP {type(exc).__name__}"
        if r.status_code >= 400:
            r.close()
            return False, f"HTTP {r.status_code}"
        first = next(r.iter_content(chunk_size=8192), b"")
        if not first.startswith(b"%PDF-"):
            r.close()
            return False, "非 PDF 响应"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            with tmp.open("wb") as fh:
                fh.write(first)
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        fh.write(chunk)
            tmp.replace(target)
        finally:
            r.close()
        return True, url[:80]

    def _http_get_pdf(self, url: str, target: Path, host: str, key: bytes,
                      iv: bytes, cookies: list[dict]) -> tuple[bool, str]:
        proxied = convert_url(url, host, key, iv)
        s = requests.Session()
        s.trust_env = False
        s.headers["User-Agent"] = UA
        _cookie_jar(s, cookies)
        try:
            r = s.get(proxied, timeout=self.timeout, allow_redirects=True, stream=True)
        except requests.RequestException as exc:
            return False, f"HTTP {type(exc).__name__}"
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}"
        first = next(r.iter_content(chunk_size=8192), b"")
        if not first.startswith(b"%PDF-"):
            r.close()
            return False, "非 PDF 响应"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            with tmp.open("wb") as fh:
                fh.write(first)
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        fh.write(chunk)
            tmp.replace(target)
        finally:
            r.close()
        return True, url[:80]

    def _browser_get_pdf(self, doi: str, resolved: str, target: Path, host: str,
                         key: bytes, iv: bytes) -> tuple[bool, str]:
        from playwright.sync_api import sync_playwright

        data = self._read_session()
        cookies = data.get("cookies") or []
        captured: list[bytes] = []
        pdf_url = publisher_pdf_url(doi, resolved)
        webvpn_url = convert_url(resolved, host, key, iv)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, args=[
                "--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            try:
                saved = []
                for c in cookies:
                    if c.get("domain"):
                        saved.append({"name": c["name"], "value": c["value"],
                                      "domain": c["domain"], "path": c.get("path", "/")})
                if saved:
                    ctx.add_cookies(saved)

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
                for nav in (webvpn_url,
                            convert_url(pdf_url, host, key, iv) if pdf_url else None):
                    captured.clear()
                    try:
                        page.goto(nav, wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass
                    time.sleep(4)
                    # 页面本身可能就是内嵌 PDF
                    body = page.evaluate(
                        "document.body ? document.body.innerText.length : 0") or 0
                    title = (page.title() or "").lower()
                    if captured:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(captured[-1])
                        if target.read_bytes()[:4] == b"%PDF-":
                            return True, "浏览器捕获 PDF"
                    if body > 200 and not any(x in title for x in
                                              ("请稍候", "loading", "just a moment")):
                        break
                # 尝试触发下载
                if pdf_url:
                    try:
                        with page.expect_download(timeout=30000) as dl:
                            page.goto(convert_url(pdf_url, host, key, iv),
                                      wait_until="commit", timeout=30000)
                        d = dl.value
                        tmp = d.path()
                        if tmp:
                            b = tmp.read_bytes()
                            if b[:4] == b"%PDF-":
                                target.parent.mkdir(parents=True, exist_ok=True)
                                target.write_bytes(b)
                                return True, "浏览器触发下载"
                    except Exception:
                        pass
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
        return False, "浏览器兜底未取得 PDF"


# --------------------------------------------------------------------------
# 登录（CAS/SSO）
# --------------------------------------------------------------------------

def webvpn_login(school: str, headful: bool = True, timeout: int = 600) -> bool:
    """弹出可见浏览器完成学校 WebVPN 的 CAS/SSO 登录，自动保存会话。"""
    entry = get_school(school)
    key = entry.key.decode("utf-8")
    iv = entry.iv.decode("utf-8")
    school_type = entry.school_type or "webvpn"

    from .. import auth as _auth

    _p, browser, ctx = _auth._launch_browser(headful=headful)
    page = ctx.new_page()
    try:
        page.goto(entry.host, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        print(f"  打开 {entry.host} 失败: {exc}")

    if school_type == "webvpn_cn":
        print(f"\n  已打开 {entry.name} 的 WebVPN（{entry.host}）")
        print("  请在浏览器中填入 学号/工号 + 密码（如有验证码也请填写）完成登录。")
        print("  脚本会自动检测登录态并保存会话…\n")
        login_paths = ("/users/sign_in", "/login", "/authserver", "/cas", "/sso")

        def _on_login_page(url: str) -> bool:
            return any(p in url for p in login_paths)

        deadline = time.time() + timeout
        saved = False
        while time.time() < deadline:
            time.sleep(3)
            try:
                if page.is_closed():
                    break
                url = page.url
                cookies = ctx.cookies()
                # 离开登录页 + cookie 明显增多 → 登录成功
                if not _on_login_page(url) and len(cookies) > 5:
                    time.sleep(2)
                    cookies = ctx.cookies()
                    save_session(entry.name, entry.host, key, iv, cookies,
                                 school_type=school_type)
                    print(f"  ✓ 登录成功，已保存 {len(cookies)} 个 cookie 到 "
                          f"sessions/webvpn.json")
                    saved = True
                    break
            except Exception:
                continue
        if not saved:
            print("  超时或未检测到登录态，未保存会话。")
        try:
            browser.close()
        except Exception:
            pass
        try:
            _p.stop()
        except Exception:
            pass
        return saved

    # ---- 原有：CAS/SSO 型 WebVPN（AES 转发） ----
    print(f"\n  已打开 {entry.school} 的 WebVPN（{entry.host}）")
    print("  请在浏览器中完成 CAS/SSO 登录（校园账号/统一身份认证）。")
    print("  脚本会自动检测登录态并保存会话…\n")

    def _login_redirect(url_low: str) -> bool:
        return any(x in url_low for x in ("/login", "cas", "sso", "/authserver",
                                          "wayf", "saml", "idp"))

    deadline = time.time() + timeout
    saved = False
    while time.time() < deadline:
        time.sleep(3)
        try:
            if page.is_closed():
                break
            current = page.url.lower()
            cookies = ctx.cookies()
            # 离开登录页且已有明显登录 cookie（>4 个）即视为成功
            if not _login_redirect(current) and len(cookies) > 4:
                time.sleep(2)
                cookies = ctx.cookies()
                save_session(entry.name, entry.host, key, iv, cookies)
                print(f"  ✓ 登录成功，已保存 {len(cookies)} 个 cookie 到 "
                      f"sessions/webvpn.json")
                saved = True
                break
        except Exception:
            continue
    if not saved:
        print("  超时或未检测到登录态，未保存会话。")
    try:
        browser.close()
    except Exception:
        pass
    try:
        _p.stop()
    except Exception:
        pass
    return saved


def show_schools(query: str = "") -> None:
    entries = search_schools(query) if query else sorted(
        _all_schools(), key=lambda e: e.province)
    if not entries:
        print(f"未找到匹配 '{query}' 的学校")
        return
    for e in entries:
        print(f"  [{e.province}] {e.name:<24} {e.host}")


def _all_schools():
    from ..schools import list_schools
    return list_schools()