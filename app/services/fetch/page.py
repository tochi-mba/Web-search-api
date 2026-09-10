"""Page acquisition: plain HTTP, headless rendering, or HTTP with a fallback.

Most pages yield their text to a plain GET, which is an order of magnitude
cheaper than a browser. Client-rendered pages return a near-empty shell instead.
``auto`` mode tries HTTP first and escalates only when the result looks too thin
to be real content.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import ForbiddenUrlError, UpstreamError
from app.core.logging import get_logger
from app.services.fetch.browser import BrowserSession
from app.services.fetch.http import HttpFetcher
from app.services.robots import RobotsPolicy
from app.services.text.extractor import ExtractedContent, extract_content
from app.services.urlsafety import Resolver, default_resolver, validate_url

logger = get_logger(__name__)

#: Below this many characters, an HTTP fetch is assumed to have returned a
#: client-rendered shell rather than real content.
RENDER_FALLBACK_THRESHOLD = 500


@dataclass(frozen=True, slots=True)
class FetchedPage:
    """Extracted content plus how it was obtained."""

    content: ExtractedContent
    final_url: str
    rendered: bool


class PageFetcher:
    """Fetches and extracts a single page under the configured policy."""

    def __init__(
        self,
        fetcher: HttpFetcher,
        *,
        browser: BrowserSession | None = None,
        robots: RobotsPolicy | None = None,
        respect_robots: bool = True,
        allow_private: bool = False,
        resolver: Resolver = default_resolver,
    ) -> None:
        """Create the page fetcher.

        Args:
            fetcher: The plain-HTTP fetcher.
            browser: Optional headless browser for rendering.
            robots: Optional robots.txt policy.
            respect_robots: Whether to honour robots.txt.
            allow_private: Whether private addresses may be fetched.
            resolver: Hostname resolver, injected for tests.
        """
        self._fetcher = fetcher
        self._browser = browser
        self._robots = robots
        self._respect_robots = respect_robots
        self._allow_private = allow_private
        self._resolver = resolver

    async def fetch(self, url: str, *, render: str = "auto") -> FetchedPage:
        """Fetch and extract one page.

        Args:
            url: The URL to fetch.
            render: ``auto``, ``always`` or ``never``.

        Returns:
            The extracted page.

        Raises:
            ForbiddenUrlError: Blocked by SSRF policy or robots.txt.
            UpstreamError: The page could not be fetched or rendered.
        """
        safe_url = validate_url(url, resolver=self._resolver, allow_private=self._allow_private)

        if self._respect_robots and self._robots is not None:
            allowed = await self._robots.can_fetch(safe_url)
            if not allowed:
                raise ForbiddenUrlError(
                    "Blocked by robots.txt",
                    detail=f"{safe_url} disallows automated fetching.",
                )

        if render == "always":
            return await self._render(safe_url)

        result = await self._fetcher.fetch(safe_url)
        content = extract_content(result.body, url=result.final_url)

        if render == "auto" and len(content.text) < RENDER_FALLBACK_THRESHOLD:
            rendered = await self._try_render(safe_url)
            if rendered is not None and len(rendered.content.text) > len(content.text):
                return rendered

        return FetchedPage(content=content, final_url=result.final_url, rendered=False)

    async def _render(self, url: str) -> FetchedPage:
        """Render a page with the browser, failing loudly if unavailable."""
        if self._browser is None:
            raise UpstreamError(
                "Rendering unavailable",
                detail="No headless browser is configured on this deployment.",
            )
        html = await self._browser.render(url)
        return FetchedPage(content=extract_content(html, url=url), final_url=url, rendered=True)

    async def _try_render(self, url: str) -> FetchedPage | None:
        """Attempt a render, returning ``None`` rather than failing the request.

        The HTTP result is already in hand at this point, so a browser problem
        should degrade to the thinner text rather than lose the page entirely.
        """
        if self._browser is None:
            return None
        try:
            return await self._render(url)
        except (UpstreamError, ForbiddenUrlError) as exc:
            logger.info("fetch.render_fallback_failed", url=url, error=str(exc))
            return None
