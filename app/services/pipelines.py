"""The actual work behind each endpoint, independent of how it was requested.

Synchronous requests and background jobs run **these same functions**. Keeping
one implementation is the point: two code paths for the same operation would
drift, and only one of them would end up properly tested.

Everything here takes its collaborators explicitly rather than reaching for
application state, so a background job can call them long after the request
that submitted it has returned.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from app import constants
from app.core.concurrency import bounded_gather
from app.core.errors import DomainError
from app.core.logging import get_logger
from app.schemas.common import ErrorPayload, ItemStatus
from app.schemas.scrape import (
    ExtractedPage,
    ScrapeRequest,
    ScrapeResponse,
    ScrapeResult,
    SummarizeRequest,
    SummarizeResponse,
)
from app.schemas.search import (
    SearchQueryIn,
    SearchQueryResult,
    SearchRequest,
    SearchResponse,
    SearchResultOut,
)
from app.schemas.summary import SummaryOut
from app.services.fetch.page import FetchedPage, PageFetcher
from app.services.keyring.caller import Caller
from app.services.keyring.client import ResolvedAuth
from app.services.llm.summarizer import Summarizer
from app.services.preferences import Preferences
from app.services.search.base import SafeSearch, SearchQuery
from app.services.search.router import SearchRouter

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


async def run_search(
    request: SearchRequest,
    *,
    search_router: SearchRouter,
    fetcher: PageFetcher,
    summarizer: Summarizer,
    concurrency: int,
    caller: Caller | None = None,
    auth: ResolvedAuth | None = None,
    preferences: Preferences | None = None,
) -> SearchResponse:
    """Run a batch of search queries and optionally summarise what comes back.

    By default the summary is built from result titles and snippets, which is
    fast and cheap. Set ``fetch_pages`` to also scrape the top result pages and
    summarise their full text instead; a request that does not say reads as many as the
    person's ``search.read_top_pages``.

    One failing query never fails the batch: each result carries its own status.
    """
    outcomes = await bounded_gather(
        [
            _make_query_runner(search_router, query, request, caller, preferences)
            for query in request.queries
        ],
        limit=concurrency,
    )

    results: list[SearchQueryResult] = []
    for query, outcome in zip(request.queries, outcomes, strict=True):
        if isinstance(outcome, DomainError):
            results.append(
                SearchQueryResult(
                    query=query.query,
                    status=ItemStatus.ERROR,
                    error=ErrorPayload(**outcome.to_error_payload()),
                )
            )
            continue
        results.append(outcome)

    per_query = _pages_to_read(request, preferences)
    if per_query:
        await _attach_page_contents(results, per_query, fetcher, concurrency)

    if request.summarize:
        for result in results:
            if result.status is ItemStatus.OK and result.results:
                await _attach_summary(result, request, summarizer, caller, auth, preferences)

    return SearchResponse(results=results)


def _pages_to_read(request: SearchRequest, preferences: Preferences | None) -> int:
    """How many top result pages each query's summary is written from; ``0`` is snippets.

    A request that says wins. One that does not takes the person's ``read_top_pages``, but
    only when it is summarised: that setting is about what a summary is written from, and
    a batch with no summary would scrape pages for nothing.
    """
    chosen = preferences.read_top_pages if preferences is not None else 0
    if request.fetch_pages is None:
        if not (request.summarize and chosen):
            return 0
    elif not request.fetch_pages:
        return 0
    return request.max_pages or chosen or constants.DEFAULT_PAGES_PER_QUERY


async def _attach_page_contents(
    results: list[SearchQueryResult],
    per_query: int,
    fetcher: PageFetcher,
    concurrency: int,
) -> None:
    """Scrape the top result pages so summaries see full text, not snippets.

    A page that cannot be fetched simply keeps its snippet: deep fetching is an
    enrichment, and one dead link should not degrade the whole query.
    """
    targets: list[SearchResultOut] = [
        item
        for result in results
        if result.status is ItemStatus.OK
        for item in result.results[:per_query]
    ]
    if not targets:
        return

    pages = await bounded_gather(
        [_make_page_fetch(fetcher, item.url) for item in targets], limit=concurrency
    )
    for item, text in zip(targets, pages, strict=True):
        if text:
            item.content = text


def _make_page_fetch(fetcher: PageFetcher, url: str) -> Callable[[], Awaitable[str | None]]:
    """Build a coroutine factory returning page text, or None on failure."""

    async def run() -> str | None:
        try:
            page = await fetcher.fetch(url)
        except DomainError as exc:
            logger.info("search.page_fetch_failed", url=url, code=exc.code)
            return None
        return page.content.text or None

    return run


def _make_query_runner(
    search_router: SearchRouter,
    query: SearchQueryIn,
    request: SearchRequest,
    caller: Caller | None,
    preferences: Preferences | None,
) -> Callable[[], Awaitable[SearchQueryResult | DomainError]]:
    """Build a coroutine factory running one query without raising."""

    chosen = preferences.safe_search if preferences is not None else None
    recency = request.recency_days
    if recency is None and preferences is not None:
        recency = preferences.recency_days
    language = request.language or (preferences.language if preferences is not None else None)
    region = request.region or (preferences.region if preferences is not None else None)
    blocked = _blocked_for(query, preferences)

    async def run() -> SearchQueryResult | DomainError:
        try:
            response = await search_router.search(
                SearchQuery(
                    query=query.query,
                    max_results=query.max_results,
                    site=query.site,
                    language=language or "en",
                    region=region or "us",
                    safe_search=SafeSearch.for_request(request.safe_search, chosen=chosen),
                    recency_days=recency,
                ),
                caller=caller,
                preferred=preferences.search_backend if preferences is not None else None,
            )
        except DomainError as exc:
            logger.info("search.query_failed", query=query.query, code=exc.code)
            return exc

        return SearchQueryResult(
            query=query.query,
            status=ItemStatus.OK,
            backend=response.backend,
            results=[
                SearchResultOut(
                    title=item.title, url=item.url, snippet=item.snippet, rank=item.rank
                )
                for item in response.results
                if not (blocked and _within_any(_host(item.url), blocked))
            ],
        )

    return run


def _blocked_for(query: SearchQueryIn, preferences: Preferences | None) -> tuple[str, ...]:
    """The person's blocked domains, less any that cover the site this query names.

    A query restricted to a site has asked for that site's results, and the request wins.
    """
    blocked = preferences.blocked_domains if preferences is not None else ()
    if not query.site:
        return blocked
    site = query.site.strip().lower()
    return tuple(domain for domain in blocked if not _within_any(site, (domain,)))


def _host(url: str) -> str:
    """The lower-cased host ``url`` points at, or empty when it has none."""
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        # A malformed link from a results page is not this person's blocked site, and
        # must not fail the query it came back in.
        return ""


def _within_any(host: str, domains: tuple[str, ...]) -> bool:
    """Whether ``host`` is one of ``domains`` or a subdomain of one."""
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


async def _attach_summary(
    result: SearchQueryResult,
    request: SearchRequest,
    summarizer: Summarizer,
    caller: Caller | None,
    auth: ResolvedAuth | None,
    preferences: Preferences | None,
) -> None:
    """Summarise one query's results in place."""
    query_in = next((q for q in request.queries if q.query == result.query), None)
    notes = (query_in.additional_notes if query_in else None) or request.additional_notes

    content = "\n\n".join(
        f"## {item.title}\n{item.url}\n{item.content or item.snippet}" for item in result.results
    )

    result.summary, result.summary_error = await _summarised(
        summarizer,
        content,
        model_id=request.model,
        topic=result.query,
        additional_notes=notes,
        sources=[item.url for item in result.results],
        caller=caller,
        auth=auth,
        preferences=preferences,
    )


