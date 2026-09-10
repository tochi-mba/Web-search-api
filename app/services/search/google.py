"""Google SERP scraping via headless Chromium."""

from __future__ import annotations

from urllib.parse import urlencode

from app.core.errors import SearchBlockedError
from app.core.logging import get_logger
from app.services.fetch.browser import BrowserSession
from app.services.search.base import SearchQuery, SearchResponse
from app.services.search.serp_parser import detect_block, parse_google_serp

logger = get_logger(__name__)

GOOGLE_SEARCH_URL = "https://www.google.com/search"

#: Present once results have rendered. Waiting on it avoids reading a half-built DOM.
_RESULTS_SELECTOR = "div#search"


def build_search_url(query: SearchQuery) -> str:
    """Build the Google search URL for a query."""
    params = {
        "q": query.to_query_string(),
        "num": str(min(query.max_results + 5, 100)),
        "hl": query.language,
        "gl": query.region,
    }
    if query.safe_search:
        params["safe"] = "active"
    return f"{GOOGLE_SEARCH_URL}?{urlencode(params)}"


class GoogleSearchBackend:
    """Scrapes Google's result pages with a headless browser.

    Google actively fights this: expect consent walls and CAPTCHAs some of the
    time. Those are surfaced as :class:`SearchBlockedError` so the router above
    can fail over to another backend rather than returning silent nonsense.
    """

    name = "google"

    def __init__(self, browser: BrowserSession) -> None:
        """Create the backend around a browser session."""
        self._browser = browser

    async def is_configured(self) -> bool:
        """Google needs no credentials, only a working browser."""
        return True

    async def search(self, query: SearchQuery) -> SearchResponse:
        """Run one query against Google and parse the results."""
        url = build_search_url(query)
        html = await self._browser.render(url, wait_for_selector=_RESULTS_SELECTOR)

        block = detect_block(html)
        if block is not None:
            logger.warning("search.blocked", backend=self.name, reason=str(block))
            raise SearchBlockedError(
                f"Google served a {block} page",
                detail=(
                    "Google returned an interstitial instead of results. "
                    "Configure an alternate search backend to fail over."
                ),
            )

        results = parse_google_serp(html, max_results=query.max_results)
        logger.info("search.completed", backend=self.name, results=len(results))
        return SearchResponse(query=query.query, backend=self.name, results=results)
