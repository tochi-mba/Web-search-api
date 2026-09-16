"""Backend selection and failover.

The preferred backend is tried first; if it is blocked or fails, the remaining
configured backends are tried in order. This is the difference between an API
that stops working the day Google tightens its bot detection and one that
degrades to a slightly different source of results.
"""

from __future__ import annotations

from app.core.errors import ProviderUnavailableError, SearchBlockedError, UpstreamError
from app.core.logging import get_logger
from app.services.keyring.caller import Caller
from app.services.search.base import CallerSearchBackend, SearchBackend, SearchQuery, SearchResponse

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

    def _ordered(self, preferred: str | None = None) -> list[SearchBackend]:
        """Backends in the order they should be attempted."""
        name = preferred if preferred is not None else self._preferred
        if not name:
            return list(self._backends)
        preferred_backends = [b for b in self._backends if b.name == name]
        rest = [b for b in self._backends if b.name != name]
        return preferred_backends + rest

    async def search(
        self,
        query: SearchQuery,
        *,
        caller: Caller | None = None,
        preferred: str | None = None,
    ) -> SearchResponse:
        """Run ``query``, failing over between backends as needed.

        Args:
            query: What to look up.
            caller: Who this is for; Serper uses their credential.
            preferred: Backend to try first for this call. ``None`` uses the
                deployment default. Unknown names are ignored.

        Returns:
            The first successful response.

        Raises:
            ProviderUnavailableError: No backend was configured, or every
                configured backend failed.
        """
        attempted = 0
        last_error: Exception | None = None

        for backend in self._ordered(preferred):
            active = (
                backend.for_caller(caller) if isinstance(backend, CallerSearchBackend) else backend
            )
            if not await active.is_configured():
                logger.debug("search.backend_skipped", backend=active.name)
                continue

            attempted += 1
            try:
                response = await active.search(query)
            except (SearchBlockedError, UpstreamError) as exc:
                logger.warning("search.backend_failed", backend=active.name, error=str(exc))
                last_error = exc
                continue

            if response.is_empty and attempted < len(self._backends):
                # An empty result may be genuine, but it may also be a layout
                # change we failed to parse. Give the next backend a chance.
                logger.info("search.backend_empty", backend=active.name)
                last_error = last_error or UpstreamError(
                    "Search returned no results", detail=f"{active.name} returned nothing."
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
