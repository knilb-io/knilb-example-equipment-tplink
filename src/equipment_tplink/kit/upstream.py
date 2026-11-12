"""Record every call into a third-party library for the ``upstream`` block.

A :class:`Recorder` is installed for the duration of one operation through a
ContextVar; drivers wrap each library call in ``async with record("op") as call``.
Set the recorder *before* entering ``asyncio.timeout`` and only mutate it inside,
so the calls are visible to the caller on every Python version.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel

from equipment_tplink.kit.clock import Clock, SystemClock
from equipment_tplink.kit.redact import redact, redact_text
from equipment_tplink.kit.timestamps import UtcTimestamp, format_utc


class UpstreamCall(BaseModel):
    """One call into a library."""

    op: str
    started_at: UtcTimestamp
    duration_ms: float
    outcome: Literal["ok", "error", "timeout"]
    error_code: int | None
    error_type: str | None
    error_message: str | None
    response: dict[str, Any] | None


class CallHandle:
    """Filled in by the caller while a call is being recorded."""

    def __init__(self) -> None:
        self.response: Any = None
        self.error_code: int | None = None


class Recorder:
    """Collects the calls made during one operation."""

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock = clock or SystemClock()
        self.calls: list[UpstreamCall] = []
        self.context: dict[str, Any] = {}

    def total_ms(self) -> float:
        """Sum of all recorded call durations."""
        return sum(call.duration_ms for call in self.calls)


_current: ContextVar[Recorder | None] = ContextVar("upstream_recorder", default=None)


def current_recorder() -> Recorder | None:
    """Return the recorder of the running operation, if any."""
    return _current.get()


@contextmanager
def recording(recorder: Recorder) -> Iterator[Recorder]:
    """Install ``recorder`` for the enclosed block."""
    token = _current.set(recorder)
    try:
        yield recorder
    finally:
        _current.reset(token)


@asynccontextmanager
async def record(op: str) -> AsyncIterator[CallHandle]:
    """Time and record one library call under the current recorder.

    Exceptions propagate unchanged; ``CancelledError`` (a timeout) is recorded as
    ``timeout``.
    """
    recorder = _current.get() or Recorder()
    clock = recorder.clock
    handle = CallHandle()
    started_at = clock.now()
    start = clock.monotonic()

    def finish(
        outcome: Literal["ok", "error", "timeout"], exc: BaseException | None = None
    ) -> None:
        response = None if exc else _as_object(handle.response)
        recorder.calls.append(
            UpstreamCall(
                op=op,
                started_at=started_at,
                duration_ms=round((clock.monotonic() - start) * 1000, 3),
                outcome=outcome,
                error_code=handle.error_code
                if handle.error_code is not None
                else _error_code(exc),
                error_type=type(exc).__name__ if exc else None,
                error_message=redact_text(str(exc)) if exc else None,
                response=response,
            )
        )

    try:
        yield handle
    except asyncio.CancelledError:
        finish("timeout")
        raise
    except BaseException as exc:
        finish("error", exc)
        raise
    else:
        finish("ok")


def to_jsonable(value: Any, _depth: int = 0) -> Any:
    """Convert library return values (objects, enums, dataclasses) to JSON data.

    Objects contribute their public attributes only.
    """
    if _depth > 8:
        return str(value)
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, enum.Enum):
        return to_jsonable(value.value, _depth + 1)
    if isinstance(value, datetime):
        return format_utc(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): to_jsonable(item, _depth + 1) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [to_jsonable(item, _depth + 1) for item in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_jsonable(getattr(value, field.name), _depth + 1)
            for field in dataclasses.fields(value)
            if not field.name.startswith("_")
        }
    if hasattr(value, "__dict__"):
        return {
            key: to_jsonable(item, _depth + 1)
            for key, item in vars(value).items()
            if not key.startswith("_") and not callable(item)
        }
    return str(value)


def _as_object(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    data = redact(to_jsonable(value))
    return data if isinstance(data, dict) else {"value": data}


def _error_code(exc: BaseException | None) -> int | None:
    code = getattr(exc, "error_code", None)
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    return int(code)
