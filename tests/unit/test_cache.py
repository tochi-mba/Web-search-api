import asyncio

import pytest

from app.core.cache import TTLCache


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


async def test_first_call_computes_and_caches(clock):
    cache = TTLCache[int](ttl_seconds=10, clock=clock)
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return 42

    assert await cache.get(factory) == 42
    assert await cache.get(factory) == 42
    assert calls == 1


async def test_value_recomputes_once_stale(clock):
    cache = TTLCache[int](ttl_seconds=10, clock=clock)
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return calls

    assert await cache.get(factory) == 1
    clock.advance(10)
    assert await cache.get(factory) == 2


async def test_zero_ttl_disables_caching(clock):
    cache = TTLCache[int](ttl_seconds=0, clock=clock)
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return calls

    await cache.get(factory)
    await cache.get(factory)
    assert calls == 2


async def test_invalidate_forces_recompute(clock):
    cache = TTLCache[int](ttl_seconds=100, clock=clock)
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return calls

    await cache.get(factory)
    cache.invalidate()
    await cache.get(factory)
    assert calls == 2


async def test_peek_returns_none_when_empty(clock):
    assert TTLCache[int](ttl_seconds=10, clock=clock).peek() is None


async def test_concurrent_callers_share_one_refresh(clock):
    cache = TTLCache[int](ttl_seconds=100, clock=clock)
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return calls

    results = await asyncio.gather(*(cache.get(factory) for _ in range(5)))
    assert calls == 1
    assert results == [1, 1, 1, 1, 1]


def test_ttl_is_exposed(clock):
    assert TTLCache[int](ttl_seconds=7, clock=clock).ttl_seconds == 7
