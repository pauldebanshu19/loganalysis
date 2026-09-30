"""Where finished summaries live for an hour.

Only the summary is stored -- a few kilobytes -- and never the log itself.
That is what lets the web UI hand out a shareable result link without the
service holding on to anybody's log contents.

Two implementations behind one interface: Redis for anything with more than one
worker, and an in-process dict for single-process development and tests.  The
dict is deliberately not the default in production, because several workers
each with their own dict would answer ``GET`` inconsistently depending on which
one the load balancer picked.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

from analyzer import AnalysisResult

#: Redis keys are namespaced so the store can share a database with anything
#: else without a chance of collision.
KEY_PREFIX = "analysis:"


@runtime_checkable
class ResultStore(Protocol):
    """Put a result in, fetch it back by id until it expires."""

    async def put(self, result: AnalysisResult) -> None: ...

    async def get(self, analysis_id: str) -> AnalysisResult | None: ...

    async def ping(self) -> bool:
        """Whether the backing store is reachable, for the health endpoint."""
        ...

    async def close(self) -> None: ...


class MemoryResultStore:
    """An in-process store with the same TTL behaviour as the Redis one.

    Expired entries are dropped when they are next read, and a sweep runs when
    the dict grows past a threshold -- enough for development, where the
    process is short-lived and the traffic is one person clicking.
    """

    #: Sweep once the dict passes this many entries, so a long-running dev
    #: server cannot accumulate expired results forever.
    _SWEEP_AT = 512

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._items: dict[str, tuple[float, AnalysisResult]] = {}

    async def put(self, result: AnalysisResult) -> None:
        if len(self._items) >= self._SWEEP_AT:
            self._sweep()
        self._items[result.id] = (time.monotonic() + self._ttl, result)

    async def get(self, analysis_id: str) -> AnalysisResult | None:
        entry = self._items.get(analysis_id)
        if entry is None:
            return None
        expires_at, result = entry
        if expires_at <= time.monotonic():
            self._items.pop(analysis_id, None)
            return None
        return result

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        self._items.clear()

    def _sweep(self) -> None:
        now = time.monotonic()
        for key in [k for k, (expires, _) in self._items.items() if expires <= now]:
            del self._items[key]


class RedisResultStore:
    """Results in Redis, so every worker and every container sees the same set."""

    def __init__(self, url: str, ttl_seconds: int, *, client: object | None = None) -> None:
        if client is None:
            # Imported here so that a deployment running the memory store does
            # not need redis installed to import this module.
            from redis.asyncio import Redis

            client = Redis.from_url(url, decode_responses=True)
        # `client` is a seam for tests, which pass a fake rather than standing
        # up a real server.
        self._redis = client
        self._ttl = ttl_seconds

    async def put(self, result: AnalysisResult) -> None:
        await self._redis.set(
            KEY_PREFIX + result.id, result.model_dump_json(), ex=self._ttl
        )

    async def get(self, analysis_id: str) -> AnalysisResult | None:
        raw = await self._redis.get(KEY_PREFIX + analysis_id)
        if raw is None:
            return None
        return AnalysisResult.model_validate_json(raw)

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:
            # The health endpoint reports this as degraded; it must not itself
            # fail, or a monitor cannot tell "Redis is down" from "the API is
            # down".
            return False

    async def close(self) -> None:
        await self._redis.aclose()


def build_store(redis_url: str | None, ttl_seconds: int) -> ResultStore:
    """Pick a store from configuration."""
    if redis_url:
        return RedisResultStore(redis_url, ttl_seconds)
    return MemoryResultStore(ttl_seconds)


__all__ = [
    "KEY_PREFIX",
    "MemoryResultStore",
    "RedisResultStore",
    "ResultStore",
    "build_store",
]
