"""Bounded concurrency helpers used by the batch endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

T = TypeVar("T")


async def bounded_gather[T](
    factories: Iterable[Callable[[], Awaitable[T]]],
    *,
    limit: int,
) -> list[T]:
    """Run coroutine factories concurrently, at most ``limit`` at a time.

    Takes factories rather than coroutines so that nothing is scheduled - and
    therefore nothing warns about never being awaited - if the caller bails out.

    Args:
        factories: Zero-argument callables each returning a fresh awaitable.
        limit: Maximum number of awaitables in flight at once.

    Returns:
        Results in the same order as ``factories``.
    """
    semaphore = asyncio.Semaphore(limit)

    async def run(factory: Callable[[], Awaitable[T]]) -> T:
        async with semaphore:
            return await factory()

    return list(await asyncio.gather(*(run(factory) for factory in factories)))
