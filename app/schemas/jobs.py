"""Request and response models for background jobs."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.schemas.common import ErrorPayload, StrictModel


class JobAccepted(StrictModel):
    """Returned with 202 when work has been queued rather than run inline."""

    job_id: str
    kind: str = Field(description="Which operation was queued: search, scrape or summarize.")
    status: str
    poll_url: str = Field(description="Where to poll for the outcome.")
    created_at: float


class JobOut(StrictModel):
    """A job's current state, and its result once it has finished."""

    id: str
    kind: str
    status: str = Field(description="queued, running, succeeded, failed or cancelled.")
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    duration_seconds: float | None = None
    result: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Present once the job succeeded. Identical to what the synchronous "
            "endpoint would have returned for this kind of request."
        ),
    )
    error: ErrorPayload | None = Field(default=None, description="Present once the job failed.")


class JobListResponse(StrictModel):
    """Recent jobs, newest first."""

    jobs: list[JobOut]
