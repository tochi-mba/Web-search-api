"""In-process job storage.

Deliberately simple, and deliberately bounded in two directions: finished jobs
expire after a retention window, and the store evicts the oldest finished jobs
once it hits a hard cap. Without both, a long-running server accumulates job
records until it falls over.

Running jobs are never evicted - losing the record of work still in flight
would leave a caller polling an id that has silently vanished.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from app.core.logging import get_logger
from app.services.jobs.base import Job, JobStatus

logger = get_logger(__name__)

Clock = Callable[[], float]


class InMemoryJobStore:
    """Keeps jobs in a dictionary, with TTL expiry and a size cap."""

    def __init__(
        self,
        *,
        retention_seconds: float = 3600.0,
        max_jobs: int = 1000,
        clock: Clock = time.time,
    ) -> None:
        """Create the store.

        Args:
            retention_seconds: How long a finished job stays readable.
            max_jobs: Hard cap before the oldest finished jobs are evicted.
            clock: Time source, injectable so TTL tests need no sleeping.
        """
        self._retention = retention_seconds
        self._max_jobs = max_jobs
        self._clock = clock
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()

    async def put(self, job: Job) -> None:
        """Insert or replace a job, purging anything stale first."""
        async with self._lock:
            self._purge_locked()
            self._jobs[job.id] = job
            self._evict_locked()

    async def get(self, job_id: str) -> Job | None:
        """Return a job, or ``None`` if unknown or expired."""
        async with self._lock:
            self._purge_locked()
            return self._jobs.get(job_id)

    async def list(self, *, status: JobStatus | None = None, limit: int = 50) -> list[Job]:
        """Return recent jobs, newest first."""
        async with self._lock:
            self._purge_locked()
            jobs = [j for j in self._jobs.values() if status is None or j.status is status]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    async def delete(self, job_id: str) -> bool:
        """Remove a job. Returns whether it existed."""
        async with self._lock:
            return self._jobs.pop(job_id, None) is not None

    def _expired(self, job: Job) -> bool:
        """Whether a finished job has outlived its retention window."""
        if not job.status.is_terminal or self._retention <= 0:
            return False
        finished = job.finished_at if job.finished_at is not None else job.created_at
        return (self._clock() - finished) >= self._retention

    def _purge_locked(self) -> None:
        """Drop expired jobs. Caller must hold the lock."""
        expired = [job_id for job_id, job in self._jobs.items() if self._expired(job)]
        for job_id in expired:
            del self._jobs[job_id]
        if expired:
            logger.debug("jobs.purged", count=len(expired))

    def _evict_locked(self) -> None:
        """Enforce the size cap, oldest finished job first. Caller holds the lock."""
        if len(self._jobs) <= self._max_jobs:
            return

        evictable = sorted(
            (job for job in self._jobs.values() if job.status.is_terminal),
            key=lambda j: j.created_at,
        )
        for job in evictable:
            if len(self._jobs) <= self._max_jobs:
                break
            del self._jobs[job.id]
            logger.debug("jobs.evicted", job_id=job.id)
