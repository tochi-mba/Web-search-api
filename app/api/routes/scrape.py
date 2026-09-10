"""The scrape endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api import deps
from app.api.jobs_support import accepted_response
from app.schemas.jobs import JobAccepted
from app.schemas.scrape import ScrapeRequest, ScrapeResponse
from app.services.fetch.page import PageFetcher
from app.services.jobs.runner import JobRunner
from app.services.llm.summarizer import Summarizer
from app.services.pipelines import run_scrape

router = APIRouter(prefix="/v1", tags=["scrape"])


@router.post(
    "/scrape",
    response_model=None,
    summary="Scrape and summarise URLs",
    responses={
        200: {"model": ScrapeResponse, "description": "Completed synchronously."},
        202: {"model": JobAccepted, "description": "Queued as a background job."},
    },
)
async def scrape(
    request: ScrapeRequest,
    fetcher: Annotated[PageFetcher, Depends(deps.get_page_fetcher)],
    summarizer: Annotated[Summarizer, Depends(deps.get_summarizer)],
    concurrency: Annotated[int, Depends(deps.get_max_concurrency)],
    runner: Annotated[JobRunner, Depends(deps.get_job_runner)],
) -> ScrapeResponse | JSONResponse:
    """Fetch each URL, extract its readable content and optionally summarise.

    One unreachable URL never fails the batch: each result carries its own
    status, and the response is still 200.

    With ``background`` (or ``async``) set, returns 202 with a job id to poll
    instead of holding the connection open for the whole batch.
    """

    async def work() -> dict[str, object]:
        response = await run_scrape(
            request, fetcher=fetcher, summarizer=summarizer, concurrency=concurrency
        )
        return response.model_dump(mode="json")

    if request.background:
        return accepted_response(await runner.submit("scrape", work))

    return await run_scrape(
        request, fetcher=fetcher, summarizer=summarizer, concurrency=concurrency
    )
