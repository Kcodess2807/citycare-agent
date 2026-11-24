"""One retry helper for everything that can be retried: model calls and tool attempts."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from agentcore.config import RetryKind, RetryPolicy

T = TypeVar("T")


def backoff_delay(policy: RetryPolicy, retry_index: int, rng: Callable[[float, float], float] = random.uniform) -> float:
    """Full jitter: a random wait between 0 and min(cap, base * 2**n). `retry_index` starts at 0."""
    return rng(0.0, min(policy.backoff_cap_s, policy.backoff_base_s * 2**retry_index))


async def with_retries(
    attempt: Callable[[int], Awaitable[T]],
    policy: RetryPolicy,
    classify: Callable[[Exception], RetryKind | None],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: Callable[[float, float], float] = random.uniform,
    can_wait: Callable[[float], bool] | None = None,
) -> T:
    """Call `attempt(1)`, then `attempt(2)`, ... until one returns.

    `classify` maps an exception to the kind of failure it is, or None if it must not be retried.
    The last exception is re-raised when the failure is not retryable, its kind is not in
    `policy.on`, the retries are used up, or `can_wait(delay)` says there is no time to wait.
    Cancellation is never swallowed.
    """
    n = 0
    while True:
        try:
            return await attempt(n + 1)
        except Exception as e:
            kind = classify(e)
            if kind is None or kind not in policy.on or n >= policy.max_retries:
                raise
            delay = backoff_delay(policy, n, rng)
            if can_wait is not None and not can_wait(delay):
                raise
            await sleep(delay)
            n += 1
