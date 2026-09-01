"""数据源适配器基类与注册表。"""

from __future__ import annotations

from typing import Any, Protocol

from ..models import Paper


class SearchSource(Protocol):
    name: str

    def search_species(self, client: Any, species: str, limit: int) -> list[Paper]:
        ...

    def search_doi(self, client: Any, doi: str) -> list[Paper]:
        ...


class Registry:
    def __init__(self) -> None:
        self._sources: dict[str, SearchSource] = {}

    def register(self, source: SearchSource) -> SearchSource:
        self._sources[source.name] = source
        return source

    def get(self, name: str) -> SearchSource | None:
        return self._sources.get(name)

    def names(self) -> list[str]:
        return list(self._sources)

    def ordered(self, order: list[str]) -> list[SearchSource]:
        return [self._sources[n] for n in order if n in self._sources]


SOURCES = Registry()

# 导入子模块触发各适配器注册
from . import wos  # noqa: F401,E402
from . import pubmed_crossref_s2  # noqa: F401,E402
from . import cnki  # noqa: F401,E402
