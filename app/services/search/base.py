"""The search backend seam.

Google actively fights scrapers: it serves consent walls, CAPTCHAs and rotating
markup to headless browsers. Rather than pretend otherwise, search is defined as
a protocol with several implementations, so a deployment that cannot reliably
scrape Google can point at SearxNG or a paid SERP API without touching anything
above this layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.services.keyring.caller import Caller


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One organic result from a search engine."""

    title: str
    url: str
    snippet: str
    rank: int


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """A single search request."""

    query: str
    max_results: int = 10
    site: str | None = None
    language: str = "en"
    region: str = "us"
    safe_search: bool = True

    def to_query_string(self) -> str:
        """Render the query, applying a ``site:`` filter when one is set."""
        if self.site:
            return f"{self.query} site:{self.site}"
        return self.query


@dataclass(frozen=True, slots=True)
class SearchResponse:
    """The outcome of running one query against one backend."""

    query: str
    backend: str
    results: list[SearchResult] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """Whether the backend returned nothing at all."""
        return not self.results


@runtime_checkable
class SearchBackend(Protocol):
    """Anything that can turn a query into ranked results."""

    #: Stable identifier reported on responses and used in configuration.
    name: str

    async def is_configured(self) -> bool:
        """Whether this backend has what it needs to run."""
        ...

    async def search(self, query: SearchQuery) -> SearchResponse:
        """Execute one query.

        Raises:
            SearchBlockedError: The engine refused to serve results.
        """
        ...


@runtime_checkable
class CallerSearchBackend(Protocol):
    """A backend whose credentials must be bound separately for each request."""

    def for_caller(self, caller: Caller | None) -> SearchBackend:
        """Return a request-local backend without modifying the shared instance."""
        ...
