"""RFC 3339 UTC timestamps with millisecond precision."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import PlainSerializer, WithJsonSchema


def format_utc(value: datetime) -> str:
    """Format ``value`` as ``YYYY-MM-DDTHH:MM:SS.mmmZ`` (naive values are UTC)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    text = value.astimezone(UTC).isoformat(timespec="milliseconds")
    return text.replace("+00:00", "Z")


UtcTimestamp = Annotated[
    datetime,
    PlainSerializer(format_utc, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]
"""A datetime serialized as an RFC 3339 UTC string with milliseconds."""
