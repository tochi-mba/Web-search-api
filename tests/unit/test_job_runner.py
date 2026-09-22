"""The job runner.

Fake work is gated with asyncio.Event rather than sleep, so nothing here is
timing-flaky.
"""

import asyncio
import gc

import pytest

from app.core.errors import NotFoundError, UpstreamError
from app.services.jobs.base import Job, JobStatus
from app.services.jobs.memory import InMemoryJobStore
from app.services.jobs.runner import JobRunner


@pytest.fixture
def store():
    return InMemoryJobStore(retention_seconds=1000.0, max_jobs=100)


@pytest.fixture
def runner(store):
    return JobRunner(store, max_concurrent=4)


async def drain(runner):
    """Await every in-flight task rather than polling for them."""
    while runner._tasks:
        await asyncio.gather(*runner._tasks.values(), return_exceptions=True)


def immediate(value):
    async def work():
        return value

    return work


# --- success --------------------------------------------------------------- #


async def test_submit_returns_immediately_in_queued_state(runner):
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return {"done": True}

    job = await runner.submit("scrape", work)
    assert job.status is JobStatus.QUEUED
    assert job.result is None

    gate.set()
    await drain(runner)


async def test_result_is_recorded_on_success(runner):
    job = await runner.submit("scrape", immediate({"results": [1, 2]}))
    await drain(runner)

    stored = await runner.get(job.id)
    assert stored.status is JobStatus.SUCCEEDED
    assert stored.result == {"results": [1, 2]}
    assert stored.error is None
    assert stored.finished_at is not None
    assert stored.duration_seconds is not None


async def test_kind_is_preserved(runner):
    job = await runner.submit("search", immediate({}))
    await drain(runner)
    assert (await runner.get(job.id)).kind == "search"


async def test_submit_carries_the_jobs_own_retention(runner):
    job = await runner.submit("scrape", immediate({}), retention_seconds=12.0)
    await drain(runner)
    assert (await runner.get(job.id)).retention_seconds == 12.0


async def test_status_moves_through_running(runner):
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return {}

    job = await runner.submit("scrape", work)
    await asyncio.sleep(0)
    assert (await runner.get(job.id)).status is JobStatus.RUNNING

    gate.set()
    await drain(runner)
    assert (await runner.get(job.id)).status is JobStatus.SUCCEEDED


async def test_waiting_on_a_finished_job_returns_at_once(runner):
    job = await runner.submit("scrape", immediate({}))
    await drain(runner)
    settled = await runner.wait_for_terminal(job.id, timeout=30)
    assert settled.status is JobStatus.SUCCEEDED


async def test_a_zero_wait_returns_the_job_as_it_stands(runner):
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return {}

    job = await runner.submit("scrape", work)
    await asyncio.sleep(0)
    current = await runner.wait_for_terminal(job.id, timeout=0)
    assert current.status is JobStatus.RUNNING
    gate.set()
    await drain(runner)


async def test_waiting_returns_when_work_finishes(runner):
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return {"done": True}

    job = await runner.submit("scrape", work)
    waiter = asyncio.create_task(runner.wait_for_terminal(job.id, timeout=5))
    await asyncio.sleep(0)
    gate.set()
    assert (await waiter).status is JobStatus.SUCCEEDED


async def test_waiting_on_a_deleted_job_is_not_found(runner, store):
    job = Job.create("scrape")
    await store.put(job)
    waiter = asyncio.create_task(runner.wait_for_terminal(job.id, timeout=5))
    await asyncio.sleep(0)
    await store.delete(job.id)
    with pytest.raises(NotFoundError):
        await waiter


# --- the task-reference trap ----------------------------------------------- #


async def test_jobs_survive_garbage_collection(runner):
    """A task referenced only by the event loop can be collected mid-flight.

    The runner keeps its own reference; this forces a collection while work is
    in flight to prove it.
    """
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return {"survived": True}

    job = await runner.submit("scrape", work)
    await asyncio.sleep(0)

    gc.collect()

    gate.set()
    await drain(runner)
    assert (await runner.get(job.id)).result == {"survived": True}


async def test_completed_tasks_are_released(runner):
    await runner.submit("scrape", immediate({}))
    await drain(runner)
    assert runner.in_flight == 0


# --- failure --------------------------------------------------------------- #


async def test_domain_errors_are_recorded_as_structured_failures(runner):
    async def work():
        raise UpstreamError("Origin exploded", detail="502 from origin")

    job = await runner.submit("scrape", work)
    await drain(runner)

    stored = await runner.get(job.id)
    assert stored.status is JobStatus.FAILED
    assert stored.error == {
        "code": "upstream_error",
        "title": "Origin exploded",
        "detail": "502 from origin",
    }
    assert stored.result is None


