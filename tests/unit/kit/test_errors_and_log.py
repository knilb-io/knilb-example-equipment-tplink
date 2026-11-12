"""Error rendering and the request log middleware on a minimal app."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from equipment_tplink.kit.errors import ServiceError, install_error_handlers
from equipment_tplink.kit.request_log import (
    RequestLogMiddleware,
    annotate,
    current_request_id,
)


class Body(BaseModel):
    code: str
    message: str
    detail: dict[str, object] | None = None
    extra: str | None = None


def make_app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app, error_model=Body, defaults={"detail": None})
    app.add_middleware(RequestLogMiddleware)

    @app.get("/service")
    async def service() -> None:
        raise ServiceError("thing_failed", "it failed", status=502, extra="x")

    @app.get("/teapot")
    async def teapot() -> None:
        raise HTTPException(status_code=418, detail="short and stout")

    @app.get("/id")
    async def request_id() -> dict[str, str | None]:
        annotate(driver="test")
        return {"id": current_request_id()}

    return app


def test_service_error_and_other_http_errors() -> None:
    with TestClient(make_app()) as client:
        service = client.get("/service")
        assert service.status_code == 502
        assert service.json() == {
            "code": "thing_failed",
            "message": "it failed",
            "detail": None,
            "extra": "x",
        }
        teapot = client.get("/teapot")
        assert teapot.status_code == 418
        assert teapot.json()["code"] == "http_error"


def test_request_log_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="equipment_tplink.requests")
    with TestClient(make_app()) as client:
        response = client.get("/id", headers={"x-request-id": "  "})
    generated = response.json()["id"]
    assert generated and len(generated) == 32
    line = json.loads(caplog.records[-1].getMessage())
    assert line["driver"] == "test" and line["outcome"] == "ok"
    assert current_request_id() is None
    annotate(ignored=True)  # outside a request: no-op
