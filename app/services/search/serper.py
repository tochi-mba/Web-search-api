"""Serper.dev search backend - a paid Google SERP API.

The most reliable option when scraping is not acceptable, at the cost of a
per-query fee and a third-party dependency.

Its key comes from keyring like every other credential, so a deployment can run
search on one person's Serper account without that key sitting in this
service's environment.
"""

from __future__ import annotations

import httpx

from app import constants
from app.core.errors import ProviderUnavailableError, TimeoutProblem, UpstreamError
from app.services.keyring.caller import Caller
from app.services.keyring.client import KeyringClient, NotConnectedError, ResolvedAuth
from app.services.search.base import SearchQuery, SearchResponse, SearchResult

SERPER_ENDPOINT = "https://google.serper.dev/search"


class SerperSearchBackend:
    """Queries the Serper.dev SERP API."""

    name = "serper"

    #: Service name this backend's credential is stored under in keyring.
    KEYRING_SERVICE = "serper"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        keyring: KeyringClient | None = None,
        caller: Caller | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        """Create the backend.

        Args:
            client: Shared HTTP client.
            keyring: Where the API key comes from.
            caller: Whose Serper account to use. Without one there is no key.
            timeout_seconds: Per-request timeout.
        """
        self._client = client
        self._keyring = keyring
        self._caller = caller
        self._timeout = timeout_seconds

    def for_caller(self, caller: Caller | None) -> SerperSearchBackend:
        """Return a backend bound to one caller's credential."""
        return SerperSearchBackend(
            self._client,
            keyring=self._keyring,
            caller=caller,
            timeout_seconds=self._timeout,
        )

    async def _auth(self) -> ResolvedAuth | None:
        """Resolve this caller's Serper credential, or ``None`` if there is none."""
        if self._keyring is None or not self._keyring.is_configured or self._caller is None:
            return None
        try:
            return await self._keyring.resolve(
                profile=self._caller.profile,
                service=self.KEYRING_SERVICE,
                user_token=self._caller.user_token,
            )
        except NotConnectedError:
            return None

    async def is_configured(self) -> bool:
        """Serper is usable only when this caller has connected it."""
        return await self._auth() is not None

    async def search(self, query: SearchQuery) -> SearchResponse:
        """Run one query against Serper with this caller's key."""
        auth = await self._auth()
        if auth is None:
            raise ProviderUnavailableError(
                "Serper is not connected",
                detail="No Serper credential is stored in keyring for this caller.",
            )

        try:
            response = await self._client.post(
                SERPER_ENDPOINT,
                headers={"Content-Type": "application/json", **auth.headers},
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
