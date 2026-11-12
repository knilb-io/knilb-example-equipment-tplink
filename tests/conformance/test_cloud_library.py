"""The cloud driver against the *real* tplink-cloud-api, with HTTP emulated.

These tests pin the library limitations the service surfaces (FINDINGS F11-F15) and
catch drift between ``equipment_tplink.fakes.cloud`` and the library it imitates.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tplinkcloud.device_client import TPLinkDeviceClient

from equipment_tplink.app import LibraryHooks, create_app
from equipment_tplink.config import Settings
from equipment_tplink.fakes.kasa import fake_discover, fake_discover_single
from equipment_tplink.fakes.world import FakeWorld
from equipment_tplink.kit.clock import FakeClock
from tests.conformance.cloud_server import CloudServer
from tests.conftest import MAC1, MAC2, OpenApiValidator, ValidatingClient


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch, world: FakeWorld) -> CloudServer:
    cloud = CloudServer(world)
    monkeypatch.setattr("tplinkcloud.client.requests.post", cloud.requests_post)

    def device_post(
        self: TPLinkDeviceClient, body: dict[str, Any], url_path: str = "/"
    ) -> Any:
        return cloud.device_post(self, body, url_path)

    monkeypatch.setattr(TPLinkDeviceClient, "_request_post", device_post)
    return cloud


@pytest.fixture
def real_client(
    server: CloudServer, world: FakeWorld, validator: OpenApiValidator
) -> Iterator[ValidatingClient]:
    """The app with the real TPLinkDeviceManager (no cloud fakes)."""
    app = create_app(
        Settings(
            tplink_username=world.account.username,
            tplink_password=world.account.password,
        ),
        clock=FakeClock(),
        hooks=LibraryHooks(
            discover=fake_discover(world), discover_single=fake_discover_single(world)
        ),
        mfa_wait_s=5,
    )
    with TestClient(app) as test_client:
        yield ValidatingClient(test_client, validator)


def login(client: ValidatingClient) -> dict[str, Any]:
    response = client.post("/v1/cloud/session", json={})
    assert response.status_code == 200, response.json()
    body: dict[str, Any] = response.json()
    return body


def test_login_list_and_operate(
    real_client: ValidatingClient, server: CloudServer
) -> None:
    assert login(real_client)["state"] == "authenticated"
    login_request = next(r for r in server.requests if r["url"].endswith("/login"))
    assert login_request["url"].startswith("https://n-use1-wap")  # regional lookup
    listed = real_client.get("/v1/cloud/plugs").json()
    assert [plug["plug"]["mac"] for plug in listed["plugs"]] == [MAC1, MAC2]
    assert listed["plugs"][0]["region"] == "us-east-1"

    relay = real_client.post(
        f"/v1/cloud/plugs/{MAC1}/relay", json={"state": "on"}
    ).json()
    assert relay["confirmed"] is True
    assert [c["op"] for c in relay["upstream"]["calls"]] == ["power_on", "get_sys_info"]
    state = real_client.get(f"/v1/cloud/plugs/{MAC1}").json()
    assert state["is_on"] is True and state["rssi"] == -52
    energy = real_client.get(f"/v1/cloud/plugs/{MAC1}/energy").json()
    assert energy["power_w"] == pytest.approx(60.0)
    assert energy["voltage_v"] == pytest.approx(120.0)
    rules = real_client.get(f"/v1/cloud/plugs/{MAC1}/schedule").json()["rules"]
    assert rules[0]["start"] == {
        "action": "on",
        "at": "time",
        "minutes": 1080,
        "offset_minutes": 0,
    }
    info = real_client.get(f"/v1/cloud/plugs/{MAC1}/info").json()
    assert (
        info["account_entry"]["app_server_url"] == "https://n-use1-wap.tplinkcloud.com"
    )
    settings = real_client.get(f"/v1/cloud/plugs/{MAC1}/settings").json()
    assert settings == {**settings, "alias": "Bench lamp", "led_on": True}


def test_passthrough_payload_shape(
    real_client: ValidatingClient, server: CloudServer
) -> None:
    login(real_client)
    real_client.post(
        f"/v1/cloud/plugs/{MAC1}/relay", json={"state": "on", "verify": False}
    )
    passthrough = [
        r for r in server.requests if r["body"].get("method") == "passthrough"
    ]
    assert (
        passthrough[-1]["body"]["params"]["deviceId"]
        == "8006F1A2B3C4D5E6F708192A3B4C5D6E7F801234"
    )
    assert (
        '"set_relay_state": {"state": 1}'
        in passthrough[-1]["body"]["params"]["requestData"]
    )


def test_f14_led_change_returns_no_result(
    real_client: ValidatingClient, server: CloudServer
) -> None:
    """F14: set_led_state sends {"set_led_off": {"off": n}}, never parses a reply."""
    login(real_client)
    response = real_client.patch(
        f"/v1/cloud/plugs/{MAC1}/settings", json={"led_on": False}
    )
    assert response.status_code == 502
    assert response.json()["code"] == "cloud_error"
    sent = [r for r in server.requests if r["body"].get("method") == "passthrough"][-1]
    assert sent["body"]["params"]["requestData"] == '{"set_led_off": {"off": 1}}'


def test_f11_offline_device_has_no_error_code(
    real_client: ValidatingClient, world: FakeWorld
) -> None:
    login(real_client)
    real_client.get("/v1/cloud/plugs")
    world.reboot(world.by_mac(MAC1), 0)
    stale = real_client.get(f"/v1/cloud/plugs/{MAC1}").json()
    # The cached list still says online, and the library dropped -20571: cloud_error.
    assert stale["code"] == "cloud_error"
    assert stale["upstream"]["calls"][-1]["error_code"] is None
    real_client.get("/v1/cloud/plugs?refresh=true")
    fresh = real_client.get(f"/v1/cloud/plugs/{MAC1}").json()
    assert fresh["code"] == "device_offline"


def test_f12_list_error_looks_like_empty_account(
    real_client: ValidatingClient, server: CloudServer
) -> None:
    login(real_client)
    server.list_error_code = -20002
    listed = real_client.get("/v1/cloud/plugs?refresh=true").json()
    assert listed["plugs"] == []
    assert real_client.get(f"/v1/cloud/plugs/{MAC1}").json()["code"] == "plug_not_found"


def test_f13_token_refresh_only_while_listing(
    real_client: ValidatingClient, server: CloudServer
) -> None:
    login(real_client)
    real_client.get("/v1/cloud/plugs")
    server.token_expired = True
    failed = real_client.get(f"/v1/cloud/plugs/{MAC1}").json()
    assert failed["code"] == "cloud_error"  # the device call can't refresh (F13)
    refreshed = real_client.get("/v1/cloud/plugs?refresh=true")  # the list call can
    assert refreshed.status_code == 200
    assert server.token == "token-2"
    assert real_client.get(f"/v1/cloud/plugs/{MAC1}").status_code == 200


def test_refresh_token_expiry_requires_login(
    real_client: ValidatingClient, server: CloudServer
) -> None:
    login(real_client)
    server.token_expired = server.refresh_expired = True
    response = real_client.get("/v1/cloud/plugs?refresh=true")
    assert response.status_code == 503
    assert response.json()["code"] == "cloud_login_required"
    assert response.json()["detail"]["error_code"] == -20655


def test_f15_legacy_emeter_units(
    real_client: ValidatingClient, server: CloudServer
) -> None:
    login(real_client)
    server.legacy_emeter = True
    real_client.post(f"/v1/cloud/plugs/{MAC1}/relay", json={"state": "on"})
    energy = real_client.get(f"/v1/cloud/plugs/{MAC1}/energy").json()
    assert energy["power_w"] == pytest.approx(0.06)  # 60 W reported as "60 mW"


def test_mfa_through_the_library_callback(
    real_client: ValidatingClient, world: FakeWorld, server: CloudServer
) -> None:
    real_client.delete("/v1/cloud/session")
    world.account.mfa_code = "246810"
    challenge = real_client.post("/v1/cloud/session", json={})
    assert challenge.json()["code"] == "cloud_mfa_required"
    done = real_client.post("/v1/cloud/session", json={"mfa_code": "246810"})
    assert done.status_code == 200
    mfa = next(r for r in server.requests if r["url"].endswith("checkMFACodeAndLogin"))
    assert mfa["body"]["code"] == "246810"


def test_wrong_password_code_is_surfaced(
    server: CloudServer, world: FakeWorld, validator: OpenApiValidator
) -> None:
    app = create_app(
        Settings(tplink_username=world.account.username, tplink_password="wrong"),
        clock=FakeClock(),
    )
    with TestClient(app) as test_client:
        client = ValidatingClient(test_client, validator)
        response = client.post("/v1/cloud/session", json={})
    body = response.json()
    assert body["code"] == "cloud_login_required"
    assert body["upstream"]["calls"][0]["error_code"] == -20601
    assert body["upstream"]["calls"][0]["error_type"] == "TPLinkAuthError"


def test_f23_kasa_mfa_nested_in_success_reply(
    server: CloudServer, world: FakeWorld, validator: OpenApiValidator
) -> None:
    """The bench account: the Kasa login asks for MFA inside a success reply, which
    tplink-cloud-api misses; the plug is listed in the Tapo cloud instead."""
    world.account.kasa_nested_mfa = True
    for plug in world.plugs:
        plug.cloud = "tapo"
    app = create_app(
        Settings(
            tplink_username=world.account.username,
            tplink_password=world.account.password,
        ),
        clock=FakeClock(),
        hooks=LibraryHooks(
            discover=fake_discover(world), discover_single=fake_discover_single(world)
        ),
    )
    with TestClient(app) as test_client:
        client = ValidatingClient(test_client, validator)
        assert client.post("/v1/cloud/session", json={}).json()["state"] == (
            "authenticated"
        )
        listed = client.get("/v1/cloud/plugs").json()
        assert listed["plugs"][0]["plug"]["mac"] == MAC1
        relay = client.post(f"/v1/cloud/plugs/{MAC1}/relay", json={"state": "on"})
        assert relay.json()["confirmed"] is True
    tapo = [r for r in server.requests if r["url"] == "/api/v2/common/passthrough"]
    assert tapo, "commands went through the Tapo cloud's V2 passthrough"


def test_f23_no_token_from_either_cloud(
    server: CloudServer, world: FakeWorld, validator: OpenApiValidator
) -> None:
    world.account.kasa_nested_mfa = True
    world.account.tapo_login_ok = False  # the library swallows this failure (F7)
    app = create_app(
        Settings(
            tplink_username=world.account.username,
            tplink_password=world.account.password,
        ),
        clock=FakeClock(),
    )
    with TestClient(app) as test_client:
        client = ValidatingClient(test_client, validator)
        response = client.post("/v1/cloud/session", json={})
    assert response.status_code == 503
    assert response.json()["code"] == "cloud_login_required"
    assert "F23" in response.json()["message"]
