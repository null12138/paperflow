"""Elsevier Article Retrieval API PDF 客户端。

只使用用户自己的 API Key/机构令牌；接口返回非 PDF 时交给其他出版社策略处理。
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import requests

from .. import net

API = "https://api.elsevier.com/content/article/doi/{}"


class ElsevierEngine:
    def __init__(self, proxies: list[str | None] | None = None) -> None:
        self.proxies = proxies or net.DEFAULT_PROXIES
        self.api_key = os.getenv("ELSEVIER_API_KEY", "").strip()
        self.insttoken = os.getenv("ELSEVIER_INSTTOKEN", "").strip()

    def fetch(self, doi: str, target: Path) -> tuple[bool, str]:
        if not self.api_key:
            return False, "ELSEVIER_API_KEY 未配置"
        url = API.format(quote(doi, safe=""))
        headers = {
            "X-ELS-APIKey": self.api_key,
            "Accept": "application/pdf",
            "User-Agent": "paperflow/1.0",
        }
        if self.insttoken:
            headers["X-ELS-Insttoken"] = self.insttoken
        last = ""
        for proxy in self.proxies:
            try:
                session = net.make_session(proxy)
                response = session.get(
                    url, params={"httpAccept": "application/pdf"},
                    headers=headers, timeout=40, allow_redirects=True,
                )
                if response.status_code in (401, 403):
                    return False, f"Elsevier API 无权限（HTTP {response.status_code}，可能需要机构令牌/订阅）"
                if response.status_code == 404:
                    return False, "Elsevier API 未找到该 DOI"
                response.raise_for_status()
                if response.content[:5] != b"%PDF-":
                    content_type = response.headers.get("content-type", "unknown")
                    return False, f"Elsevier API 未返回 PDF（{content_type}）"
                target.write_bytes(response.content)
                return True, f"Elsevier API → {doi}"
            except requests.RequestException as exc:
                last = f"{type(exc).__name__}: {str(exc)[:100]}"
        return False, f"Elsevier API 网络失败（{last or 'unknown'}）"
