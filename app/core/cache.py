"""A minimal async TTL cache.

Small enough to own outright: an external dependency would add a lot of surface
area for what amounts to a dictionary plus a clock and a lock.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

T = TypeVar("T")

Clock = Callable[[], float]


class TTLCache(Generic[T]):
    """Single-value cache that recomputes once its entry goes stale.

    Concurrent callers arriving during a refresh wait for the in-flight refresh
    rather than each triggering their own.
    """

    def __init__(self, ttl_seconds: float, *, clock: Clock = time.monotonic) -> None:
        """Create the cache.

        Args:
            ttl_seconds: How long a stored value stays fresh. ``0`` disables caching.
            clock: Monotonic time source, injectable for deterministic tests.
        """
        self._ttl = ttl_seconds
        self._clock = clock
        self._value: T | None = None
        self._stored_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def ttl_seconds(self) -> float:
        """The configured time-to-live."""
        return self._ttl

    def invalidate(self) -> None:
        """Drop any stored value, forcing the next read to recompute."""
        self._value = None

    def peek(self) -> T | None:
        """Return the stored value if it is still fresh, else ``None``."""
        if self._value is None or self._ttl <= 0:
            return None
        if self._clock() - self._stored_at >= self._ttl:
            return None
        return self._value

    async def get(self, factory: Callable[[], Awaitable[T]]) -> T:
        """Return the cached value, computing it with ``factory`` when stale."""
        fresh = self.peek()
        if fresh is not None:
            return fresh
        async with self._lock:
            # Another waiter may have refreshed while we queued on the lock.
            fresh = self.peek()
            if fresh is not None:
                return fresh
            value = await factory()
            self._value = value
            self._stored_at = self._clock()
            return value
