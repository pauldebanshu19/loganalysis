"""Shared fixtures.

``samples/`` lives at the repo root so the CLI, the load tests and the web
end-to-end test can all reach the same fixtures the unit tests use.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app.config import Settings
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLES = REPO_ROOT / "samples"


@pytest.fixture(scope="session")
def samples_dir() -> Path:
    return SAMPLES


@pytest.fixture(scope="session")
def brief_log(samples_dir: Path) -> Path:
    """The brief's seven-line sample: the first test and the last check."""
    return samples_dir / "brief.log"


@pytest.fixture(scope="session")
def brief_bytes(brief_log: Path) -> bytes:
    return brief_log.read_bytes()


@pytest.fixture(scope="session")
def brief_lines(brief_log: Path) -> list[str]:
    return brief_log.read_text(encoding="utf-8").splitlines()


# -- API fixtures ----------------------------------------------------------

#: Defaults that make tests fast and self-contained: the in-process store, no
#: rate limit, and short waits so a timeout test does not take 30 seconds.
TEST_SETTINGS = {
    "REDIS_URL": None,
    "RATE_LIMIT_PER_MIN": 0,
    "SLOT_WAIT_S": 0.2,
    "UPLOAD_IDLE_TIMEOUT_S": 2.0,
    "LOG_LEVEL": "CRITICAL",
    "ENV": "test",
}


@pytest.fixture
def make_app() -> Callable[..., object]:
    """Build an app with overridden settings.

    Each call is a fresh application with its own store and slot pool, so a
    test that fills the pool cannot affect the next one.
    """

    def factory(**overrides):
        return create_app(Settings(**{**TEST_SETTINGS, **overrides}))

    return factory


@pytest_asyncio.fixture
async def client_factory(make_app):
    """Yield a helper that starts an app and returns a client bound to it."""
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:

        async def build(**overrides) -> httpx.AsyncClient:
            app = make_app(**overrides)
            await stack.enter_async_context(app.router.lifespan_context(app))
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(
                        app=app,
                        # Let the catch-all handler's 500 response be observed
                        # rather than re-raised into the test.
                        raise_app_exceptions=False,
                    ),
                    base_url="http://testserver",
                )
            )
            client.app = app  # type: ignore[attr-defined]
            return client

        yield build


@pytest_asyncio.fixture
async def client(client_factory) -> AsyncIterator[httpx.AsyncClient]:
    """A client against an app with the default test settings."""
    yield await client_factory()


@pytest.fixture
def error_of() -> Callable[[httpx.Response], dict]:
    """Returns a helper that pulls the ``error`` object out of a failure.

    Every failure in this service shares one body, so the shape is asserted
    here rather than repeated in each error test.
    """

    def extract(response: httpx.Response) -> dict:
        body = response.json()
        assert set(body) == {"error"}, f"unexpected top-level keys: {sorted(body)}"
        error = body["error"]
        assert set(error) == {"code", "message", "details", "request_id"}
        assert isinstance(error["code"], str) and error["code"]
        assert isinstance(error["message"], str) and error["message"]
        assert isinstance(error["details"], dict)
        assert error["request_id"] == response.headers.get("x-request-id")
        return error

    return extract
