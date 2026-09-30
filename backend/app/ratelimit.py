"""Per-IP request limiting, shared across workers when Redis is configured.

A fixed one-minute window: cheap, one round trip, and easy to explain in a
``Retry-After``.  It admits a burst at a window boundary, which for a limit
meant to stop one client monopolising a small pool is a fair trade against the
bookkeeping a sliding window needs.

The PRD names slowapi for this.  It is not used, for one reason: its Redis
backend is synchronous, and a blocking round trip on every request would stall
the event loop that the streaming upload reader depends on being free.  The
limiter below is async end to end and shares the connection pool the result
store already opens.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable

from starlette.types import ASGIApp, Receive, Scope, Send

from .errors import AppError, RateLimited

log = logging.getLogger("app.ratelimit")

WINDOW_SECONDS = 60
KEY_PREFIX = "ratelimit:"

#: Operational endpoints are exempt.  A Prometheus scrape and a liveness probe
#: together can eat a meaningful share of a 30/min budget, and throttling your
#: own monitoring is how an incident becomes invisible.
EXEMPT_PATHS = frozenset({"/metrics", "/api/v1/health"})

#: Clients tracked by the in-process counter before it is reset.  Only the
#: no-Redis path uses it, where one process is the whole deployment.
_LOCAL_MAX_CLIENTS = 10_000


class RateLimiter:
    """Counts requests per client per minute."""

    def __init__(self, limit_per_minute: int, redis_url: str | None = None) -> None:
        self._limit = limit_per_minute
        self._redis = None
        if redis_url and limit_per_minute > 0:
            from redis.asyncio import Redis

            self._redis = Redis.from_url(redis_url, decode_responses=True)
        self._local: dict[str, tuple[int, int]] = {}

    @property
    def enabled(self) -> bool:
        return self._limit > 0

    async def check(self, client: str) -> None:
        """Raise :class:`RateLimited` if ``client`` is over its limit."""
        if not self.enabled:
            return

        window = int(time.time()) // WINDOW_SECONDS
        count = (
            await self._count_redis(client, window)
            if self._redis is not None
            else self._count_local(client, window)
        )
        if count > self._limit:
            retry_after = WINDOW_SECONDS - int(time.time()) % WINDOW_SECONDS
            raise RateLimited(
                f"Limit is {self._limit} requests per minute.",
                retry_after=max(retry_after, 1),
                limit_per_minute=self._limit,
            )

    async def _count_redis(self, client: str, window: int) -> int:
        key = f"{KEY_PREFIX}{client}:{window}"
        try:
            pipe = self._redis.pipeline()
            pipe.incr(key)
            # Twice the window, so the key outlives its usefulness by a margin
            # and a clock skew between workers cannot expire it early.
            pipe.expire(key, WINDOW_SECONDS * 2)
            count, _ = await pipe.execute()
            return int(count)
        except Exception:
            # A limiter that fails closed would turn a Redis blip into a total
            # outage.  Let the request through and say so.
            log.warning("rate limiter unavailable, allowing request", exc_info=True)
            return 0

    def _count_local(self, client: str, window: int) -> int:
        stored_window, count = self._local.get(client, (window, 0))
        if stored_window != window:
            stored_window, count = window, 0
        count += 1
        self._local[client] = (stored_window, count)
        if len(self._local) > _LOCAL_MAX_CLIENTS:
            self._prune(window)
        return count

    def _prune(self, window: int) -> None:
        for key in [k for k, (w, _) in self._local.items() if w != window]:
            del self._local[key]
        if len(self._local) > _LOCAL_MAX_CLIENTS:
            # Every entry is from the current window, so there is nothing stale
            # to drop.  Clearing hands everyone a fresh budget for the rest of
            # the minute, which is a better failure than letting a flood of
            # distinct source addresses grow this dict without limit.
            log.warning(
                "rate limiter tracking table full, resetting",
                extra={"clients": len(self._local)},
            )
            self._local.clear()

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()


class RateLimitMiddleware:
    """Checks the limit before a single byte of body is read.

    Pure ASGI rather than ``BaseHTTPMiddleware``, which would wrap the request
    in its own receive channel and break the streaming reads the upload path
    depends on.
    """

    def __init__(self, app: ASGIApp, exempt: Iterable[str] = EXEMPT_PATHS) -> None:
        self.app = app
        self.exempt = frozenset(exempt)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self.exempt:
            await self.app(scope, receive, send)
            return

        limiter: RateLimiter = scope["app"].state.limiter
        if limiter.enabled:
            from .middleware import client_ip

            try:
                await limiter.check(client_ip(scope))
            except AppError as exc:
                # Middleware sits outside the exception handlers, so the
                # response is built here to keep the single error shape.
                from .errors import count_error

                count_error(exc.code)
                request_id = scope.get("state", {}).get("request_id", "req_unknown")
                await exc.to_response(request_id)(scope, receive, send)
                return

        await self.app(scope, receive, send)


__all__ = ["EXEMPT_PATHS", "WINDOW_SECONDS", "RateLimitMiddleware", "RateLimiter"]
