"""Health and readiness response models."""

from __future__ import annotations

from app.schemas.common import StrictModel


class HealthResponse(StrictModel):
    """Liveness payload."""

    status: str
    service: str
    version: str
    uptime_seconds: float


class ReadinessComponent(StrictModel):
    """State of one dependency the service needs to do real work."""

    name: str
    ready: bool
    detail: str


class ReadinessResponse(StrictModel):
    """Readiness payload aggregating every dependency."""

    status: str
    ready: bool
    components: list[ReadinessComponent]
