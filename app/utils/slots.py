from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from app.utils.errors import ServerBusy
from app.utils.metrics import slot_waits_total, slots_in_use, slots_total


class SlotPool:
    """A semaphore that gives up rather than queueing forever."""

    def __init__(self, capacity: int, wait_seconds: float) -> None:
        self._semaphore = asyncio.Semaphore(capacity)
        self._capacity = capacity
        self._wait_seconds = wait_seconds
        self._in_use = 0
        slots_total.set(capacity)
        slots_in_use.set(0)

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def in_use(self) -> int:
        return self._in_use

    @property
    def available(self) -> int:
        return self._capacity - self._in_use

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        """Hold a slot for the duration of the block, or raise :class:`ServerBusy`.

        The release is in a ``finally``, so a client that disconnects mid-upload
        frees its slot on the way out instead of leaking it.
        """
        try:
            if self._semaphore.locked():
                await asyncio.wait_for(
                    self._semaphore.acquire(), timeout=self._wait_seconds
                )
            else:
                # A free slot is taken directly.  Routing it through `wait_for`
                # would reject outright whenever SLOT_WAIT_S is zero, because a
                # zero timeout never lets the acquire run at all -- turning
                # "do not queue" into "never serve anyone".
                await self._semaphore.acquire()
        except TimeoutError:
            slot_waits_total.labels(outcome="rejected").inc()
            raise ServerBusy(
                slots=self._capacity, waited_seconds=self._wait_seconds
            ) from None

        slot_waits_total.labels(outcome="acquired").inc()
        self._in_use += 1
        slots_in_use.set(self._in_use)
        try:
            yield
        finally:
            self._in_use -= 1
            slots_in_use.set(self._in_use)
            self._semaphore.release()


__all__ = ["SlotPool"]
