"""Bounded concurrency: many clients at once, and what happens at the limit.

The promise is that load turns into a clear, retryable 503 rather than a
timeout, a stall, or an out-of-memory kill.  These tests hold the API to it.
"""

from __future__ import annotations

import asyncio

import pytest

from app.utils.errors import ServerBusy
from app.utils.slots import SlotPool


class TestSlotPool:
    async def test_a_slot_is_released_even_when_the_body_raises(self) -> None:
        """A client that disconnects mid-upload must not leak its slot."""
        pool = SlotPool(capacity=1, wait_seconds=0.05)
        with pytest.raises(RuntimeError):
            async with pool.acquire():
                raise RuntimeError("client vanished")
        assert pool.in_use == 0

        # The slot is genuinely free again.
        async with pool.acquire():
            assert pool.in_use == 1

    async def test_waiting_past_the_deadline_gives_server_busy(self) -> None:
        pool = SlotPool(capacity=1, wait_seconds=0.05)
        async with pool.acquire():
            with pytest.raises(ServerBusy) as caught:
                async with pool.acquire():
                    pass
        assert caught.value.details["slots"] == 1
        assert caught.value.retry_after == 5

    async def test_a_waiter_is_served_when_a_slot_frees_up(self) -> None:
        """Waiting is the normal case; rejection only after the deadline."""
        pool = SlotPool(capacity=1, wait_seconds=5.0)
        served = asyncio.Event()

        async def holder():
            async with pool.acquire():
                await asyncio.sleep(0.05)

        async def waiter():
            async with pool.acquire():
                served.set()

        await asyncio.gather(holder(), waiter())
        assert served.is_set()
        assert pool.in_use == 0

    async def test_capacity_is_never_exceeded(self) -> None:
        pool = SlotPool(capacity=4, wait_seconds=5.0)
        peak = 0

        async def work():
            nonlocal peak
            async with pool.acquire():
                peak = max(peak, pool.in_use)
                await asyncio.sleep(0.01)

        await asyncio.gather(*(work() for _ in range(40)))
        assert peak <= 4
        assert pool.in_use == 0


class TestConcurrentUploads:
    async def test_fifty_concurrent_uploads_all_succeed(
        self, client_factory, brief_bytes: bytes
    ) -> None:
        """The headline concurrency target, in miniature.

        Fifty at once against eight slots: the fifty-first through to the last
        wait their turn rather than failing, because `SLOT_WAIT_S` is longer
        than the work takes.
        """
        client = await client_factory(MAX_CONCURRENT_ANALYSES=8, SLOT_WAIT_S=10.0)

        async def upload(i: int):
            return await client.post(
                "/api/v1/analyses",
                files={"file": (f"app-{i}.log", brief_bytes)},
            )

        responses = await asyncio.gather(*(upload(i) for i in range(50)))

        assert [r.status_code for r in responses] == [201] * 50
        # Every one got its own id and its own correct answer.
        assert len({r.json()["id"] for r in responses}) == 50
        assert all(r.json()["lines_processed"] == 7 for r in responses)
        assert all(r.json()["top_offenders"] == ["payment-service"] for r in responses)
        assert client.app.state.slots.in_use == 0

    async def test_every_result_is_fetchable_afterwards(
        self, client_factory, brief_bytes: bytes
    ) -> None:
        client = await client_factory(MAX_CONCURRENT_ANALYSES=8, SLOT_WAIT_S=10.0)
        created = await asyncio.gather(
            *(
                client.post("/api/v1/analyses", files={"file": ("a.log", brief_bytes)})
                for _ in range(20)
            )
        )
        fetched = await asyncio.gather(
            *(client.get(r.headers["location"]) for r in created)
        )
        assert all(r.status_code == 200 for r in fetched)
        assert {r.json()["id"] for r in fetched} == {r.json()["id"] for r in created}

    async def test_slot_exhaustion_gives_503_and_never_500(
        self, client_factory, brief_bytes: bytes
    ) -> None:
        """Overload has to be a clean, retryable answer."""
        client = await client_factory(MAX_CONCURRENT_ANALYSES=1, SLOT_WAIT_S=0.0)

        async with client.app.state.slots.acquire():
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/api/v1/analyses", files={"file": ("a.log", brief_bytes)}
                    )
                    for _ in range(10)
                )
            )

        assert {r.status_code for r in responses} == {503}
        for response in responses:
            assert response.json()["error"]["code"] == "server_busy"
            assert response.headers["retry-after"] == "5"

    async def test_the_service_recovers_after_a_burst_of_rejections(
        self, client_factory, brief_bytes: bytes
    ) -> None:
        client = await client_factory(MAX_CONCURRENT_ANALYSES=1, SLOT_WAIT_S=0.0)

        async with client.app.state.slots.acquire():
            rejected = await client.post(
                "/api/v1/analyses", files={"file": ("a.log", brief_bytes)}
            )
        assert rejected.status_code == 503

        accepted = await client.post(
            "/api/v1/analyses", files={"file": ("a.log", brief_bytes)}
        )
        assert accepted.status_code == 201


class TestZeroWait:
    async def test_zero_wait_still_serves_a_free_slot(self) -> None:
        """`SLOT_WAIT_S=0` means "do not queue", not "never serve anyone"."""
        pool = SlotPool(capacity=2, wait_seconds=0.0)
        async with pool.acquire():
            async with pool.acquire():
                assert pool.in_use == 2
        assert pool.in_use == 0

    async def test_zero_wait_rejects_immediately_when_full(self) -> None:
        pool = SlotPool(capacity=1, wait_seconds=0.0)
        async with pool.acquire():
            with pytest.raises(ServerBusy):
                async with pool.acquire():
                    pass
