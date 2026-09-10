"""Shared helper for the endpoints that can queue their work."""

from __future__ import annotations

from fastapi.responses import JSONResponse

from app.api.mapping import to_job_accepted
from app.api.routes.jobs import POLL_INTERVAL_SECONDS
from app.services.jobs.base import Job


def accepted_response(job: Job) -> JSONResponse:
    """Render the 202 handed back when work has been queued.

    ``Location`` points at the job so a client can follow it without building
    the URL itself, and ``Retry-After`` suggests a polling interval.
    """
    payload = to_job_accepted(job)
    return JSONResponse(
        status_code=202,
        content=payload.model_dump(mode="json"),
        headers={
            "Location": payload.poll_url,
            "Retry-After": str(POLL_INTERVAL_SECONDS),
        },
    )
