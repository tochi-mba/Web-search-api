"""Conversions from internal dataclasses to API response models."""

from __future__ import annotations

from app.schemas.common import ErrorPayload
from app.schemas.jobs import JobAccepted, JobOut
from app.services.jobs.base import Job


def to_job_out(job: Job) -> JobOut:
    """Render an internal :class:`Job` as its API representation."""
    return JobOut(
        id=job.id,
        kind=job.kind,
        status=str(job.status),
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        duration_seconds=job.duration_seconds,
        result=job.result,
        error=ErrorPayload(**job.error) if job.error else None,
    )


def to_job_accepted(job: Job) -> JobAccepted:
    """Render the 202 envelope handed back when work is queued."""
    return JobAccepted(
        job_id=job.id,
        kind=job.kind,
        status=str(job.status),
        poll_url=f"/v1/jobs/{job.id}",
        created_at=job.created_at,
    )
