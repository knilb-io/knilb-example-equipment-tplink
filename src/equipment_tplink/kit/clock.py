"""Injectable time source, so waits and fakes are deterministic under test."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """Wall-clock time, a monotonic counter and sleeping."""

    def now(self) -> datetime:
        """Return the current time as an aware UTC datetime."""
        ...

    def monotonic(self) -> float:
        """Return a monotonic counter in seconds."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Sleep for ``seconds``."""
        ...


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        """Return the current UTC time."""
        return datetime.now(UTC)

    def monotonic(self) -> float:
        """Return ``time.monotonic()``."""
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        """Sleep on the event loop."""
        await asyncio.sleep(max(seconds, 0.0))


class FakeClock:
    """A clock that only moves when something sleeps or calls :meth:`advance`."""

    def __init__(self, start: datetime | None = None) -> None:
        self._start = start or datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
        self._elapsed = 0.0

    def now(self) -> datetime:
        """Return the start time plus the elapsed fake time."""
        return self._start + timedelta(seconds=self._elapsed)

    def monotonic(self) -> float:
        """Return the elapsed fake time."""
        return self._elapsed

    def advance(self, seconds: float) -> None:
        """Move time forward without yielding."""
        self._elapsed += max(seconds, 0.0)

    async def sleep(self, seconds: float) -> None:
        """Advance time instantly, then yield once to the event loop."""
        self.advance(seconds)
        await asyncio.sleep(0)
