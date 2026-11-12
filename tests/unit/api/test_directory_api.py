"""``GET /v1/plugs``: the merged directory."""

from __future__ import annotations

from fastapi.testclient import TestClient

from equipment_tplink.app import LibraryHooks, create_app
from equipment_tplink.config import Settings
from equipment_tplink.fakes.kasa import fake_discover, fake_discover_single
from equipment_tplink.fakes.world import FakeWorld
from equipment_tplink.kit.clock import FakeClock
from tests.conftest import MAC1, MAC2, OpenApiValidator, ValidatingClient


def test_empty_then_refreshed(client: ValidatingClient) -> None:
    assert client.get("/v1/plugs").json() == {
        "plugs": [],
        "local_error": None,
        "cloud_error": None,
    }
    client.post("/v1/cloud/session")
    body = client.get("/v1/plugs?refresh=true").json()
    assert [entry["mac"] for entry in body["plugs"]] == [MAC1, MAC2]
    first = body["plugs"][0]
    assert first["local"]["host"] == "192.0.2.10"
    assert first["local"]["auth"] == "ok"
    assert first["cloud"]["online"] is True
    assert body["local_error"] is None and body["cloud_error"] is None
    assert client.get("/v1/health").json()["local"]["plugs_known"] == 2


def test_refresh_reports_driver_errors(validator: OpenApiValidator) -> None:
    clock = FakeClock()
    world = FakeWorld(clock)

    async def broken_discover(**_: object) -> dict[str, object]:
        raise OSError(49, "Can't assign requested address")

    app = create_app(
        Settings(),
        clock=clock,
        hooks=LibraryHooks(
            discover=broken_discover, discover_single=fake_discover_single(world)
        ),
    )
    with TestClient(app) as test_client:
        client = ValidatingClient(test_client, validator)
        body = client.get("/v1/plugs?refresh=true").json()
    assert body["local_error"]["code"] == "device_unreachable"
    assert body["cloud_error"]["code"] == "cloud_not_configured"


def test_refresh_survives_unexpected_errors(validator: OpenApiValidator) -> None:
    clock = FakeClock()
    world = FakeWorld(clock)

    async def weird_discover(**_: object) -> dict[str, object]:
        raise ValueError("unexpected")

    app = create_app(
        Settings(),
        clock=clock,
        hooks=LibraryHooks(
            discover=weird_discover, discover_single=fake_discover(world)
        ),
    )
    with TestClient(app) as test_client:
        body = (
            ValidatingClient(test_client, validator)
            .get("/v1/plugs?refresh=true")
            .json()
        )
    assert body["local_error"]["code"] == "device_error"
