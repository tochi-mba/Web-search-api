"""Running work in the background and recording what happened.

Callers submit an awaitable factory and get a job id back immediately. The work
then runs detached from the request that started it, which is the whole point:
a twenty-URL scrape should not hold an HTTP connection open for minutes.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.errors import DomainError, NotFoundError
from app.core.logging import get_logger, request_id_var
from app.services.jobs.base import Job, JobStatus, JobStore

logger = get_logger(__name__)

#: Work that produces something JSON-serialisable.
Work = Callable[[], Awaitable[Any]]

#: Error recorded when work fails for a reason we did not anticipate. The real
#: exception is logged; the caller is told nothing that could leak internals.
_INTERNAL_ERROR = {
    "code": "internal_error",
    "title": "Internal server error",
    "detail": "The job failed unexpectedly.",
}


class JobRunner:
    """Executes submitted work as detached tasks and records the outcome."""

    def __init__(
        self,
        store: JobStore,
        *,
        max_concurrent: int = 4,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create the runner.

        Args:
            store: Where job state is kept.
            max_concurrent: How many jobs may run at once. Background work is
                bounded so it cannot starve synchronous requests or exhaust the
                shared browser.
            clock: Time source, injectable for tests.
        """
        self._store = store
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._clock = clock
        self._tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def in_flight(self) -> int:
        """How many jobs are currently submitted but not finished."""
        return len(self._tasks)

    async def submit(self, kind: str, work: Work, *, retention_seconds: float | None = None) -> Job:
        """Queue ``work`` and return its job straight away.

        Args:
            kind: Which operation this is, e.g. ``"scrape"``.
            work: Zero-argument callable returning an awaitable. Its result must
                be JSON-serialisable, since it is stored and returned verbatim.
            retention_seconds: How long this finished job stays readable. ``None``
                uses the store's default, which is the deployment-wide window.

        Returns:
            The freshly created job, in ``queued`` state.
        """
        job = Job.create(kind, now=self._clock(), retention_seconds=retention_seconds)
        await self._store.put(job)

        task = asyncio.create_task(self._execute(job, work, request_id_var.get()))
        # A task referenced only by the event loop can be garbage-collected
        # mid-flight, so the runner holds its own reference until it finishes.
        self._tasks[job.id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job.id, None))

        logger.info("job.submitted", job_id=job.id, kind=kind)
        return job

    async def _execute(self, job: Job, work: Work, request_id: str | None) -> None:
        """Run one job to completion, recording whatever happens."""
        token = request_id_var.set(request_id)
        try:
            async with self._semaphore:
                job.status = JobStatus.RUNNING
                job.started_at = self._clock()
                await self._store.put(job)

                try:
                    result = await work()
                except asyncio.CancelledError:
                    await self._finish(job, JobStatus.CANCELLED)
                    raise
                except DomainError as exc:
                    logger.info("job.failed", job_id=job.id, kind=job.kind, code=exc.code)
                    await self._finish(job, JobStatus.FAILED, error=exc.to_error_payload())
                    return
                except Exception:
                    logger.exception("job.crashed", job_id=job.id, kind=job.kind)
                    await self._finish(job, JobStatus.FAILED, error=dict(_INTERNAL_ERROR))
                    return

                await self._finish(job, JobStatus.SUCCEEDED, result=result)
                logger.info(
                    "job.succeeded",
                    job_id=job.id,
                    kind=job.kind,
                    duration_seconds=job.duration_seconds,
                )
        finally:
            request_id_var.reset(token)

    async def _finish(
        self,
        job: Job,
        status: JobStatus,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, str] | None = None,
    ) -> None:
        """Record a terminal state and persist it."""
        job.status = status
        job.finished_at = self._clock()
        job.result = result
        job.error = error
        await self._store.put(job)

    async def cancel(self, job_id: str) -> Job:
        """Cancel a queued or running job.

        Raises:
            NotFoundError: No such job, or it has already expired.
        """
        job = await self._store.get(job_id)
        if job is None:
            raise NotFoundError(
                "Unknown job", detail=f"No job with id '{job_id}' - it may have expired."
            )

        if job.status.is_terminal:
            return job

        task = self._tasks.get(job_id)
        if task is not None:
            task.cancel()

        await self._finish(job, JobStatus.CANCELLED)
        logger.info("job.cancelled", job_id=job_id)
        return job

    async def get(self, job_id: str) -> Job:
        """Return a job.

        Raises:
            NotFoundError: No such job, or it has already expired.
        """
        job = await self._store.get(job_id)
        if job is None:
            raise NotFoundError(
                "Unknown job", detail=f"No job with id '{job_id}' - it may have expired."
            )
        return job

    async def wait_for_terminal(self, job_id: str, *, timeout: float) -> Job:  # noqa: ASYNC109
        """Return the job once it is terminal, or as it stands when ``timeout`` elapses.

        Raises:
            NotFoundError: No such job, or it has already expired.
        """
        job = await self.get(job_id)
        if job.status.is_terminal or timeout <= 0:
            return job
        waited = await self._store.wait_for_terminal(job_id, timeout=timeout)
        if waited is None:
            raise NotFoundError(
                "Unknown job", detail=f"No job with id '{job_id}' - it may have expired."
            )
        return waited

    async def list(self, *, status: JobStatus | None = None, limit: int = 50) -> list[Job]:
        """Return recent jobs, newest first."""
        return await self._store.list(status=status, limit=limit)

    async def aclose(self) -> None:
        """Cancel everything in flight so shutdown leaves no job stuck running."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info("jobs.shutdown_cancelled", count=len(tasks))
