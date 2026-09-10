"""Serper.dev search backend - a paid Google SERP API.

The most reliable option when scraping is not acceptable, at the cost of a
per-query fee and a third-party dependency.
"""

from __future__ import annotations

import httpx

from app import constants
from app.core.errors import TimeoutProblem, UpstreamError
from app.services.search.base import SearchQuery, SearchResponse, SearchResult

SERPER_ENDPOINT = "https://google.serper.dev/search"


class SerperSearchBackend:
    """Queries the Serper.dev SERP API."""

    name = "serper"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str,
        timeout_seconds: float = 15.0,
    ) -> None:
        """Create the backend.

        Args:
            client: Shared HTTP client.
            api_key: Serper API key. Empty disables the backend.
            timeout_seconds: Per-request timeout.
        """
        self._client = client
        self._api_key = api_key
        self._timeout = timeout_seconds

    async def is_configured(self) -> bool:
        """Serper is usable only once an API key is configured."""
        return bool(self._api_key)

    async def search(self, query: SearchQuery) -> SearchResponse:
        """Run one query against Serper."""
        try:
            response = await self._client.post(
                SERPER_ENDPOINT,
                headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
                json={
                    "q": query.to_query_string(),
                    "num": query.max_results,
                    "hl": query.language,
                    "gl": query.region,
                },
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise TimeoutProblem("Serper timed out", detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError("Serper request failed", detail=str(exc)) from exc

        if response.status_code != 200:
            raise UpstreamError(
                "Serper returned an error", detail=f"Serper responded {response.status_code}."
            )

        payload = response.json()
        results = [
            SearchResult(
                title=str(item.get("title", "")),
                url=str(item["link"]),
                snippet=str(item.get("snippet", ""))[: constants.MAX_SNIPPET_CHARS],
                rank=index,
            )
            for index, item in enumerate(payload.get("organic", [])[: query.max_results], start=1)
            if item.get("link")
        ]
        return SearchResponse(query=query.query, backend=self.name, results=results)