async def test_unexpected_errors_never_leak_internals(runner):
    """An unhandled exception must not put its message in front of a caller."""

    async def work():
        raise RuntimeError("connection string with a password in it")

    job = await runner.submit("scrape", work)
    await drain(runner)

    stored = await runner.get(job.id)
    assert stored.status is JobStatus.FAILED
    assert stored.error == {
        "code": "internal_error",
        "title": "Internal server error",
        "detail": "The job failed unexpectedly.",
    }
    assert "password" not in str(stored.error)


async def test_one_failing_job_does_not_affect_another(runner):
    async def boom():
        raise UpstreamError("nope")

    bad = await runner.submit("scrape", boom)
    good = await runner.submit("scrape", immediate({"ok": True}))
    await drain(runner)

    assert (await runner.get(bad.id)).status is JobStatus.FAILED
    assert (await runner.get(good.id)).status is JobStatus.SUCCEEDED


# --- cancellation ---------------------------------------------------------- #


async def test_cancel_stops_a_running_job(runner):
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return {}

    job = await runner.submit("scrape", work)
    await asyncio.sleep(0)

    cancelled = await runner.cancel(job.id)
    assert cancelled.status is JobStatus.CANCELLED
    assert (await runner.get(job.id)).status is JobStatus.CANCELLED


async def test_cancelling_a_finished_job_is_a_no_op(runner):
    job = await runner.submit("scrape", immediate({"ok": True}))
    await drain(runner)

    result = await runner.cancel(job.id)
    assert result.status is JobStatus.SUCCEEDED


async def test_cancelling_an_unknown_job_raises(runner):
    with pytest.raises(NotFoundError, match="Unknown job"):
        await runner.cancel("does-not-exist")


async def test_getting_an_unknown_job_raises(runner):
    with pytest.raises(NotFoundError, match="may have expired"):
        await runner.get("does-not-exist")


# --- bounded concurrency --------------------------------------------------- #


async def test_concurrency_is_bounded(store):
    runner = JobRunner(store, max_concurrent=2)
    active = 0
    peak = 0
    gate = asyncio.Event()

    async def work():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await gate.wait()
        active -= 1
        return {}

    for _ in range(6):
        await runner.submit("scrape", work)
    await asyncio.sleep(0)

    assert peak <= 2

    gate.set()
    await drain(runner)


async def test_queued_jobs_run_once_capacity_frees_up(store):
    runner = JobRunner(store, max_concurrent=1)
    gate = asyncio.Event()
    completed = []

    async def blocking():
        await gate.wait()
        completed.append("first")
        return {}

    first = await runner.submit("scrape", blocking)
    second = await runner.submit("scrape", immediate({"n": 2}))

    await asyncio.sleep(0)
    assert (await runner.get(second.id)).status is JobStatus.QUEUED

    gate.set()
    await drain(runner)
    assert (await runner.get(first.id)).status is JobStatus.SUCCEEDED
    assert (await runner.get(second.id)).result == {"n": 2}


# --- listing and shutdown -------------------------------------------------- #


async def test_list_returns_recent_jobs(runner):
    await runner.submit("scrape", immediate({}))
    await runner.submit("search", immediate({}))
    await drain(runner)
    assert len(await runner.list()) == 2


async def test_list_filters_by_status(runner):
    async def boom():
        raise UpstreamError("nope")

    await runner.submit("scrape", immediate({}))
    await runner.submit("scrape", boom)
    await drain(runner)

    assert len(await runner.list(status=JobStatus.FAILED)) == 1
    assert len(await runner.list(status=JobStatus.SUCCEEDED)) == 1


async def test_shutdown_cancels_in_flight_jobs(runner):
    """A restart must not leave a job stuck at running forever."""
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return {}

    job = await runner.submit("scrape", work)
    await asyncio.sleep(0)

    await runner.aclose()

    assert (await runner.get(job.id)).status is JobStatus.CANCELLED
    assert runner.in_flight == 0


async def test_shutdown_with_nothing_in_flight_is_fine(runner):
    await runner.aclose()
    assert runner.in_flight == 0


async def test_in_flight_is_reported(runner):
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return {}

    await runner.submit("scrape", work)
    assert runner.in_flight == 1
    gate.set()
    await drain(runner)
    assert runner.in_flight == 0


async def test_cancelling_a_job_with_no_live_task_still_marks_it_cancelled(runner, store):
    """An orphaned record - in the store but with no task - must still cancel.

    This is the shape a restored or externally-written job would have; leaving
    it stuck at running would strand anyone polling it.
    """
    from app.services.jobs.base import Job

    orphan = Job.create("scrape")
    orphan.status = JobStatus.RUNNING
    await store.put(orphan)

    cancelled = await runner.cancel(orphan.id)
    assert cancelled.status is JobStatus.CANCELLED
