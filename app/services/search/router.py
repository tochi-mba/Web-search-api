"""Backend selection and failover.

The preferred backend is tried first; if it is blocked or fails, the remaining
configured backends are tried in order. This is the difference between an API
that stops working the day Google tightens its bot detection and one that
degrades to a slightly different source of results.
"""

from __future__ import annotations

from app.core.errors import ProviderUnavailableError, SearchBlockedError, UpstreamError
from app.core.logging import get_logger
from app.services.search.base import SearchBackend, SearchQuery, SearchResponse

logger = get_logger(__name__)


class SearchRouter:
    """Runs a query against the first backend that can serve it."""

    def __init__(self, backends: list[SearchBackend], *, preferred: str | None = None) -> None:
        """Create the router.

        Args:
            backends: Every backend this deployment knows about.
            preferred: Name of the backend to try first. Unknown names are
                ignored, so a typo degrades to the default order rather than
                taking search down.
        """
        self._backends = backends
        self._preferred = preferred

    def _ordered(self) -> list[SearchBackend]:
        """Backends in the order they should be attempted."""
        if not self._preferred:
            return list(self._backends)
        preferred = [b for b in self._backends if b.name == self._preferred]
        rest = [b for b in self._backends if b.name != self._preferred]
        return preferred + rest

    async def search(self, query: SearchQuery) -> SearchResponse:
        """Run ``query``, failing over between backends as needed.

        Returns:
            The first successful response.

        Raises:
            ProviderUnavailableError: No backend was configured, or every
                configured backend failed.
        """
        attempted = 0
        last_error: Exception | None = None

        for backend in self._ordered():
            if not await backend.is_configured():
                logger.debug("search.backend_skipped", backend=backend.name)
                continue

            attempted += 1
            try:
                response = await backend.search(query)
            except (SearchBlockedError, UpstreamError) as exc:
                logger.warning("search.backend_failed", backend=backend.name, error=str(exc))
                last_error = exc
                continue

            if response.is_empty and attempted < len(self._backends):
                # An empty result may be genuine, but it may also be a layout
                # change we failed to parse. Give the next backend a chance.
                logger.info("search.backend_empty", backend=backend.name)
                last_error = last_error or UpstreamError(
                    "Search returned no results", detail=f"{backend.name} returned nothing."
                )
                continue

            return response

        if attempted == 0:
            raise ProviderUnavailableError(
                "No search backend configured",
                detail="Configure the Google browser backend, SearxNG or Serper.",
            )

        raise ProviderUnavailableError(
            "Every search backend failed",
            detail=str(last_error) if last_error else "All backends were exhausted.",
        )
