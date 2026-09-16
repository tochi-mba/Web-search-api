"""Liveness and readiness endpoints."""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app import __version__
from app.api import deps
from app.config import Settings
from app.schemas.health import HealthResponse, ReadinessComponent, ReadinessResponse

router = APIRouter(tags=["health"])

#: Process start time, used to report uptime.
_STARTED_AT = time.monotonic()


def uptime_seconds() -> float:
    """Seconds since this module was imported (process start, in practice)."""
    return round(time.monotonic() - _STARTED_AT, 3)


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
@router.get("/healthy", response_model=HealthResponse, include_in_schema=False)
async def health(
    settings: Annotated[Settings, Depends(deps.get_settings_dep)],
) -> HealthResponse:
    """Report that the process is up.

    Deliberately does no I/O: an orchestrator uses this to decide whether to
    restart the container, and a slow dependency must not trigger that.
    """
    return HealthResponse(
        status="ok",
        service=settings.service_name,
        version=__version__,
        uptime_seconds=uptime_seconds(),
    )


@router.get("/health/ready", response_model=ReadinessResponse, summary="Readiness probe")
@router.get("/ready", response_model=ReadinessResponse, include_in_schema=False)
async def ready(
    response: Response,
    components: Annotated[list[ReadinessComponent], Depends(deps.get_readiness_components)],
) -> ReadinessResponse:
    """Report whether the service can currently serve real traffic."""
    all_ready = all(component.ready for component in components)
    response.status_code = 200 if all_ready else 503
    return ReadinessResponse(
        status="ok" if all_ready else "degraded",
        ready=all_ready,
        components=components,
    )