async def _summarised(
    summarizer: Summarizer, content: str, **options: Any
) -> tuple[SummaryOut | None, ErrorPayload | None]:
    """A summary, or why there is none -- never a failure of what was being summarised.

    The model that writes a summary is a dependency the results do not have: a clyde timeout,
    a 502 or a provider's 400 used to fail the whole query, so a search that found ten
    results reported none, and a page that was fetched reported it could not be. The
    contract is that one failing piece never fails the batch; a summary is one piece.
    """
    try:
        summary = await summarizer.summarize(content, **options)
    except DomainError as exc:
        logger.info("summary.failed", topic=options.get("topic"), code=exc.code)
        return None, ErrorPayload(**exc.to_error_payload())
    return summary.as_out(), None


# --------------------------------------------------------------------------- #
# Scrape
# --------------------------------------------------------------------------- #


async def run_scrape(
    request: ScrapeRequest,
    *,
    fetcher: PageFetcher,
    summarizer: Summarizer,
    concurrency: int,
    caller: Caller | None = None,
    auth: ResolvedAuth | None = None,
    preferences: Preferences | None = None,
) -> ScrapeResponse:
    """Fetch each URL, extract its readable content and optionally summarise.

    One unreachable URL never fails the batch: each result carries its own
    status, and the response is still 200.
    """
    pages = await bounded_gather(
        [_make_fetch(fetcher, url, request.render_js.value) for url in request.urls],
        limit=concurrency,
    )

    results: list[ScrapeResult] = []
    for url, outcome in zip(request.urls, pages, strict=True):
        if isinstance(outcome, DomainError):
            results.append(
                ScrapeResult(
                    url=url,
                    status=ItemStatus.ERROR,
                    error=ErrorPayload(**outcome.to_error_payload()),
                )
            )
            continue
        results.append(
            ScrapeResult(
                url=url,
                status=ItemStatus.OK,
                page=ExtractedPage(
                    url=url,
                    final_url=outcome.final_url,
                    title=outcome.content.title,
                    author=outcome.content.author,
                    published=outcome.content.published,
                    text=outcome.content.text,
                    word_count=outcome.content.word_count,
                    rendered=outcome.rendered,
                ),
            )
        )

    if not request.summarize:
        return ScrapeResponse(results=results)

    successful = [r for r in results if r.status is ItemStatus.OK and r.page is not None]

    if request.summarize_together:
        combined = "\n\n".join(
            f"# {r.page.title or r.page.url}\n{r.page.text}" for r in successful if r.page
        )
        together, failed = await _summarised(
            summarizer,
            combined,
            model_id=request.model,
            additional_notes=request.additional_notes,
            sources=[r.url for r in successful],
            caller=caller,
            auth=auth,
            preferences=preferences,
        )
        return ScrapeResponse(results=results, summary=together, summary_error=failed)

    for result in successful:
        assert result.page is not None  # noqa: S101 - filtered above
        result.summary, result.summary_error = await _summarised(
            summarizer,
            result.page.text,
            model_id=request.model,
            topic=result.page.title,
            additional_notes=request.additional_notes,
            sources=[result.url],
            caller=caller,
            auth=auth,
            preferences=preferences,
        )

    return ScrapeResponse(results=results)


def _make_fetch(
    fetcher: PageFetcher, url: str, render: str
) -> Callable[[], Awaitable[FetchedPage | DomainError]]:
    """Build a coroutine factory that never raises a domain error."""

    async def run() -> FetchedPage | DomainError:
        try:
            return await fetcher.fetch(url, render=render)
        except DomainError as exc:
            logger.info("scrape.item_failed", url=url, code=exc.code)
            return exc

    return run


# --------------------------------------------------------------------------- #
# Summarize
# --------------------------------------------------------------------------- #


async def run_summarize(
    request: SummarizeRequest,
    *,
    summarizer: Summarizer,
    caller: Caller | None = None,
    auth: ResolvedAuth | None = None,
    preferences: Preferences | None = None,
) -> SummarizeResponse:
    """Summarise text the caller already has."""
    summary = await summarizer.summarize(
        request.text,
        model_id=request.model,
        topic=request.topic,
        additional_notes=request.additional_notes,
        sources=list(request.sources) or None,
        caller=caller,
        auth=auth,
        preferences=preferences,
    )
    return SummarizeResponse(summary=summary.as_out())
