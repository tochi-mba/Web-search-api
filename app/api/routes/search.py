"""The search endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api import deps
from app.api.jobs_support import accepted_response
from app.schemas.jobs import JobAccepted
from app.schemas.search import SearchRequest, SearchResponse
from app.services.fetch.page import PageFetcher
from app.services.jobs.runner import JobRunner
from app.services.llm.summarizer import Summarizer
from app.services.pipelines import run_search
from app.services.search.router import SearchRouter

router = APIRouter(prefix="/v1", tags=["search"])


@router.post(
    "/search",
    response_model=None,
    summary="Search and summarise",
    responses={
        200: {"model": SearchResponse, "description": "Completed synchronously."},
        202: {"model": JobAccepted, "description": "Queued as a background job."},
    },
)
async def search(
    request: SearchRequest,
    search_router: Annotated[SearchRouter, Depends(deps.get_search_router)],
    fetcher: Annotated[PageFetcher, Depends(deps.get_page_fetcher)],
    summarizer: Annotated[Summarizer, Depends(deps.get_summarizer)],
    concurrency: Annotated[int, Depends(deps.get_max_concurrency)],
    runner: Annotated[JobRunner, Depends(deps.get_job_runner)],
) -> SearchResponse | JSONResponse:
    """Run a batch of search queries and optionally summarise what comes back.

    By default the summary is built from result titles and snippets, which is
    fast and cheap. Set ``fetch_pages`` to also scrape the top result pages and
    summarise their full text instead.

    One failing query never fails the batch: each result carries its own status.

    With ``background`` (or ``async``) set, returns 202 with a job id to poll
    instead of holding the connection open for the whole batch.
    """

    async def work() -> dict[str, object]:
        response = await run_search(
            request,
            search_router=search_router,
            fetcher=fetcher,
            summarizer=summarizer,
            concurrency=concurrency,
        )
        return response.model_dump(mode="json")

    if request.background:
        return accepted_response(await runner.submit("search", work))

    return await run_search(
        request,
        search_router=search_router,
        fetcher=fetcher,
        summarizer=summarizer,
        concurrency=concurrency,
    )
