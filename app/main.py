from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import ClientDisconnect

from app.api import analyses, health
from app.config import Settings, get_settings
from app.models.analysis import ANALYZER_VERSION
from app.storage.store import build_store
from app.utils.errors import install_error_handlers
from app.utils.logger import configure_logging
from app.utils.metrics import build_registry
from app.utils.middleware import RequestContextMiddleware
from app.utils.rate_limit import RateLimiter, RateLimitMiddleware
from app.utils.slots import SlotPool

log = logging.getLogger("app.main")

DESCRIPTION = """
Upload a log file and find out which service is failing and how badly.

Bad log lines are data, not errors: they are counted, reported with a reason,
and the request still succeeds. Failures are only for requests the server
cannot process, and every one returns the same body with a stable `code`.
Switch on `code`, never on `message`.
""".strip()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an application instance.

    A factory rather than a module-level singleton so tests can run several
    configurations in one session without fighting over global state.
    """
    settings = settings or get_settings()
    configure_logging(settings.LOG_LEVEL)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.store = build_store(settings.REDIS_URL, settings.RESULT_TTL_S)
        app.state.slots = SlotPool(
            settings.MAX_CONCURRENT_ANALYSES, settings.SLOT_WAIT_S
        )
        app.state.limiter = RateLimiter(settings.RATE_LIMIT_PER_MIN, settings.REDIS_URL)
        log.info(
            "startup",
            extra={
                "env": settings.ENV,
                "store": "redis" if settings.REDIS_URL else "memory",
                "slots": settings.MAX_CONCURRENT_ANALYSES,
                "max_upload_mb": settings.MAX_UPLOAD_MB,
                "auth": settings.auth_required,
            },
        )
        try:
            yield
        finally:
            await app.state.store.close()
            await app.state.limiter.close()

    app = FastAPI(
        title="Log Analyzer",
        version=ANALYZER_VERSION,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    # Routes ask for settings through `Depends(get_settings)`, which reads the
    # process environment.  Binding the override here makes an app instance use
    # the settings it was built with, so a test can stand up two servers with
    # different limits in the same process.
    app.dependency_overrides[get_settings] = lambda: settings

    install_error_handlers(app)
    _install_disconnect_handler(app)
    app.include_router(analyses.router)
    app.include_router(health.router)
    _install_metrics_endpoint(app)

    # `add_middleware` prepends, so the last one added is the outermost: the
    # request context wraps the limiter, so a rejected request still gets an id
    # and a log line; the limiter is innermost, checked before a byte of body
    # is read.
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)

    return app


def _install_disconnect_handler(app: FastAPI) -> None:
    """A client that hangs up mid-upload is not an error.

    Unwinding out of the route has already released the analysis slot and the
    buffers.  Nobody is listening for a reply, so it is logged rather than
    reported; the 499 is nginx's code for it and is never actually delivered.
    Without this the catch-all handler would record every cancelled upload as
    a 500 with a stack trace.
    """

    @app.exception_handler(ClientDisconnect)
    async def _client_disconnect(request: Request, exc: ClientDisconnect) -> Response:
        log.info(
            "client_disconnected",
            extra={
                "request_id": getattr(request.state, "request_id", "req_unknown"),
                "path": request.url.path,
            },
        )
        return Response(status_code=499)


def _install_metrics_endpoint(app: FastAPI) -> None:
    @app.get(
        "/metrics",
        include_in_schema=False,
        response_class=Response,
        summary="Prometheus metrics",
    )
    async def metrics() -> Response:
        return Response(
            content=generate_latest(build_registry()),
            media_type=CONTENT_TYPE_LATEST,
        )


app = create_app()

__all__ = ["app", "create_app"]
