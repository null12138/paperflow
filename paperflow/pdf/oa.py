"""OA 下载引擎：Unpaywall（含 PMC/EuropePMC 候选）。"""

from __future__ import annotations

import re
from pathlib import Path

import requests

from .. import net

API = "https://api.unpaywall.org/v2/{}"
OPENALEX_API = "https://api.openalex.org/works/https://doi.org/{}"
PROXY_TIMEOUTS = [8, 12, 25]


class OaEngine:
    def __init__(self, email: str = "", proxies: list[str | None] | None = None) -> None:
        self.email = email
        self.proxies = proxies or net.DEFAULT_PROXIES

    def _candidates_for_doi(self, doi: str) -> list[str]:
        # OpenAlex 不强制邮箱，先查其公开 locations；再用 Unpaywall 补充更完整的 OA 列表。
        candidates: list[str] = []
        for proxy in self.proxies:
            try:
                session = net.make_session(proxy, email=self.email)
                params = {"mailto": self.email} if self.email else {}
                response = session.get(OPENALEX_API.format(doi), params=params, timeout=20)
                if response.status_code == 404:
                    break
                response.raise_for_status()
                payload = response.json()
                locations = []
                best = payload.get("best_oa_location")
                if best:
                    locations.append(best)
                locations.extend(payload.get("locations") or [])
                for location in locations:
                    source = location.get("pdf_url") or (location.get("landing_page_url") if location.get("is_oa") else "")
                    if source and source not in candidates:
                        candidates.append(source)
                break
            except Exception:
                continue
        if not self.email:
            return candidates
        last = None
        for attempt in range(3):
            for i, proxy in enumerate(self.proxies):
                s = net.make_session(proxy, email=self.email)
                try:
                    r = s.get(API.format(doi), params={"email": self.email}, timeout=PROXY_TIMEOUTS[i])
                    if r.status_code == 404:
                        return []
                    r.raise_for_status()
                    data = r.json()
                    cands: list[str] = list(candidates)
                    locs = []
                    if data.get("best_oa_location"):
                        locs.append(data["best_oa_location"])
                    locs.extend(data.get("oa_locations") or [])
                    for loc in locs:
                        url = loc.get("url_for_pdf") or loc.get("url")
                        if url and url not in cands:
                            cands.append(url)
                    return cands
                except Exception as exc:
                    last = exc
                    continue
        raise RuntimeError(f"unpaywall查询失败: {str(last)[:100]}")

    def fetch(self, doi: str, target: Path) -> tuple[bool, str]:
        try:
            cands = self._candidates_for_doi(doi)
        except Exception as exc:
            return False, str(exc)[:120]
        if not cands:
            return False, "Unpaywall 无 OA 候选"
        for url in cands:
            if re.search(r"pmc\.ncbi\.nlm\.nih\.gov", url):
                proxies = [None] + [p for p in self.proxies if p]  # PMC 直连优先
            else:
                proxies = self.proxies
            for proxy in proxies:
                try:
                    s = net.make_session(proxy, email=self.email)
                    r = s.get(url, timeout=25, allow_redirects=True)
                    if r.content[:5] == b"%PDF-":
                        target.write_bytes(r.content)
                        return True, f"OA → {url[:80]}"
                    break
                except requests.RequestException:
                    continue
        return False, "OA 候选下载失败"
