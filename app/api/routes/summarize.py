"""Direct summarisation of caller-supplied text."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api import deps
from app.api.jobs_support import accepted_response
from app.schemas.jobs import JobAccepted
from app.schemas.scrape import SummarizeRequest, SummarizeResponse
from app.services.jobs.runner import JobRunner
from app.services.keyring.caller import Caller
from app.services.llm.registry import ModelRegistry
from app.services.llm.summarizer import Summarizer
from app.services.pipelines import run_summarize
from app.services.preferences import Preferences

router = APIRouter(prefix="/v1", tags=["summarize"])


@router.post(
    "/summarize",
    response_model=None,
    summary="Summarise text",
    responses={
        200: {"model": SummarizeResponse, "description": "Completed synchronously."},
        202: {"model": JobAccepted, "description": "Queued as a background job."},
    },
)
async def summarize(
    request: SummarizeRequest,
    summarizer: Annotated[Summarizer, Depends(deps.get_summarizer)],
    runner: Annotated[JobRunner, Depends(deps.get_job_runner)],
    registry: Annotated[ModelRegistry, Depends(deps.get_registry)],
    caller: Annotated[Caller | None, Depends(deps.get_caller)],
    preferences: Annotated[Preferences, Depends(deps.get_preferences)],
) -> SummarizeResponse | JSONResponse:
    """Summarise text the caller already has.

    Exists so an MCP server or another service can reuse the summarisation
    pipeline without going through scraping.

    With ``background`` (or ``async``) set, returns 202 with a job id to poll.
    """
    if request.background:
        auth = await deps.resolve_job_auth(registry, request.model, caller, preferences)

        async def work() -> dict[str, object]:
            response = await run_summarize(
                request,
                summarizer=summarizer,
                caller=caller,
                auth=auth,
                preferences=preferences,
            )
            return response.model_dump(mode="json")

        return accepted_response(
            await runner.submit(
                "summarize", work, retention_seconds=preferences.job_retention_seconds
            )
        )

    return await run_summarize(
        request, summarizer=summarizer, caller=caller, preferences=preferences
    )
