"""The scrape endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api import deps
from app.api.mapping import to_summary_out
from app.core.concurrency import bounded_gather
from app.core.errors import DomainError
from app.core.logging import get_logger
from app.schemas.common import ItemStatus
from app.schemas.scrape import (
    ExtractedPage,
    ScrapeRequest,
    ScrapeResponse,
    ScrapeResult,
)
from app.services.fetch.page import PageFetcher
from app.services.llm.summarizer import Summarizer

logger = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["scrape"])


@router.post("/scrape", response_model=ScrapeResponse, summary="Scrape and summarise URLs")
async def scrape(
    request: ScrapeRequest,
    fetcher: Annotated[PageFetcher, Depends(deps.get_page_fetcher)],
    summarizer: Annotated[Summarizer, Depends(deps.get_summarizer)],
    concurrency: Annotated[int, Depends(deps.get_max_concurrency)],
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
                ScrapeResult(url=url, status=ItemStatus.ERROR, error=_error_payload(outcome))
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
        )
        return ScrapeResponse(results=results, summary=to_summary_out(summary))

    for result in successful:
        assert result.page is not None  # noqa: S101 - filtered above
        summary = await summarizer.summarize(
            result.page.text,
            model_id=request.model,
            topic=result.page.title,
            additional_notes=request.additional_notes,
            sources=[result.url],
        )
        result.summary = to_summary_out(summary)

    return ScrapeResponse(results=results)


def _make_fetch(fetcher: PageFetcher, url: str, render: str):  # type: ignore[no-untyped-def] # noqa: ANN202
    """Build a coroutine factory that never raises a domain error."""

    async def run() -> object:
        try:
            return await fetcher.fetch(url, render=render)
        except DomainError as exc:
            logger.info("scrape.item_failed", url=url, code=exc.code)
            return exc

    return run


def _error_payload(exc: DomainError):  # type: ignore[no-untyped-def] # noqa: ANN202
    """Render a domain error as the batch-item error object."""
    from app.schemas.common import ErrorPayload

    return ErrorPayload(**exc.to_error_payload())
