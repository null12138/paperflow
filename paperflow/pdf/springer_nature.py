"""Springer Nature OpenAccess API 客户端。

该接口只返回 Springer Nature API 授权范围内的 OA 内容；付费文章由
SpringerLink 机构授权策略继续处理。
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import requests

from .. import net

API = "https://api.springernature.com/openaccess/json"
PREFIXES = ("10.1007/", "10.1186/", "10.1038/", "10.1057/", "10.1365/")


class SpringerNatureEngine:
    def __init__(self, proxies: list[str | None] | None = None) -> None:
        self.proxies = proxies or net.DEFAULT_PROXIES
        self.api_key = os.getenv("SPRINGER_NATURE_API_KEY", "").strip()

    def fetch(self, doi: str, target: Path) -> tuple[bool, str]:
        if not self.api_key:
            return False, "SPRINGER_NATURE_API_KEY 未配置"
        last = ""
        for proxy in self.proxies:
            try:
                session = net.make_session(proxy)
                response = session.get(
                    API,
                    params={"q": f"doi:{quote(doi, safe='')}", "api_key": self.api_key},
                    headers={"Accept": "application/json", "User-Agent": "paperflow/1.0"},
                    timeout=30,
                )
                if response.status_code in (401, 403):
                    return False, f"Springer Nature API 无权限（HTTP {response.status_code}）"
                if response.status_code == 404:
                    return False, "Springer Nature API 未找到该 DOI"
                response.raise_for_status()
                payload = response.json()
                urls: list[str] = []
                for record in payload.get("records") or []:
                    for item in record.get("url") or []:
                        if isinstance(item, dict):
                            value = item.get("value") or item.get("url")
                            fmt = str(item.get("format") or "").casefold()
                            if value and (fmt == "pdf" or str(value).casefold().endswith(".pdf")):
                                urls.append(str(value))
                        elif isinstance(item, str) and item.casefold().endswith(".pdf"):
                            urls.append(item)
                seen: set[str] = set()
                for pdf_url in urls:
                    if pdf_url in seen:
                        continue
                    seen.add(pdf_url)
                    result = session.get(pdf_url, timeout=40, allow_redirects=True)
                    if result.content[:5] == b"%PDF-":
                        target.write_bytes(result.content)
                        return True, f"Springer Nature API → {doi}"
                return False, "Springer Nature API 未返回可用 OA PDF"
            except (requests.RequestException, ValueError) as exc:
                last = f"{type(exc).__name__}: {str(exc)[:100]}"
        return False, f"Springer Nature API 网络失败（{last or 'unknown'}）"
