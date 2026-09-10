"""robots.txt fetching, caching and evaluation.

Being a polite crawler is cheap: one cached request per host, and sites that ask
not to be scraped are not scraped. ``WSA_RESPECT_ROBOTS=false`` exists for
deployments crawling their own infrastructure.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)

#: How long a fetched robots.txt is trusted before being fetched again.
ROBOTS_CACHE_TTL_SECONDS = 3600.0


class RobotsPolicy:
    """Caches robots.txt per host and answers fetch-permission questions."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        user_agent: str,
        timeout_seconds: float = 5.0,
    ) -> None:
        """Create the policy.

        Args:
            client: HTTP client used to fetch robots.txt.
            user_agent: The agent string rules are evaluated against.
            timeout_seconds: Per-fetch timeout for robots.txt itself.
        """
        self._client = client
        self._user_agent = user_agent
        self._timeout = timeout_seconds
        self._parsers: dict[str, RobotFileParser | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, origin: str) -> asyncio.Lock:
        """Return the lock guarding fetches for one origin."""
        lock = self._locks.get(origin)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[origin] = lock
        return lock

    async def _parser_for(self, origin: str) -> RobotFileParser | None:
        """Fetch and parse robots.txt for an origin, memoising the result.

        Returns ``None`` when robots.txt is missing or unreadable, which by
        convention means everything is allowed.
        """
        if origin in self._parsers:
            return self._parsers[origin]

        async with self._lock_for(origin):
            if origin in self._parsers:
                return self._parsers[origin]

            parser: RobotFileParser | None = None
            try:
                response = await self._client.get(
                    f"{origin}/robots.txt",
                    timeout=self._timeout,
                    headers={"User-Agent": self._user_agent},
                )
            except httpx.HTTPError as exc:
                logger.debug("robots.fetch_failed", origin=origin, error=str(exc))
            else:
                if response.status_code == 200:
                    parser = RobotFileParser()
                    parser.parse(response.text.splitlines())
                else:
                    logger.debug("robots.absent", origin=origin, status=response.status_code)

            self._parsers[origin] = parser
            return parser

    async def can_fetch(self, url: str) -> bool:
        """Whether robots.txt permits fetching ``url``."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        parser = await self._parser_for(origin)
        if parser is None:
            return True
        return parser.can_fetch(self._user_agent, url)
