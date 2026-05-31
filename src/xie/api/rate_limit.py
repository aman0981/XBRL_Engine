"""Tiny per-IP sliding-window rate limiter, in-process.

Used by the Mode C upload endpoint. We keep this simple — no Redis, no slowapi —
because the deploy target is a single Hetzner VM with one app process. If we
ever scale to multiple workers, replace with `slowapi` + Redis.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque


class RateLimitExceeded(RuntimeError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limit hit; retry after {retry_after:.0f}s")
        self.retry_after = retry_after


class SlidingWindowLimiter:
    """At most `limit` events per `window_seconds` per key."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check_and_record(self, key: str, now: float | None = None) -> None:
        ts = now if now is not None else time.monotonic()
        async with self._lock:
            bucket = self._events[key]
            cutoff = ts - self.window
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                retry_after = max(0.0, bucket[0] + self.window - ts)
                raise RateLimitExceeded(retry_after)
            bucket.append(ts)

    async def prune_empty(self) -> int:
        """Drop entries whose deque has aged out completely.

        Long-running deploys with many distinct client IPs would otherwise let
        `_events` grow without bound. Call from a periodic task (or after a
        burst of misses) to reclaim memory.
        """
        async with self._lock:
            to_drop = [k for k, q in self._events.items() if not q]
            for k in to_drop:
                del self._events[k]
            return len(to_drop)

    async def reset(self) -> None:
        """Drop all per-key state. Tests call this between cases."""
        async with self._lock:
            self._events.clear()

    def _peek(self, key: str) -> int:
        return len(self._events.get(key, ()))
