"""Rate limiting, log formatting and settings parsing."""

from __future__ import annotations

import json
import logging

import pytest

from app.config import Settings
from app.utils.errors import RateLimited
from app.utils.logger import JsonFormatter
from app.utils.rate_limit import WINDOW_SECONDS, RateLimiter


def _fake_redis():
    from fakeredis import FakeAsyncRedis

    return FakeAsyncRedis(decode_responses=True)


@pytest.fixture(params=["memory", "redis"])
def limiter(request):
    limiter = RateLimiter(limit_per_minute=3)
    if request.param == "redis":
        limiter._redis = _fake_redis()
    return limiter


class TestRateLimiter:
    async def test_requests_under_the_limit_pass(self, limiter) -> None:
        for _ in range(3):
            await limiter.check("10.0.0.1")

    async def test_the_next_request_is_refused(self, limiter) -> None:
        for _ in range(3):
            await limiter.check("10.0.0.1")
        with pytest.raises(RateLimited) as caught:
            await limiter.check("10.0.0.1")
        assert caught.value.details["limit_per_minute"] == 3
        assert 1 <= caught.value.retry_after <= WINDOW_SECONDS

    async def test_clients_are_counted_separately(self, limiter) -> None:
        for _ in range(3):
            await limiter.check("10.0.0.1")
        # A different caller starts with a clean budget.
        await limiter.check("10.0.0.2")

    async def test_a_zero_limit_disables_the_check(self) -> None:
        limiter = RateLimiter(limit_per_minute=0)
        assert limiter.enabled is False
        for _ in range(100):
            await limiter.check("10.0.0.1")

    async def test_a_redis_outage_lets_requests_through(self) -> None:
        """Failing closed would turn a Redis blip into a total outage."""

        class Broken:
            def pipeline(self):
                raise ConnectionError("no route to host")

        limiter = RateLimiter(limit_per_minute=1)
        limiter._redis = Broken()
        for _ in range(10):
            await limiter.check("10.0.0.1")

    async def test_the_local_counter_does_not_grow_without_bound(self) -> None:
        limiter = RateLimiter(limit_per_minute=1_000_000)
        for i in range(10_100):
            await limiter.check(f"10.0.{i // 256}.{i % 256}")
        assert len(limiter._local) <= 10_001

    async def test_a_new_window_resets_the_count(self, monkeypatch) -> None:
        limiter = RateLimiter(limit_per_minute=1)
        await limiter.check("10.0.0.1")
        with pytest.raises(RateLimited):
            await limiter.check("10.0.0.1")

        real_time = __import__("time").time
        monkeypatch.setattr(
            "app.utils.rate_limit.time.time", lambda: real_time() + WINDOW_SECONDS
        )
        await limiter.check("10.0.0.1")


class TestJsonFormatter:
    def _format(self, record: logging.LogRecord) -> dict:
        return json.loads(JsonFormatter().format(record))

    def test_a_record_becomes_one_json_object(self) -> None:
        record = logging.LogRecord(
            "app.access", logging.INFO, "f.py", 1, "request", None, None
        )
        payload = self._format(record)
        assert payload["message"] == "request"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "app.access"
        assert payload["ts"].endswith("+00:00")

    def test_extra_fields_are_included(self) -> None:
        record = logging.LogRecord(
            "app.access", logging.INFO, "f.py", 1, "request", None, None
        )
        record.request_id = "req_abc"
        record.lines_processed = 1247
        payload = self._format(record)
        assert payload["request_id"] == "req_abc"
        assert payload["lines_processed"] == 1247

    def test_an_exception_is_captured_as_text(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = logging.LogRecord(
                "app", logging.ERROR, "f.py", 1, "failed", None, sys.exc_info()
            )
        payload = self._format(record)
        assert "ValueError: boom" in payload["exception"]

    def test_unserialisable_values_do_not_break_the_line(self) -> None:
        record = logging.LogRecord("app", logging.INFO, "f.py", 1, "x", None, None)
        record.thing = object()
        assert isinstance(self._format(record)["thing"], str)


class TestSettings:
    def test_defaults_match_the_documented_limits(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.MAX_UPLOAD_MB == 100
        assert settings.MAX_LINE_KB == 64
        assert settings.UPLOAD_IDLE_TIMEOUT_S == 30
        assert settings.MAX_CONCURRENT_ANALYSES == 8
        assert settings.SLOT_WAIT_S == 5
        assert settings.RATE_LIMIT_PER_MIN == 30
        assert settings.RESULT_TTL_S == 3600

    def test_derived_byte_limits(self) -> None:
        settings = Settings(_env_file=None, MAX_UPLOAD_MB=10, MAX_LINE_KB=8)
        assert settings.max_upload_bytes == 10 * 1024 * 1024
        assert settings.max_line_bytes == 8 * 1024

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("a,b", ["a", "b"]),
            ("a, b , c", ["a", "b", "c"]),
            ("", []),
            ("   ", []),
            ('["x","y"]', ["x", "y"]),
            ("single", ["single"]),
        ],
    )
    def test_list_settings_accept_comma_separated_strings(
        self, raw: str, expected: list[str]
    ) -> None:
        """.env files and shell exports write `a,b`, not JSON."""
        assert Settings(_env_file=None, API_KEYS=raw).API_KEYS == expected

    def test_auth_is_off_until_keys_are_set(self) -> None:
        assert Settings(_env_file=None).auth_required is False
        assert Settings(_env_file=None, API_KEYS="k").auth_required is True

    def test_settings_come_from_the_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("MAX_UPLOAD_MB", "7")
        monkeypatch.setenv("API_KEYS", "key-a,key-b")
        settings = Settings(_env_file=None)
        assert settings.MAX_UPLOAD_MB == 7
        assert settings.API_KEYS == ["key-a", "key-b"]

    @pytest.mark.parametrize(
        ("field", "value"),
        [("MAX_UPLOAD_MB", 0), ("MAX_LINE_KB", -1), ("RESULT_TTL_S", 0)],
    )
    def test_nonsense_limits_are_rejected_at_startup(self, field, value) -> None:
        with pytest.raises(ValueError):
            Settings(_env_file=None, **{field: value})
