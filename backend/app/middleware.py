"""Request id, metrics and the access log, in one pass over every request.

Kept as pure ASGI middleware rather than ``BaseHTTPMiddleware``: the latter
wraps the response body in an anyio stream, which would put a memory-bound
queue between the streaming upload reader and the socket -- exactly what the
rest of this service goes out of its way to avoid.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .metrics import request_duration_seconds, requests_total

log = logging.getLogger("app.access")

REQUEST_ID_HEADER = "x-request-id"

#: Client-supplied ids are echoed rather than replaced, so a trace id from a
#: caller's system survives; anything longer or stranger than this is dropped
#: and replaced, because it ends up in log files and response headers.
_MAX_CLIENT_REQUEST_ID = 64


def new_request_id() -> str:
    return "req_" + os.urandom(4).hex()


def _clean_request_id(value: str | None) -> str:
    if not value:
        return new_request_id()
    value = value.strip()
    if not value or len(value) > _MAX_CLIENT_REQUEST_ID:
        return new_request_id()
    if not all(char.isalnum() or char in "-_." for char in value):
        return new_request_id()
    return value


def route_template(scope: Scope) -> str:
    """The route's path template, for metric labels.

    Using the resolved path would mint a new time series per analysis id, so an
    unmatched path is reported as a single ``<unmatched>`` bucket instead.
    """
    route = scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return str(path)
    return "<unmatched>"


class RequestContextMiddleware:
    """Assign a request id, time the request, count it, and log it once."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope["headers"]}
        request_id = _clean_request_id(headers.get(REQUEST_ID_HEADER))
        scope.setdefault("state", {})["request_id"] = request_id

        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                # Every response carries the id, including ones produced by
                # handlers that never saw the request object.
                MutableHeaders(scope=message).setdefault("x-request-id", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - started
            route = route_template(scope)
            method = scope.get("method", "")
            requests_total.labels(
                route=route, method=method, status=str(status)
            ).inc()
            request_duration_seconds.labels(route=route, method=method).observe(elapsed)

            fields: dict[str, Any] = {
                "request_id": request_id,
                "method": method,
                "route": route,
                "path": scope.get("path", ""),
                "status": status,
                "duration_ms": round(elapsed * 1000, 2),
                "client_ip": client_ip(scope),
            }
            # Routes attach what only they know -- bytes read, lines analysed.
            fields.update(scope.get("state", {}).get("log_fields", {}))
            log.info("request", extra=fields)


def client_ip(scope: Scope) -> str:
    """Best guess at who is calling.

    ``X-Forwarded-For`` is trusted because in every deployment shape this
    service supports it sits behind a load balancer that sets it.  Exposed
    directly to the internet it would be spoofable, and the per-IP rate limit
    would be worth exactly what the header is -- which is why the compose file
    does not publish the API without one.
    """
    for key, value in scope.get("headers", ()):
        if key == b"x-forwarded-for":
            return value.decode("latin-1").split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else "unknown"


def note(scope_or_request: Any, **fields: Any) -> None:
    """Add fields to this request's access log line."""
    state = getattr(scope_or_request, "state", None)
    if state is None:  # a raw ASGI scope
        state = scope_or_request.setdefault("state", {})
        state.setdefault("log_fields", {}).update(fields)
        return
    existing = getattr(state, "log_fields", None)
    if existing is None:
        existing = {}
        state.log_fields = existing
    existing.update(fields)


__all__ = [
    "REQUEST_ID_HEADER",
    "RequestContextMiddleware",
    "client_ip",
    "new_request_id",
    "note",
    "route_template",
]
