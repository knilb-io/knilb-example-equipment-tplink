"""One structured JSON log line per request, correlated by ``X-Request-Id``.

Pure ASGI middleware (not ``BaseHTTPMiddleware``) so the ContextVar it sets is
visible to endpoints and exception handlers, which add fields with :func:`annotate`.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from equipment_tplink.kit.timestamps import format_utc

logger = logging.getLogger("equipment_tplink.requests")

_fields: ContextVar[dict[str, Any] | None] = ContextVar(
    "request_log_fields", default=None
)

REQUEST_ID_HEADER = "x-request-id"


def annotate(**fields: Any) -> None:
    """Add fields to the current request's log line (no-op outside a request)."""
    current = _fields.get()
    if current is not None:
        current.update(fields)


def current_request_id() -> str | None:
    """Return the id of the request being handled, if any."""
    current = _fields.get()
    return None if current is None else current.get("request_id")


class RequestLogMiddleware:
    """Log each HTTP request as JSON and echo or assign ``X-Request-Id``."""

    def __init__(self, app: ASGIApp, *, log: logging.Logger | None = None) -> None:
        self.app = app
        self.log = log or logger

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI call."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _header(scope, REQUEST_ID_HEADER) or uuid.uuid4().hex
        fields: dict[str, Any] = {"request_id": request_id}
        token = _fields.set(fields)
        status = 500
        start = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER.encode(), request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            route = scope.get("route")
            line = {
                "ts": format_utc(datetime.now(UTC)),
                "request_id": request_id,
                "method": scope.get("method"),
                "path": scope.get("path"),
                "operation_id": getattr(route, "operation_id", None),
                "status": status,
                "outcome": "ok",
                "latency_ms": round((time.perf_counter() - start) * 1000, 3),
            }
            line.update(
                {key: value for key, value in fields.items() if key != "request_id"}
            )
            self.log.info(json.dumps(line, default=str))
            _fields.reset(token)


def _header(scope: Scope, name: str) -> str | None:
    for key, value in scope.get("headers", []):
        if key.decode("latin-1").lower() == name:
            text: str = value.decode("latin-1").strip()
            return text[:128] or None
    return None
