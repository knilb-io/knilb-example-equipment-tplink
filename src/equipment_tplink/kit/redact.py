"""Scrub secrets from anything that leaves the service (responses and logs)."""

from __future__ import annotations

import re
from typing import Any

_SECRET_KEYS = {
    "token",
    "refreshtoken",
    "refresh_token",
    "access_token",
    "password",
    "passwd",
    "secret",
}
_EMAIL = re.compile(
    r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)"
)
_QUERY_SECRET = re.compile(
    r"(?i)\b(token|refreshToken|refresh_token|password)=([^&\s'\"]+)"
)
MASK = "***"


def mask_email(value: str | None) -> str | None:
    """Mask the local part of an email address: ``n***@example.com``."""
    if value is None:
        return None
    return _EMAIL.sub(lambda m: f"{m.group(1)}{MASK}@{m.group(2)}", value)


def redact_text(value: str) -> str:
    """Mask emails and ``token=``-style query parameters inside free text."""
    value = _QUERY_SECRET.sub(lambda m: f"{m.group(1)}={MASK}", value)
    return mask_email(value) or value


def redact(value: Any) -> Any:
    """Return a copy of a JSON-like value with secrets masked.

    Keys that name secrets are replaced wholesale; every string is passed through
    :func:`redact_text`.
    """
    if isinstance(value, dict):
        return {
            key: MASK
            if isinstance(key, str) and key.lower() in _SECRET_KEYS
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
