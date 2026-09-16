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
from app.services.keyring.caller import Caller
from app.services.llm.registry import ModelRegistry
from app.services.llm.summarizer import Summarizer
from app.services.pipelines import run_search
from app.services.preferences import Preferences
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
    registry: Annotated[ModelRegistry, Depends(deps.get_registry)],
    caller: Annotated[Caller | None, Depends(deps.get_caller)],
    preferences: Annotated[Preferences, Depends(deps.get_preferences)],
) -> SearchResponse | JSONResponse:
    """Run a batch of search queries and optionally summarise what comes back.

    By default the summary is built from result titles and snippets, which is
    fast and cheap. Set ``fetch_pages`` to also scrape the top result pages and
    summarise their full text instead.

    One failing query never fails the batch: each result carries its own status.

    With ``background`` (or ``async``) set, returns 202 with a job id to poll
    instead of holding the connection open for the whole batch.
    """
    if request.background:
        # Resolve the credential now, while the caller's token is still fresh,
        # and hand the job the result rather than the token: tokens live
        # minutes and a large batch can outlast one. Search without a summary
        # never talks to a model, so it must not demand disabled_providers.
        auth = None
        if request.summarize:
            auth = await deps.resolve_job_auth(registry, request.model, caller, preferences)

        async def work() -> dict[str, object]:
            response = await run_search(
                request,
                search_router=search_router,
                fetcher=fetcher,
                summarizer=summarizer,
                concurrency=concurrency,
                caller=caller,
                auth=auth,
                preferences=preferences,
            )
            return response.model_dump(mode="json")

        return accepted_response(
            await runner.submit("search", work, retention_seconds=preferences.job_retention_seconds)
        )

    return await run_search(
        request,
        search_router=search_router,
        fetcher=fetcher,
        summarizer=summarizer,
        concurrency=concurrency,
        caller=caller,
        preferences=preferences,
    )
