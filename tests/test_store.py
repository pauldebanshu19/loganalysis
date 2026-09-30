"""The result store, in both of its implementations.

The same tests run against the in-process dict and against Redis (faked), so
"works in development" and "works with four workers" cannot drift apart.
"""

from __future__ import annotations

import pytest

from app.services.analyzer import analyze
from app.storage.store import KEY_PREFIX, MemoryResultStore, RedisResultStore, build_store


@pytest.fixture
def result():
    return analyze(
        [
            "2026-09-18 10:23:45 ERROR payment-service Connection timeout after 30s",
            "2026-09-18 10:23:46 INFO auth-service User login successful",
            "ERROR billing-service No Auth token",
        ],
        filename="brief.log",
        size_bytes=180,
    )


def _fake_redis():
    from fakeredis import FakeAsyncRedis

    return FakeAsyncRedis(decode_responses=True)


@pytest.fixture(params=["memory", "redis"])
def store(request):
    if request.param == "memory":
        return MemoryResultStore(ttl_seconds=3600)
    return RedisResultStore("", ttl_seconds=3600, client=_fake_redis())


class TestEitherStore:
    async def test_a_stored_result_comes_back_unchanged(self, store, result) -> None:
        await store.put(result)
        assert await store.get(result.id) == result

    async def test_an_unknown_id_is_none_not_an_error(self, store) -> None:
        assert await store.get("an_0000000000000000") is None

    async def test_ping_reports_reachable(self, store) -> None:
        assert await store.ping() is True

    async def test_results_are_independent(self, store, result) -> None:
        other = result.model_copy(update={"id": "an_0000000000000001"})
        await store.put(result)
        await store.put(other)
        assert (await store.get(result.id)).id == result.id
        assert (await store.get(other.id)).id == other.id

    async def test_round_tripping_keeps_every_field(self, store, result) -> None:
        """The stored form is JSON, so nothing may be lost in the conversion."""
        await store.put(result)
        restored = await store.get(result.id)
        assert restored.time_range == result.time_range
        assert restored.unparseable_samples == result.unparseable_samples
        assert restored.services == result.services
        assert restored.meta == result.meta

    async def test_a_deleted_result_is_gone(self, store, result) -> None:
        await store.put(result)
        assert await store.delete(result.id) is True
        assert await store.get(result.id) is None

    async def test_deleting_twice_reports_nothing_the_second_time(
        self, store, result
    ) -> None:
        await store.put(result)
        await store.delete(result.id)
        assert await store.delete(result.id) is False

    async def test_deleting_one_result_leaves_the_others(self, store, result) -> None:
        other = result.model_copy(update={"id": "an_0000000000000001"})
        await store.put(result)
        await store.put(other)
        await store.delete(result.id)
        assert (await store.get(other.id)).id == other.id


class TestMemoryStore:
    async def test_an_expired_result_is_gone(self, result) -> None:
        store = MemoryResultStore(ttl_seconds=3600)
        await store.put(result)
        store._items[result.id] = (0.0, result)
        assert await store.get(result.id) is None

    async def test_deleting_an_expired_result_reports_nothing(self, result) -> None:
        store = MemoryResultStore(ttl_seconds=3600)
        await store.put(result)
        store._items[result.id] = (0.0, result)
        assert await store.delete(result.id) is False

    async def test_expired_entries_are_swept_rather_than_accumulating(self) -> None:
        """A long-running dev server must not grow forever."""
        store = MemoryResultStore(ttl_seconds=0)
        base = analyze(["2026-09-18 10:23:45 INFO svc x"])
        for i in range(MemoryResultStore._SWEEP_AT + 5):
            await store.put(base.model_copy(update={"id": f"an_{i:016d}"}))
        assert len(store._items) < MemoryResultStore._SWEEP_AT

    async def test_close_clears_everything(self, result) -> None:
        store = MemoryResultStore(ttl_seconds=3600)
        await store.put(result)
        await store.close()
        assert await store.get(result.id) is None


class TestRedisStore:
    async def test_keys_are_namespaced(self, result) -> None:
        client = _fake_redis()
        store = RedisResultStore("", ttl_seconds=3600, client=client)
        await store.put(result)
        assert await client.exists(KEY_PREFIX + result.id)

    async def test_a_ttl_is_set_on_every_result(self, result) -> None:
        client = _fake_redis()
        store = RedisResultStore("", ttl_seconds=1800, client=client)
        await store.put(result)
        assert 0 < await client.ttl(KEY_PREFIX + result.id) <= 1800

    async def test_ping_reports_unreachable_instead_of_raising(self, result) -> None:
        """Health has to distinguish "Redis is down" from "the API is down",
        so a failed ping is an answer, not an exception."""

        class Broken:
            async def ping(self):
                raise ConnectionError("no route to host")

        store = RedisResultStore("", ttl_seconds=60, client=Broken())
        assert await store.ping() is False


class TestBuildStore:
    def test_no_url_gives_the_in_process_store(self) -> None:
        assert isinstance(build_store(None, 3600), MemoryResultStore)

    def test_a_url_gives_the_redis_store(self) -> None:
        store = build_store("redis://localhost:6379/0", 3600)
        assert isinstance(store, RedisResultStore)
