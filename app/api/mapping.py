"""Conversions from internal dataclasses to API response models."""

from __future__ import annotations

from app.schemas.common import ErrorPayload, Usage
from app.schemas.jobs import JobAccepted, JobOut
from app.schemas.summary import SummaryOut
from app.services.jobs.base import Job
from app.services.llm.summarizer import Summary


def to_summary_out(summary: Summary) -> SummaryOut:
    """Render an internal :class:`Summary` as its API representation."""
    return SummaryOut(
        executive_summary=summary.executive_summary,
        key_points=summary.key_points,
        model=summary.model,
        provider=summary.provider,
        usage=Usage(
            input_tokens=summary.input_tokens,
            output_tokens=summary.output_tokens,
        ),
        truncated=summary.truncated,
        chars_submitted=summary.chars_submitted,
        original_chars=summary.original_chars,
        notes_applied=summary.notes_applied,
        param_adjustments=summary.param_adjustments,
    )


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
