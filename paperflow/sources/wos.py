"""WOS Starter API 与本地 Plain Text 导出适配器。"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import requests
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..models import Paper, author_names, clean_text, normalize_doi
from . import SOURCES


WOS_API_URL = "https://api.clarivate.com/apis/wos-starter/v1/documents"
WOS_PAGE_SIZE = 50


class WosApiError(RuntimeError):
    """不携带请求头或 API Key 的可操作 WOS 错误。"""


@dataclass(frozen=True)
class WosPage:
    papers: list[Paper]
    page: int
    total: int
    record_start: int
    record_end: int
    remaining_day: int | None = None
    remaining_second: int | None = None


def parse_wos_plain_text(text: str, species: str) -> list[Paper]:
    papers: list[Paper] = []
    for raw_record in re.split(r"\nER\s*(?:\n|$)", text.replace("\r\n", "\n")):
        fields: dict[str, list[str]] = {}
        current_tag = ""
        for line in raw_record.splitlines():
            match = re.match(r"^([A-Z0-9]{2}) (.*)$", line)
            if match:
                current_tag = match.group(1)
                fields.setdefault(current_tag, []).append(match.group(2).strip())
            elif line.startswith("   ") and current_tag and fields.get(current_tag):
                if current_tag == "AU":
                    fields[current_tag].append(line.strip())
                else:
                    fields[current_tag][-1] += " " + line.strip()
        title = clean_text(" ".join(fields.get("TI", [])))
        if not title:
            continue
        papers.append(Paper(
            title=title,
            abstract=clean_text(" ".join(fields.get("AB", []))),
            year=clean_text(" ".join(fields.get("PY", []))),
            journal=clean_text(" ".join(fields.get("SO", []))),
            authors=author_names(fields.get("AU", [])),
            doi=normalize_doi(" ".join(fields.get("DI", []))),
            pmid=clean_text(" ".join(fields.get("PM", []))),
            sources={"WOS"},
            species={species},
        ))
    return papers


def load_wos_exports(directory: Path, species: str = "") -> list[Paper]:
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    papers: list[Paper] = []
    wanted = species.casefold()
    for item in manifest.get("exports", []):
        keyword = clean_text(item.get("species"))
        if wanted and keyword.casefold() != wanted:
            continue
        path = directory / str(item.get("file", ""))
        if path.is_file():
            papers.extend(parse_wos_plain_text(path.read_text(encoding="utf-8-sig"), keyword))
    return papers


def _authors(record: dict[str, Any]) -> list[str]:
    items = (record.get("names") or {}).get("authors") or record.get("authors") or []
    normalized: list[Any] = []
    for item in items:
        if isinstance(item, dict) and not item.get("name"):
            item = {**item, "name": item.get("displayName") or item.get("wosStandard") or ""}
        normalized.append(item)
    return author_names(normalized)


def parse_wos_api_record(record: dict[str, Any], species: str) -> Paper | None:
    identifiers = record.get("identifiers") or {}
    source = record.get("source") or {}
    title = clean_text(record.get("title"))
    if not title:
        return None
    return Paper(
        title=title,
        abstract=clean_text(record.get("abstract")),
        year=clean_text(source.get("publishYear") or record.get("year")),
        journal=clean_text(source.get("sourceTitle") or record.get("journal")),
        authors=_authors(record),
        doi=normalize_doi(identifiers.get("doi", "")),
        sources={"WOS"},
        species={species},
    )


def _header_int(headers: Any, name: str) -> int | None:
    try:
        value = headers.get(name)
        return int(value) if value not in (None, "") else None
    except (AttributeError, TypeError, ValueError):
        return None


class WosSource:
    name = "WOS"

    def __init__(self, exports_dir: Path | None = None) -> None:
        self.exports_dir = exports_dir or Path("wos_exports")
        self._request_lock = threading.Lock()
        self._next_request_at = 0.0

    @staticmethod
    def api_key() -> str:
        return os.getenv("WOS_API_KEY", "").strip()

    def _throttle(self, interval: float) -> None:
        with self._request_lock:
            now = time.monotonic()
            if now < self._next_request_at:
                time.sleep(self._next_request_at - now)
            self._next_request_at = time.monotonic() + max(0.0, interval)

    @staticmethod
    def _query(species: str) -> str:
        escaped = species.replace("\\", "\\\\").replace('"', '\\"')
        return f'TS=("{escaped}")'

    def _request_page(
        self, client: Any, species: str, page: int, request_interval: float,
    ) -> tuple[list[dict[str, Any]], int, int | None, int | None]:
        api_key = self.api_key()
        if not api_key:
            raise WosApiError("WOS_API_KEY 未配置；请写入本机 .env 后重试")
        self._throttle(request_interval)
        request_kwargs = {
            "headers": {"X-ApiKey": api_key},
            "params": {"db": "WOS", "q": self._query(species), "limit": WOS_PAGE_SIZE, "page": page},
            "timeout": 30,
        }
        response = None
        last_error: Exception | None = None
        request_client = client
        for attempt in range(3):
            try:
                response = request_client.get(WOS_API_URL, **request_kwargs)
                break
            except requests.exceptions.ProxyError as exc:
                last_error = exc
                # WOS 官方 API 可直连；代理链路异常时用独立直连会话重试。
                request_client = requests.Session()
                request_client.trust_env = False
            except requests.exceptions.RequestException as exc:
                last_error = exc
            if attempt < 2:
                time.sleep(attempt + 1)
        if response is None:
            error_name = type(last_error).__name__ if last_error else "NetworkError"
            raise WosApiError(f"WOS API 网络错误（{error_name}，已重试 3 次）") from None

        status = int(getattr(response, "status_code", 0))
        if status == 401:
            raise WosApiError("WOS API Key 无效或已失效（HTTP 401）")
        if status == 403:
            raise WosApiError("WOS API 无权限或当日配额已用完（HTTP 403）")
        if status == 429:
            retry = _header_int(getattr(response, "headers", {}), "Retry-After")
            suffix = f"，建议 {retry} 秒后重试" if retry is not None else "，请降低速率后重试"
            raise WosApiError(f"WOS API 触发限速（HTTP 429）{suffix}")
        if status >= 500:
            raise WosApiError(f"WOS API 服务暂时不可用（HTTP {status}）")
        if status != 200:
            raise WosApiError(f"WOS API 请求失败（HTTP {status or 'unknown'}）")
        try:
            payload = response.json()
        except Exception:
            raise WosApiError("WOS API 返回了无法解析的数据") from None
        metadata = payload.get("metadata") or {}
        hits = payload.get("hits") or payload.get("documents") or []
        return (
            hits,
            int(metadata.get("total") or 0),
            _header_int(getattr(response, "headers", {}), "x-ratelimit-remaining-day"),
            _header_int(getattr(response, "headers", {}), "x-ratelimit-remaining-second"),
        )

    def iter_species_pages(
        self,
        client: Any,
        species: str,
        max_records: int,
        *,
        start_record: int = 0,
        request_interval: float = 1.0,
    ) -> Iterator[WosPage]:
        """按固定 50 条分页；start_record 可位于页中，用于精确断点续传。"""
        cursor = max(0, start_record)
        target = max_records if max_records > 0 else None
        total: int | None = None
        while total is None or cursor < total:
            if target is not None and cursor >= target:
                break
            page_number = cursor // WOS_PAGE_SIZE + 1
            page_offset = cursor % WOS_PAGE_SIZE
            hits, total, remaining_day, remaining_second = self._request_page(
                client, species, page_number, request_interval
            )
            if total == 0:
                yield WosPage([], page_number, 0, 0, 0, remaining_day, remaining_second)
                break
            available = hits[page_offset:]
            if not available:
                break
            remaining = len(available)
            if target is not None:
                remaining = min(remaining, target - cursor)
            selected = available[:remaining]
            papers = [paper for record in selected if (paper := parse_wos_api_record(record, species))]
            record_start = cursor
            cursor += len(selected)
            yield WosPage(
                papers=papers,
                page=page_number,
                total=total,
                record_start=record_start,
                record_end=cursor,
                remaining_day=remaining_day,
                remaining_second=remaining_second,
            )

    def search_species(self, client: Any, species: str, limit: int, **kw: Any) -> list[Paper]:
        if self.api_key():
            results: list[Paper] = []
            for page in self.iter_species_pages(client, species, limit, request_interval=float(kw.get("request_interval", 1.0))):
                results.extend(page.papers)
            return results
        local = load_wos_exports(self.exports_dir, species) if self.exports_dir.exists() else []
        if local:
            return local[:limit] if limit > 0 else local
        raise WosApiError("WOS_API_KEY 未配置，且 wos_exports/ 中没有该关键词的本地导出")

    def search_doi(self, client: Any, doi: str) -> list[Paper]:
        return []


SOURCES.register(WosSource())
