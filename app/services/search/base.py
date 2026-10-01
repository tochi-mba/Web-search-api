"""The search backend seam.

Google actively fights scrapers: it serves consent walls, CAPTCHAs and rotating
markup to headless browsers. Rather than pretend otherwise, search is defined as
a protocol with several implementations, so a deployment that cannot reliably
scrape Google can point at SearxNG or a paid SERP API without touching anything
above this layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.services.keyring.caller import Caller


class SafeSearch(StrEnum):
    """How hard explicit results are filtered out, least to most."""

    OFF = "off"
    MODERATE = "moderate"
    STRICT = "strict"

    def at_least(self, floor: SafeSearch) -> SafeSearch:
        """This level, or ``floor`` when that filters more."""
        order = tuple(SafeSearch)
        return self if order.index(self) >= order.index(floor) else floor

    @classmethod
    def for_request(cls, requested: bool | None, *, chosen: SafeSearch | None) -> SafeSearch:
        """The filtering one request runs under.

        ``chosen`` is the person's level, or ``None`` when nobody has chosen and the
        request alone decides, as it always did. A request that says nothing takes the
        person's level. One that says takes the stricter of the two: a household that
        chose ``strict`` is not undone by a request body.
        """
        asked = cls.OFF if requested is False else cls.MODERATE
        if chosen is None:
            return asked
        return chosen if requested is None else asked.at_least(chosen)


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
    safe_search: SafeSearch = SafeSearch.MODERATE
    recency_days: int | None = None
    """Only results this recent, in days. ``None`` is no filter."""

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
