"""Admission at the relay's door (see ``AdmissionGate``): past a point the honest answer to a burst is 429,
not a queue nobody is still waiting on."""

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager


class Overloaded(Exception):
    """Relay refused the request at the door rather than queueing it."""

    def __init__(self, reason: str, retry_after: int):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


class AdmissionGate:
    """Bounded admission control for the forwarding hot path.

    Three numbers, not one:

    - ``max_concurrency`` — attempts in flight at an upstream at once.
    - ``queue_limit`` — how many *more* may wait for a slot. This is the one
      that was missing. A bare semaphore has an unbounded wait queue, so a
      burst of 300 against 18 slots parked 282 callers behind agent turns
      that may run for minutes; by the time a slot freed, the client had
      long since given up, and relay still spent an upstream turn on it.
    - ``queue_timeout`` — how long a waiter may sit there before relay
      answers 429 itself instead of letting the caller's own timeout decide.

    Shedding at the door is what keeps p99 finite when arrivals outrun the
    upstream: a request that will not start for two minutes is better served
    a Retry-After now. ``queue_timeout <= 0`` restores the old unbounded
    wait, and ``queue_limit <= 0`` means "never queue".
    """

    def __init__(self, max_concurrency: int, queue_limit: int = 512,
                 queue_timeout: float = 30.0):
        self.max_concurrency = max(1, max_concurrency)
        self.queue_limit = max(0, queue_limit)
        self.queue_timeout = queue_timeout
        self._slots = asyncio.Semaphore(self.max_concurrency)
        self._active = 0
        self._waiting = 0

    @property
    def active(self) -> int:
        return self._active

    @property
    def waiting(self) -> int:
        return self._waiting

    async def acquire(self, queue: bool = True) -> "Callable[[], None]":
        """Take a slot, or raise :class:`Overloaded`. Returns the (idempotent)
        release callable — a slot outlives the coroutine that took it, because
        a streamed response is still occupying the upstream long after the
        handler returned.
        """
        # Checked before incrementing the waiter count, so the limit counts
        # callers actually parked on the semaphore and not this one.
        if self._slots.locked():
            if not queue or self.queue_limit <= 0:
                raise Overloaded("concurrency limit reached", 1)
            if self._waiting >= self.queue_limit:
                raise Overloaded(
                    f"queue full: {self._waiting} requests already waiting"
                    f" for one of {self.max_concurrency} slots", 5)
        self._waiting += 1
        try:
            await asyncio.wait_for(
                self._slots.acquire(),
                self.queue_timeout if self.queue_timeout > 0 else None)
        except (TimeoutError, asyncio.TimeoutError):
            raise Overloaded(
                f"waited {self.queue_timeout:.0f}s for a free upstream slot",
                5) from None
        finally:
            self._waiting -= 1
        self._active += 1
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            self._active -= 1
            self._slots.release()

        return release

    @asynccontextmanager
    async def slot(self, queue: bool = True):
        release = await self.acquire(queue=queue)
        try:
            yield
        finally:
            release()

    def snapshot(self) -> dict:
        return {"active": self._active, "waiting": self._waiting,
                "max_concurrency": self.max_concurrency,
                "queue_limit": self.queue_limit}
