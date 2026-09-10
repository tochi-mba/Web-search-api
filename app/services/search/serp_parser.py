"""Parsing of Google search result pages.

Kept deliberately free of any I/O so it can be tested against saved HTML, which
matters because Google's markup changes without notice and the fixtures are the
only honest record of what we expect to see.
"""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import parse_qs, urlsplit

from selectolax.parser import HTMLParser, Node

from app import constants
from app.core.errors import SearchBlockedError
from app.services.search.base import SearchResult

#: Containers that have held one organic result across Google layouts.
_RESULT_SELECTORS = ("div.MjjYud", "div.g", "div.Gx5Zad", "div.tF2Cxc")

#: Candidate selectors for a result's heading, most specific first.
_TITLE_SELECTORS = ("h3.LC20lb", "h3")

#: Candidate selectors for a result's snippet, most specific first.
_SNIPPET_SELECTORS = ("div.VwiC3b", "div.st", "div[data-sncf]", "span.aCOpRe")

#: Google-internal paths that are navigation, not results.
_INTERNAL_PREFIXES = ("/search", "/preferences", "/advanced_search", "/setprefs")

#: Phrases that mean Google served a consent interstitial.
_CONSENT_MARKERS = (
    "before you continue",
    "consent.google.com",
    "we use cookies and data",
)

#: Phrases that mean Google served an anti-bot challenge.
_CAPTCHA_MARKERS = (
    "unusual traffic",
    "/sorry/index",
    "g-recaptcha",
    "recaptcha",
    "our systems have detected",
)


class BlockReason(StrEnum):
    """Why a search engine refused to return results."""

    CONSENT = "consent"
    CAPTCHA = "captcha"


def detect_block(html: str) -> BlockReason | None:
    """Detect whether a page is an interstitial rather than a results page.

    Args:
        html: The raw HTML of the page Google returned.

    Returns:
        The reason if the page is a block, otherwise ``None``.
    """
    lowered = html.lower()
    if any(marker in lowered for marker in _CAPTCHA_MARKERS):
        return BlockReason.CAPTCHA
    if any(marker in lowered for marker in _CONSENT_MARKERS):
        return BlockReason.CONSENT
    return None


def unwrap_google_redirect(href: str) -> str | None:
    """Turn a Google result href into the destination URL.

    Google sometimes links results through ``/url?q=<target>``. Internal
    navigation links and anything that is not an absolute http(s) URL are
    rejected by returning ``None``.
    """
    if not href:
        return None

    parts = urlsplit(href)

    if parts.path == "/url" or parts.path.endswith("/url"):
        target = parse_qs(parts.query).get("q", [])
        return target[0] if target else None

    if not parts.scheme:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    if parts.netloc.endswith("google.com") and parts.path.startswith(_INTERNAL_PREFIXES):
        return None

    return href


def _first_text(node: Node, selectors: tuple[str, ...]) -> str:
    """Return the text of the first matching descendant, or an empty string."""
    for selector in selectors:
        found = node.css_first(selector)
        if found is not None:
            text = found.text(strip=True)
            if text:
                return text
    return ""


def _parse_result(node: Node) -> tuple[str, str, str] | None:
    """Extract ``(title, url, snippet)`` from one result container."""
    link = node.css_first("a[href]")
    if link is None:
        return None

    url = unwrap_google_redirect(link.attributes.get("href") or "")
    if url is None:
        return None

    title = _first_text(node, _TITLE_SELECTORS)
    if not title:
        return None

    snippet = _first_text(node, _SNIPPET_SELECTORS)[: constants.MAX_SNIPPET_CHARS]
    return title, url, snippet


def parse_google_serp(
    html: str,
    *,
    max_results: int = constants.MAX_RESULTS_PER_QUERY,
    raise_on_block: bool = False,
) -> list[SearchResult]:
    """Parse organic results out of a Google results page.

    Args:
        html: The raw HTML of the results page.
        max_results: Stop after this many results.
        raise_on_block: Raise when the page is a consent wall or CAPTCHA rather
            than returning an empty list.

    Returns:
        Ranked organic results, de-duplicated by URL.

    Raises:
        SearchBlockedError: ``raise_on_block`` is set and the page is a block.
    """
    if not html.strip():
        return []

    if raise_on_block:
        block = detect_block(html)
        if block is BlockReason.CAPTCHA:
            raise SearchBlockedError(
                "Search engine served a CAPTCHA",
                detail="Google returned an anti-bot challenge instead of results.",
            )
        if block is BlockReason.CONSENT:
            raise SearchBlockedError(
                "Search engine served a consent wall",
                detail="Google returned a cookie consent interstitial instead of results.",
            )

    tree = HTMLParser(html)
    results: list[SearchResult] = []
    seen: set[str] = set()

    for selector in _RESULT_SELECTORS:
        for node in tree.css(selector):
            parsed = _parse_result(node)
            if parsed is None:
                continue
            title, url, snippet = parsed
            if url in seen:
                continue
            seen.add(url)
            results.append(
                SearchResult(title=title, url=url, snippet=snippet, rank=len(results) + 1)
            )
            if len(results) >= max_results:
                return results
        if results:
            break

    return results
