import asyncio

from app.core.concurrency import HostLimiter, bounded_gather


async def test_results_preserve_input_order():
    async def make(value, delay):
        await asyncio.sleep(delay)
        return value

    factories = [
        lambda: make("slow", 0.03),
        lambda: make("fast", 0.0),
        lambda: make("mid", 0.01),
    ]
    assert await bounded_gather(factories, limit=3) == ["slow", "fast", "mid"]


async def test_limit_is_respected():
    in_flight = 0
    peak = 0

    async def work():
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return peak

    await bounded_gather([work for _ in range(10)], limit=3)
    assert peak <= 3


async def test_empty_input_returns_empty_list():
    assert await bounded_gather([], limit=2) == []


def test_host_limiter_reuses_one_semaphore_per_host():
    limiter = HostLimiter(2)
    first = limiter.for_url("https://example.com/a")
    second = limiter.for_url("https://EXAMPLE.com/b?q=1")
    assert first is second


def test_host_limiter_separates_distinct_hosts():
    limiter = HostLimiter(2)
    assert limiter.for_url("https://a.com/") is not limiter.for_url("https://b.com/")
