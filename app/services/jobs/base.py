"""Job types and the storage seam.

Job state lives behind a protocol so the in-memory implementation can be
swapped for a shared one (Redis, a database) without anything above this layer
changing. That matters because the in-memory store is deliberately limited:
jobs live in the process that accepted them.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class JobStatus(StrEnum):
    """Where a job is in its lifecycle."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Whether no further transition is possible."""
        return self in _TERMINAL


_TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})


@dataclass
class Job:
    """One unit of background work and its outcome."""

    id: str
    kind: str
    status: JobStatus = JobStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    """The response the synchronous endpoint would have returned."""

    error: dict[str, str] | None = None
    """The compact error object used by batch items, when the job failed."""

    retention_seconds: float | None = None
    """How long this finished job stays readable. ``None`` uses the store default.

    Computed from the owner's settings at submit time and carried on the job,
    because the reaper has no user token with which to ask settings-api again.
    """

    @staticmethod
    def create(
        kind: str, *, now: float | None = None, retention_seconds: float | None = None
    ) -> Job:
        """Build a fresh queued job."""
        return Job(
            id=uuid.uuid4().hex,
            kind=kind,
            status=JobStatus.QUEUED,
            created_at=now if now is not None else time.time(),
            retention_seconds=retention_seconds,
        )

    @property
    def duration_seconds(self) -> float | None:
        """How long the job ran, once it has finished."""
        if self.started_at is None or self.finished_at is None:
            return None
        return round(self.finished_at - self.started_at, 3)


@runtime_checkable
class JobStore(Protocol):
    """Where job state is kept."""

    async def put(self, job: Job) -> None:
        """Insert or replace a job."""
        ...

    async def get(self, job_id: str) -> Job | None:
        """Return a job, or ``None`` if it is unknown or has expired."""
        ...

    async def list(self, *, status: JobStatus | None = None, limit: int = 50) -> list[Job]:
        """Return recent jobs, newest first."""
        ...

    async def delete(self, job_id: str) -> bool:
        """Remove a job. Returns whether it existed."""
        ...
