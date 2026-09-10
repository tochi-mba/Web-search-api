"""Direct summarisation of caller-supplied text."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api import deps
from app.api.mapping import to_summary_out
from app.schemas.scrape import SummarizeRequest, SummarizeResponse
from app.services.llm.summarizer import Summarizer

router = APIRouter(prefix="/v1", tags=["summarize"])


@router.post("/summarize", response_model=SummarizeResponse, summary="Summarise text")
async def summarize(
    request: SummarizeRequest,
    summarizer: Annotated[Summarizer, Depends(deps.get_summarizer)],
) -> SummarizeResponse:
    """Summarise text the caller already has.

    Exists so an MCP server or another service can reuse the summarisation
    pipeline without going through scraping.
    """
    summary = await summarizer.summarize(
        request.text,
        model_id=request.model,
        topic=request.topic,
        additional_notes=request.additional_notes,
        sources=list(request.sources) or None,
    )
    return SummarizeResponse(summary=to_summary_out(summary))
