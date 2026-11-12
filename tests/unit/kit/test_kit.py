"""The generic kit: clock, timestamps, redaction, upstream recording, OpenAPI I/O."""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import enum
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from equipment_tplink.kit.clock import FakeClock, SystemClock
from equipment_tplink.kit.openapi import to_json, to_yaml
from equipment_tplink.kit.redact import mask_email, redact, redact_text
from equipment_tplink.kit.timestamps import format_utc
from equipment_tplink.kit.upstream import (
    Recorder,
    current_recorder,
    record,
    recording,
    to_jsonable,
)

KIT = Path(__file__).resolve().parents[3] / "src" / "equipment_tplink" / "kit"


def test_kit_does_not_import_tplink_code() -> None:
    forbidden = ("core", "drivers", "fakes", "routers", "app", "cli", "config")
    for source in KIT.glob("*.py"):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                parts = node.module.split(".")
                if parts[0] == "equipment_tplink":
                    assert parts[1] in ("kit",) or parts[1:] == [], (
                        source.name,
                        node.module,
                    )
                    assert parts[1] not in forbidden


async def test_system_clock() -> None:
    clock = SystemClock()
    assert clock.now().tzinfo is UTC
    before = clock.monotonic()
    await clock.sleep(0)
    await clock.sleep(-1)
    assert clock.monotonic() >= before


async def test_fake_clock() -> None:
    clock = FakeClock()
    start = clock.now()
    await clock.sleep(1.5)
    clock.advance(-5)
    assert clock.monotonic() == 1.5
    assert clock.now() - start == timedelta(seconds=1.5)


def test_format_utc() -> None:
    naive = datetime(2026, 1, 2, 3, 4, 5, 678901)
    assert format_utc(naive) == "2026-01-02T03:04:05.678Z"
    plus_two = datetime(2026, 1, 2, 5, 4, 5, tzinfo=timezone(timedelta(hours=2)))
    assert format_utc(plus_two) == "2026-01-02T03:04:05.000Z"


def test_redaction() -> None:
    assert mask_email(None) is None
    assert mask_email("nathan@example.com") == "n***@example.com"
    assert redact_text("GET /?token=abc123&x=1 failed") == "GET /?token=***&x=1 failed"
    data = {
        "token": "t",
        "refreshToken": "r",
        "nested": [{"password": "p", "user": "ann@example.org"}, 3, None],
        "tuple": ("a",),
    }
    assert redact(data) == {
        "token": "***",
        "refreshToken": "***",
        "nested": [{"password": "***", "user": "a***@example.org"}, 3, None],
        "tuple": ["a"],
    }


class Color(enum.Enum):
    RED = 1


@dataclasses.dataclass
class Point:
    x: int
    _hidden: int = 0


def test_to_jsonable() -> None:
    obj = SimpleNamespace(a=1, _b=2, when=datetime(2026, 1, 1, tzinfo=UTC), fn=len)
    assert to_jsonable(obj) == {"a": 1, "when": "2026-01-01T00:00:00.000Z"}
    assert to_jsonable(Color.RED) == 1
    assert to_jsonable(date(2026, 1, 1)) == "2026-01-01"
    assert to_jsonable({1: {2}}) == {"1": [2]}
    assert to_jsonable(Point(3)) == {"x": 3}
    assert to_jsonable(object.__new__(object)).startswith("<object")
    deep: list[object] = []
    cursor = deep
    for _ in range(12):
        nested: list[object] = []
        cursor.append(nested)
        cursor = nested
    assert "[" in str(to_jsonable(deep))


class CodedError(Exception):
    def __init__(self, code: object) -> None:
        super().__init__("coded token=secret")
        self.error_code = code


async def test_record_outcomes() -> None:
    clock = FakeClock()
    recorder = Recorder(clock)
    with recording(recorder):
        assert current_recorder() is recorder
        async with record("ok") as call:
            call.response = [1, 2]
        with pytest.raises(CodedError):
            async with record("err"):
                raise CodedError(-20601)
        with pytest.raises(CodedError):
            async with record("bool-code"):
                raise CodedError(True)
        with pytest.raises(ValueError):
            async with record("handle-code") as call:
                call.error_code = 7
                raise ValueError("x")

        async def slow() -> None:
            async with record("slow"):
                await asyncio.sleep(1)

        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                await slow()
    assert current_recorder() is None
    ok, err, boolean, handled, slow_call = recorder.calls
    assert ok.response == {"value": [1, 2]}
    assert err.error_code == -20601 and err.error_message == "coded token=***"
    assert boolean.error_code is None
    assert handled.error_code == 7
    assert slow_call.outcome == "timeout"
    assert recorder.total_ms() >= 0


async def test_record_without_recorder() -> None:
    async with record("orphan") as call:
        call.response = None


def test_openapi_serialization() -> None:
    doc = {"b": [1, {"c": None}], "a": "x"}
    assert to_yaml(doc).startswith("b:")
    assert to_json(doc).endswith("}\n")
