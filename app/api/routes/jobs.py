"""Inspecting and cancelling background jobs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from app.api import deps
from app.api.mapping import to_job_out
from app.schemas.jobs import JobListResponse, JobOut
from app.services.jobs.base import JobStatus
from app.services.jobs.runner import JobRunner

router = APIRouter(prefix="/v1", tags=["jobs"])

#: Suggested seconds between polls while a job is still running.
POLL_INTERVAL_SECONDS = 2
MAX_WAIT_SECONDS = 60.0


@router.get("/jobs", response_model=JobListResponse, summary="List recent jobs")
async def list_jobs(
    runner: Annotated[JobRunner, Depends(deps.get_job_runner)],
    status: Annotated[JobStatus | None, Query(description="Only jobs in this state.")] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="Maximum jobs to return.")] = 50,
) -> JobListResponse:
    """Return recent jobs, newest first."""
    jobs = await runner.list(status=status, limit=limit)
    return JobListResponse(jobs=[to_job_out(job) for job in jobs])


@router.get("/jobs/{job_id}", response_model=JobOut, summary="Poll a job")
async def get_job(
    job_id: str,
    response: Response,
    runner: Annotated[JobRunner, Depends(deps.get_job_runner)],
    wait_seconds: Annotated[
        float,
        Query(
            ge=0,
            le=MAX_WAIT_SECONDS,
            description=(
                "Seconds to wait for the job to finish before answering. 0 (the "
                "default) answers straight away."
            ),
        ),
    ] = 0.0,
) -> JobOut:
    """Return one job's state, including its result once it has finished.

    While the job is still running a ``Retry-After`` header suggests how long to
    wait before polling again, so callers need not invent a backoff. Pass
    ``wait_seconds`` to hold the request open until the job finishes instead.
    """
    if wait_seconds > 0:
        job = await runner.wait_for_terminal(job_id, timeout=wait_seconds)
    else:
        job = await runner.get(job_id)
    if not job.status.is_terminal:
        response.headers["Retry-After"] = str(POLL_INTERVAL_SECONDS)
    return to_job_out(job)


@router.delete("/jobs/{job_id}", response_model=JobOut, summary="Cancel a job")
async def cancel_job(
    job_id: str,
    runner: Annotated[JobRunner, Depends(deps.get_job_runner)],
) -> JobOut:
    """Cancel a queued or running job.

    Cancelling a job that has already finished is a no-op that returns its
    existing state, so a retried cancel is safe.
    """
    return to_job_out(await runner.cancel(job_id))
