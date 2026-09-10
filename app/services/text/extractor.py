"""Readable-content extraction from raw HTML.

Uses selectolax, a very fast C-backed HTML parser, and a density heuristic in
the spirit of Readability: prefer semantic containers when the page provides
them, fall back to the densest block otherwise, and always strip the elements
that are never content.
"""

from __future__ import annotations

from dataclasses import dataclass

from selectolax.parser import HTMLParser, Node

from app.services.text.cleaner import clean_text

#: Elements that never contain readable content.
_STRIP_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "iframe",
    "form",
    "button",
    "nav",
    "header",
    "footer",
    "aside",
)

#: Containers checked in order; the first one with real text wins.
_CONTENT_SELECTORS = (
    "article",
    "main",
    '[role="main"]',
    "#content",
    ".post-content",
    ".article-body",
    ".entry-content",
    "body",
)

#: Meta tags consulted for the author, in order of preference.
_AUTHOR_META = (
    'meta[name="author"]',
    'meta[property="article:author"]',
    'meta[name="twitter:creator"]',
)

#: Meta tags consulted for the publication date, in order of preference.
_DATE_META = (
    'meta[property="article:published_time"]',
    'meta[name="date"]',
    'meta[name="pubdate"]',
    'meta[itemprop="datePublished"]',
)

#: Below this many characters a container is treated as having no real content.
_MIN_CONTENT_CHARS = 25


@dataclass(frozen=True, slots=True)
class ExtractedContent:
    """Readable content and metadata recovered from an HTML document."""

    url: str
    title: str | None
    author: str | None
    published: str | None
    text: str

    @property
    def word_count(self) -> int:
        """Approximate number of words in the extracted text."""
        return len(self.text.split())


def _first_meta(tree: HTMLParser, selectors: tuple[str, ...]) -> str | None:
    """Return the first non-empty ``content`` attribute among ``selectors``."""
    for selector in selectors:
        node = tree.css_first(selector)
        if node is not None:
            value = (node.attributes.get("content") or "").strip()
            if value:
                return value
    return None


def _extract_author(tree: HTMLParser) -> str | None:
    """Recover the author from meta tags, then from a visible byline."""
    author = _first_meta(tree, _AUTHOR_META)
    if author:
        return author
    for selector in (".byline", ".author", '[rel="author"]'):
        node = tree.css_first(selector)
        if node is not None:
            text = node.text(strip=True)
            if text:
                return text
    return None


def _extract_published(tree: HTMLParser) -> str | None:
    """Recover the publication date from meta tags, then from a ``time`` element."""
    published = _first_meta(tree, _DATE_META)
    if published:
        return published
    node = tree.css_first("time[datetime]")
    if node is not None:
        value = (node.attributes.get("datetime") or "").strip()
        if value:
            return value
    return None


def _strip_non_content(tree: HTMLParser) -> None:
    """Remove elements that never carry readable content."""
    for tag in _STRIP_TAGS:
        for node in tree.css(tag):
            node.decompose()


def _best_container(tree: HTMLParser) -> Node | None:
    """Return the most promising content container in the document."""
    for selector in _CONTENT_SELECTORS:
        node = tree.css_first(selector)
        if node is not None and len(node.text(strip=True)) >= _MIN_CONTENT_CHARS:
            return node
    return None


def extract_content(html: str, *, url: str) -> ExtractedContent:
    """Extract readable text and metadata from an HTML document.

    Args:
        html: The raw HTML source.
        url: The URL the document came from, echoed back on the result.

    Returns:
        The extracted content. ``text`` is empty when the page has nothing
        readable, which callers treat as a reason to try a rendered fetch.
    """
    if not html.strip():
        return ExtractedContent(url=url, title=None, author=None, published=None, text="")

    tree = HTMLParser(html)

    title_node = tree.css_first("title")
    title = title_node.text(strip=True) if title_node is not None else None
    author = _extract_author(tree)
    published = _extract_published(tree)

    _strip_non_content(tree)
    container = _best_container(tree)
    raw_text = container.text(separator="\n") if container is not None else ""

    return ExtractedContent(
        url=url,
        title=title or None,
        author=author,
        published=published,
        text=clean_text(raw_text),
    )
