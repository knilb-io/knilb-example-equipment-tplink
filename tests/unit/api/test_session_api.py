"""Cloud login over REST, including MFA through the library's own callback."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from equipment_tplink.app import LibraryHooks, create_app
from equipment_tplink.config import Settings
from equipment_tplink.fakes.cloud import fake_manager_factory
from equipment_tplink.fakes.world import FakeWorld
from equipment_tplink.kit.clock import FakeClock
from tests.conftest import MAC1, OpenApiValidator, ValidatingClient


def wait_for_state(client: ValidatingClient, state: str) -> dict[str, object]:
    for _ in range(200):
        body: dict[str, object] = client.get("/v1/cloud/session").json()
        if body["state"] == state:
            return body
        time.sleep(0.01)
    raise AssertionError(f"session never reached {state}")


def test_startup_login_and_logout(client: ValidatingClient) -> None:
    body = wait_for_state(client, "authenticated")
    assert body["username_masked"] == "u***@example.com"
    assert body["logged_in_at"] is not None
    logged_out = client.delete("/v1/cloud/session").json()
    assert logged_out["state"] == "login_required"
    assert client.post("/v1/cloud/session").json()["state"] == "authenticated"


def test_mfa_flow(client: ValidatingClient, world: FakeWorld) -> None:
    client.delete("/v1/cloud/session")
    world.account.mfa_code = "123456"
    challenge = client.post("/v1/cloud/session", json={})
    assert challenge.status_code == 503
    assert challenge.json()["code"] == "cloud_mfa_required"
    assert client.get("/v1/cloud/session").json()["state"] == "mfa_required"
    health = client.get("/v1/health").json()
    assert health["cloud"]["status"] == "mfa_required"
    again = client.post("/v1/cloud/session", json={})
    assert again.json()["code"] == "cloud_mfa_required"
    blocked = client.get(f"/v1/cloud/plugs/{MAC1}")
    assert blocked.json()["code"] == "cloud_mfa_required"
    done = client.post("/v1/cloud/session", json={"mfa_code": "123456"})
    assert done.status_code == 200
    assert done.json()["state"] == "authenticated"


def test_wrong_mfa_code(client: ValidatingClient, world: FakeWorld) -> None:
    client.delete("/v1/cloud/session")
    world.account.mfa_code = "123456"
    client.post("/v1/cloud/session", json={})
    wrong = client.post("/v1/cloud/session", json={"mfa_code": "000000"})
    assert wrong.status_code == 503
    assert wrong.json()["code"] == "cloud_login_required"
    assert wrong.json()["upstream"]["calls"][0]["error_code"] == -20676
    stale = client.post("/v1/cloud/session", json={"mfa_code": "123456"})
    assert stale.status_code == 409
    assert stale.json()["code"] == "mfa_not_pending"


def test_mfa_code_nobody_waits_for(client: ValidatingClient) -> None:
    wait_for_state(client, "authenticated")
    response = client.post("/v1/cloud/session", json={"mfa_code": "123456"})
    assert response.status_code == 409


def test_wrong_password(settings: Settings, validator: OpenApiValidator) -> None:
    clock = FakeClock()
    world = FakeWorld(clock)
    app = create_app(
        Settings(tplink_username="user@example.com", tplink_password="nope"),
        clock=clock,
        hooks=LibraryHooks(manager_factory=fake_manager_factory(world)),
    )
    with TestClient(app) as test_client:
        client = ValidatingClient(test_client, validator)
        response = client.post("/v1/cloud/session", json={})
        assert response.status_code == 503
        assert response.json()["code"] == "cloud_login_required"
        assert "Incorrect email or password" in response.json()["message"]


def test_not_configured(validator: OpenApiValidator) -> None:
    clock = FakeClock()
    world = FakeWorld(clock)
    app = create_app(
        Settings(),
        clock=clock,
        hooks=LibraryHooks(manager_factory=fake_manager_factory(world)),
    )
    with TestClient(app) as test_client:
        client = ValidatingClient(test_client, validator)
        assert client.get("/v1/cloud/session").json()["state"] == "not_configured"
        response = client.post("/v1/cloud/session", json={})
        assert response.status_code == 503
        assert response.json()["code"] == "cloud_not_configured"
        listed = client.get("/v1/cloud/plugs")
        assert listed.json()["code"] == "cloud_not_configured"
        assert client.delete("/v1/cloud/session").json()["state"] == "not_configured"
