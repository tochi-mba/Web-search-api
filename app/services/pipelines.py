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
from app.services.fetch.page import FetchedPage, PageFetcher
from app.services.keyring.caller import Caller
from app.services.keyring.client import ResolvedAuth
from app.services.llm.summarizer import Summarizer
from app.services.preferences import Preferences
from app.services.search.base import SearchQuery
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
    summarise their full text instead.

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

    if request.fetch_pages:
        await _attach_page_contents(results, request, fetcher, concurrency)

    if request.summarize:
        for result in results:
            if result.status is ItemStatus.OK and result.results:
                await _attach_summary(result, request, summarizer, caller, auth, preferences)

    return SearchResponse(results=results)


async def _attach_page_contents(
    results: list[SearchQueryResult],
    request: SearchRequest,
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
        for item in result.results[: request.max_pages]
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

    async def run() -> SearchQueryResult | DomainError:
        try:
            response = await search_router.search(
                SearchQuery(
                    query=query.query,
                    max_results=query.max_results,
                    site=query.site,
                    language=request.language,
                    region=request.region,
                    safe_search=request.safe_search,
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
            ],
        )

    return run


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

    summary = await summarizer.summarize(
        content,
        model_id=request.model,
        topic=result.query,
        additional_notes=notes,
        sources=[item.url for item in result.results],
        caller=caller,
        auth=auth,
        preferences=preferences,
    )
    result.summary = summary.as_out()


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
        summary = await summarizer.summarize(
            combined,
            model_id=request.model,
            additional_notes=request.additional_notes,
            sources=[r.url for r in successful],
            caller=caller,
            auth=auth,
            preferences=preferences,
        )
        return ScrapeResponse(results=results, summary=summary.as_out())

    for result in successful:
        assert result.page is not None  # noqa: S101 - filtered above
        summary = await summarizer.summarize(
            result.page.text,
            model_id=request.model,
            topic=result.page.title,
            additional_notes=request.additional_notes,
            sources=[result.url],
            caller=caller,
            auth=auth,
            preferences=preferences,
        )
        result.summary = summary.as_out()

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
