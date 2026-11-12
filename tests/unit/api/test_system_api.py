"""Health, version, OpenAPI endpoints and generic error handling."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from equipment_tplink import __version__
from equipment_tplink.core.service import PlugService
from tests.conftest import OpenApiValidator, ValidatingClient


def test_health(client: ValidatingClient) -> None:
    body = client.get("/v1/health").json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["local"] == {
        "status": "ready",
        "discovery_target": "255.255.255.255",
        "plugs_known": 0,
    }


def test_version(client: ValidatingClient) -> None:
    body = client.get("/v1/version").json()
    assert body["python_kasa"] == "0.12.0"
    assert body["tplink_cloud_api"].startswith("5.")


def test_openapi_json_and_yaml(client: ValidatingClient) -> None:
    doc = client.get("/openapi.json").json()
    assert doc["openapi"] == "3.1.0"
    yaml_text = client.get("/openapi.yaml").text
    assert yaml_text.startswith("openapi: 3.1.0")


def test_unknown_route_and_method(client: ValidatingClient) -> None:
    missing = client.get("/v1/nope")
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"
    wrong = client.request("PUT", "/v1/health")
    assert wrong.status_code == 405
    assert wrong.json()["code"] == "method_not_allowed"


def test_validation_error_body(client: ValidatingClient) -> None:
    response = client.post("/v1/local/plugs/192.0.2.10/relay", json={"state": "up"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["detail"]["errors"][0]["loc"] == ["body", "state"]


def test_request_id_echo_and_log(
    client: ValidatingClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="equipment_tplink.requests")
    response = client.get("/v1/local/plugs/lamp!", headers={"X-Request-Id": "knilb-42"})
    assert response.headers["x-request-id"] == "knilb-42"
    line = json.loads(caplog.records[-1].getMessage())
    assert line["request_id"] == "knilb-42"
    assert line["operation_id"] == "getLocalPlugState"
    assert line["status"] == 400
    assert line["outcome"] == "invalid_plug_ref"
    assert line["driver"] == "local"
    generated = client.get("/v1/health")
    assert len(generated.headers["x-request-id"]) == 32


def test_unexpected_error_is_500(
    app: FastAPI, validator: OpenApiValidator, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(self: PlugService, raw_ref: str, *, timeout_s: float | None) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(PlugService, "state", boom)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        client = ValidatingClient(test_client, validator)
        response = client.get("/v1/local/plugs/192.0.2.10")
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert response.json()["detail"] == {"type": "RuntimeError"}
