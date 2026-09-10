"""The in-memory job store.

TTL behaviour is tested against a fake clock rather than by sleeping, reusing
the pattern already proven for TTLCache.
"""

import asyncio

import pytest

from app.services.jobs.base import Job, JobStatus
from app.services.jobs.memory import InMemoryJobStore


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def store(clock):
    return InMemoryJobStore(retention_seconds=100.0, max_jobs=5, clock=clock)


def finished(kind="scrape", *, status=JobStatus.SUCCEEDED, at=1000.0, job_id=None):
    job = Job.create(kind, now=at)
    if job_id:
        job.id = job_id
    job.status = status
    job.started_at = at
    job.finished_at = at
    return job


# --- status ---------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "terminal"),
    [
        (JobStatus.QUEUED, False),
        (JobStatus.RUNNING, False),
        (JobStatus.SUCCEEDED, True),
        (JobStatus.FAILED, True),
        (JobStatus.CANCELLED, True),
    ],
)
def test_terminal_states(status, terminal):
    assert status.is_terminal is terminal


def test_created_jobs_start_queued_with_a_unique_id():
    a, b = Job.create("scrape"), Job.create("scrape")
    assert a.status is JobStatus.QUEUED
    assert a.id != b.id
    assert a.created_at > 0


def test_duration_is_none_until_finished():
    job = Job.create("scrape")
    assert job.duration_seconds is None
    job.started_at = 10.0
    assert job.duration_seconds is None
    job.finished_at = 12.5
    assert job.duration_seconds == 2.5


# --- basic storage --------------------------------------------------------- #


async def test_put_and_get_round_trip(store):
    job = Job.create("search")
    await store.put(job)
    assert (await store.get(job.id)) is job


async def test_get_unknown_returns_none(store):
    assert await store.get("nope") is None


async def test_put_replaces_an_existing_job(store):
    job = Job.create("search")
    await store.put(job)
    job.status = JobStatus.RUNNING
    await store.put(job)
    assert (await store.get(job.id)).status is JobStatus.RUNNING


async def test_delete_removes_and_reports(store):
    job = Job.create("search")
    await store.put(job)
    assert await store.delete(job.id) is True
    assert await store.delete(job.id) is False
    assert await store.get(job.id) is None


# --- listing --------------------------------------------------------------- #


async def test_list_is_newest_first(store, clock):
    for i in range(3):
        await store.put(Job.create(f"kind{i}", now=1000.0 + i))
    jobs = await store.list()
    assert [j.kind for j in jobs] == ["kind2", "kind1", "kind0"]


async def test_list_filters_by_status(store):
    await store.put(Job.create("a"))
    await store.put(finished("b"))
    assert [j.kind for j in await store.list(status=JobStatus.SUCCEEDED)] == ["b"]
    assert [j.kind for j in await store.list(status=JobStatus.QUEUED)] == ["a"]


async def test_list_honours_the_limit(store):
    for i in range(4):
        await store.put(Job.create("k", now=1000.0 + i))
    assert len(await store.list(limit=2)) == 2


async def test_list_of_an_empty_store(store):
    assert await store.list() == []


# --- expiry ---------------------------------------------------------------- #


async def test_finished_jobs_expire_after_the_retention_window(store, clock):
    job = finished(at=clock.now)
    await store.put(job)
    assert await store.get(job.id) is not None

    clock.advance(100.0)
    assert await store.get(job.id) is None


async def test_finished_jobs_survive_within_the_window(store, clock):
    job = finished(at=clock.now)
    await store.put(job)
    clock.advance(99.0)
    assert await store.get(job.id) is not None


async def test_running_jobs_never_expire(store, clock):
    job = Job.create("scrape", now=clock.now)
    job.status = JobStatus.RUNNING
    await store.put(job)
    clock.advance(10_000.0)
    assert await store.get(job.id) is not None


async def test_zero_retention_disables_expiry(clock):
    store = InMemoryJobStore(retention_seconds=0, max_jobs=10, clock=clock)
    job = finished(at=clock.now)
    await store.put(job)
    clock.advance(10_000.0)
    assert await store.get(job.id) is not None


async def test_a_terminal_job_without_a_finish_time_expires_from_creation(store, clock):
    job = Job.create("scrape", now=clock.now)
    job.status = JobStatus.CANCELLED
    await store.put(job)
    clock.advance(100.0)
    assert await store.get(job.id) is None


async def test_expired_jobs_disappear_from_listings(store, clock):
    await store.put(finished(at=clock.now))
    clock.advance(100.0)
    assert await store.list() == []


# --- eviction -------------------------------------------------------------- #


async def test_oldest_finished_jobs_are_evicted_at_the_cap(store, clock):
    for i in range(6):
        await store.put(finished(f"kind{i}", at=clock.now + i))
    jobs = await store.list()
    assert len(jobs) == 5
    assert "kind0" not in [j.kind for j in jobs]


async def test_running_jobs_are_never_evicted(clock):
    """Losing an in-flight job would leave a caller polling a vanished id."""
    store = InMemoryJobStore(retention_seconds=1000.0, max_jobs=2, clock=clock)

    running = Job.create("running-work", now=clock.now)
    running.status = JobStatus.RUNNING
    await store.put(running)

    for i in range(5):
        await store.put(finished(f"done{i}", at=clock.now + i + 1))

    assert await store.get(running.id) is not None


async def test_a_store_of_only_running_jobs_exceeds_the_cap_rather_than_lose_work(clock):
    store = InMemoryJobStore(retention_seconds=1000.0, max_jobs=2, clock=clock)
    ids = []
    for i in range(4):
        job = Job.create(f"k{i}", now=clock.now + i)
        job.status = JobStatus.RUNNING
        ids.append(job.id)
        await store.put(job)
    for job_id in ids:
        assert await store.get(job_id) is not None


# --- concurrency ----------------------------------------------------------- #


async def test_concurrent_writes_do_not_lose_jobs(store):
    jobs = [Job.create(f"k{i}") for i in range(5)]
    await asyncio.gather(*(store.put(j) for j in jobs))
    assert len(await store.list()) == 5
