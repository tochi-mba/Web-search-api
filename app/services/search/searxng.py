"""SearxNG search backend.

SearxNG is a self-hostable metasearch engine with a JSON API. It is the
recommended failover for deployments that cannot rely on scraping Google.
"""

from __future__ import annotations

import httpx

from app import constants
from app.core.errors import TimeoutProblem, UpstreamError
from app.services.search.base import SearchQuery, SearchResponse, SearchResult


class SearxngSearchBackend:
    """Queries a SearxNG instance over its JSON API."""

    name = "searxng"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        timeout_seconds: float = 15.0,
    ) -> None:
        """Create the backend.

        Args:
            client: Shared HTTP client.
            base_url: Root URL of the SearxNG instance. Empty disables the backend.
            timeout_seconds: Per-request timeout.
        """
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    async def is_configured(self) -> bool:
        """SearxNG is usable only once an instance URL is configured."""
        return bool(self._base_url)

    async def search(self, query: SearchQuery) -> SearchResponse:
        """Run one query against the configured SearxNG instance."""
        try:
            response = await self._client.get(
                f"{self._base_url}/search",
                params={
                    "q": query.to_query_string(),
                    "format": "json",
                    "language": query.language,
                    "safesearch": "1" if query.safe_search else "0",
                },
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise TimeoutProblem("SearxNG timed out", detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError("SearxNG request failed", detail=str(exc)) from exc

        if response.status_code != 200:
            raise UpstreamError(
                "SearxNG returned an error",
                detail=f"{self._base_url} responded {response.status_code}.",
            )

        payload = response.json()
        results = [
            SearchResult(
                title=str(item.get("title", "")),
                url=str(item["url"]),
                snippet=str(item.get("content", ""))[: constants.MAX_SNIPPET_CHARS],
                rank=index,
            )
            for index, item in enumerate(payload.get("results", [])[: query.max_results], start=1)
            if item.get("url")
        ]
        return SearchResponse(query=query.query, backend=self.name, results=results)
