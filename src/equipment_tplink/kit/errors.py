"""One error body for every failure, including FastAPI's and Starlette's own.

The service defines its concrete error model (a pydantic model with at least
``code`` and ``message``); :func:`install_error_handlers` renders every exception
into it so all error responses validate against the OpenAPI document.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from equipment_tplink.kit.request_log import annotate

logger = logging.getLogger(__name__)

GENERIC_STATUS: dict[str, int] = {
    "not_found": 404,
    "method_not_allowed": 405,
    "validation_error": 422,
    "internal_error": 500,
}


class ServiceError(Exception):
    """An error the service reports with a stable ``code``.

    Extra keyword ``fields`` are merged into the error body (e.g. ``driver``).
    """

    def __init__(self, code: str, message: str, *, status: int, **fields: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.fields: dict[str, Any] = fields


def render_error(
    error_model: type[BaseModel],
    defaults: Mapping[str, Any],
    code: str,
    message: str,
    fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a JSON-ready error body through ``error_model``."""
    data = {**defaults, **(fields or {}), "code": code, "message": message}
    return error_model.model_validate(data).model_dump(mode="json")


def install_error_handlers(
    app: FastAPI, *, error_model: type[BaseModel], defaults: Mapping[str, Any]
) -> None:
    """Render :class:`ServiceError`, validation, routing and unexpected errors."""

    def respond(
        status: int, code: str, message: str, fields: Mapping[str, Any] | None = None
    ) -> JSONResponse:
        annotate(outcome=code)
        body = render_error(error_model, defaults, code, message, fields)
        return JSONResponse(body, status_code=status)

    async def on_service_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ServiceError)
        return respond(exc.status, exc.code, exc.message, exc.fields)

    async def on_validation_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        errors = [
            {
                "loc": list(err.get("loc", ())),
                "msg": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ]
        return respond(
            422,
            "validation_error",
            "request validation failed",
            {"detail": {"errors": errors}},
        )

    async def on_http_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException)
        if exc.status_code == 404:
            return respond(404, "not_found", "no such route")
        if exc.status_code == 405:
            return respond(
                405, "method_not_allowed", "method not allowed for this route"
            )
        return respond(exc.status_code, "http_error", str(exc.detail))

    async def on_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", exc_info=exc)
        return respond(
            500,
            "internal_error",
            "internal error",
            {"detail": {"type": type(exc).__name__}},
        )

    app.add_exception_handler(ServiceError, on_service_error)
    app.add_exception_handler(RequestValidationError, on_validation_error)
    app.add_exception_handler(StarletteHTTPException, on_http_error)
    app.add_exception_handler(Exception, on_unexpected)


def error_responses(
    error_model: type[BaseModel], *statuses: int
) -> dict[int | str, Any]:
    """Declare error responses for a route (``responses=`` in FastAPI).

    Declaring 422 also stops FastAPI from injecting its ``HTTPValidationError``.
    """
    return {
        status: {
            "model": error_model,
            "description": _DESCRIPTIONS.get(status, "Error"),
        }
        for status in statuses
    }


_DESCRIPTIONS = {
    400: "Bad request",
    404: "Not found",
    409: "Conflict",
    422: "Validation error",
    500: "Internal error",
    501: "Not supported",
    502: "Upstream failure",
    503: "Service not ready",
    504: "Timed out",
}
