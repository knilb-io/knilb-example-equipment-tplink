"""Plug operations through HTTP, for both drivers, validated against the contract."""

from __future__ import annotations

import pytest

from equipment_tplink.fakes.world import FakeWorld
from equipment_tplink.kit.clock import FakeClock
from tests.conftest import MAC1, MAC2, ValidatingClient

DRIVERS = ["local", "cloud"]


@pytest.fixture(params=DRIVERS)
def kind(request: pytest.FixtureRequest) -> str:
    value: str = request.param
    return value


def login(client: ValidatingClient) -> None:
    response = client.post("/v1/cloud/session", json={})
    assert response.status_code == 200, response.json()


def test_state_by_mac_any_spelling(client: ValidatingClient, kind: str) -> None:
    login(client)
    response = client.get(f"/v1/{kind}/plugs/00-00-5e-00-53-01")
    assert response.status_code == 200
    body = response.json()
    assert body["driver"] == kind
    assert body["plug"]["mac"] == MAC1
    assert body["is_on"] is False
    assert body["on_since"] is None
    assert body["upstream"]["driver"] == kind
    assert body["upstream"]["calls"], "library calls are recorded"


def test_local_state_by_ip_learns_mac(client: ValidatingClient) -> None:
    body = client.get("/v1/local/plugs/192.0.2.11").json()
    assert body["plug"]["mac"] == MAC2
    assert body["plug"]["host"] == "192.0.2.11"
    assert body["upstream"]["connection"] == {
        "host": "192.0.2.11",
        "port": 80,
        "encrypt_type": "KLAP",
        "login_version": 2,
    }


def test_cloud_state_by_device_id(client: ValidatingClient, world: FakeWorld) -> None:
    login(client)
    device_id = world.by_mac(MAC1).device_id
    body = client.get(f"/v1/cloud/plugs/{device_id.lower()}").json()
    assert body["plug"]["device_id"] == device_id


def test_relay_on_verify_and_energy(
    client: ValidatingClient, kind: str, clock: FakeClock
) -> None:
    login(client)
    relay = client.post(f"/v1/{kind}/plugs/{MAC1}/relay", json={"state": "on"}).json()
    assert relay["expected_on"] is True
    assert relay["is_on"] is True
    assert relay["confirmed"] is True
    assert [call["op"] for call in relay["upstream"]["calls"]][-1] in (
        "update",
        "get_sys_info",
    )
    clock.advance(30)
    state = client.get(f"/v1/{kind}/plugs/{MAC1}").json()
    assert state["is_on"] is True
    assert state["on_since"] is not None
    energy = client.get(f"/v1/{kind}/plugs/{MAC1}/energy").json()
    assert energy["power_w"] == pytest.approx(60.0)
    assert energy["voltage_v"] == pytest.approx(120.0)
    assert energy["current_a"] == pytest.approx(0.5)
    assert energy["energy_total_kwh"] == pytest.approx(12.345)


def test_relay_toggle_and_no_verify(client: ValidatingClient, kind: str) -> None:
    login(client)
    toggled = client.post(f"/v1/{kind}/plugs/{MAC1}/relay", json={"state": "toggle"})
    assert toggled.json()["expected_on"] is True
    unverified = client.post(
        f"/v1/{kind}/plugs/{MAC1}/relay", json={"state": "off", "verify": False}
    ).json()
    assert unverified["is_on"] is None
    assert unverified["confirmed"] is None


def test_cloud_relay_mismatch_is_still_200(
    client: ValidatingClient, world: FakeWorld
) -> None:
    world.cloud_latency_s = 5.0
    login(client)
    result = client.post(
        f"/v1/cloud/plugs/{MAC1}/relay", json={"state": "on", "settle_ms": 100}
    ).json()
    assert result["confirmed"] is False
    assert result["is_on"] is False


def test_cloud_command_seen_by_local_wait(
    client: ValidatingClient, world: FakeWorld
) -> None:
    world.cloud_latency_s = 1.2
    login(client)
    client.post(f"/v1/cloud/plugs/{MAC1}/relay", json={"state": "on", "verify": False})
    waited = client.get(f"/v1/local/plugs/{MAC1}/wait?until=on&timeout_s=5").json()
    assert waited["satisfied"] is True
    assert waited["polls"] > 1
    assert waited["last_observed"] == {
        "is_on": True,
        "online": True,
        "authenticated": True,
        "listed": None,
    }


def test_wait_deadline_is_a_result(client: ValidatingClient, kind: str) -> None:
    login(client)
    waited = client.get(f"/v1/{kind}/plugs/{MAC1}/wait?until=on&timeout_s=2").json()
    assert waited["satisfied"] is False
    assert waited["satisfied_at"] is None
    assert waited["waited_ms"] >= 2000


def test_reboot_then_wait_offline_and_online(client: ValidatingClient) -> None:
    accepted = client.post(f"/v1/local/plugs/{MAC1}/reboot", json={"delay_s": 1})
    assert accepted.status_code == 202
    assert accepted.json()["action"] == "reboot"
    offline = client.get(
        f"/v1/local/plugs/{MAC1}/wait?until=offline&timeout_s=5"
    ).json()
    assert offline["satisfied"] is True
    online = client.get(f"/v1/local/plugs/{MAC1}/wait?until=online&timeout_s=10").json()
    assert online["satisfied"] is True
    assert online["last_observed"]["authenticated"] is True


def test_reboot_body_is_optional(client: ValidatingClient) -> None:
    assert client.post(f"/v1/local/plugs/{MAC2}/reboot").status_code == 202


def test_cloud_wait_offline_and_online(
    client: ValidatingClient, world: FakeWorld
) -> None:
    login(client)
    world.reboot(world.by_mac(MAC1), 0)
    offline = client.get(
        f"/v1/cloud/plugs/{MAC1}/wait?until=offline&timeout_s=3"
    ).json()
    assert offline["satisfied"] is True
    assert offline["last_observed"]["listed"] is True
    online = client.get(f"/v1/cloud/plugs/{MAC1}/wait?until=online&timeout_s=10").json()
    assert online["satisfied"] is True


def test_cloud_wait_unlisted_plug_is_offline(
    client: ValidatingClient, world: FakeWorld
) -> None:
    login(client)
    world.by_mac(MAC2).listed = False
    waited = client.get(f"/v1/cloud/plugs/{MAC2}/wait?until=offline&timeout_s=2").json()
    assert waited["satisfied"] is True
    assert waited["last_observed"]["listed"] is False


def test_factory_reset_guard_and_recovery(
    client: ValidatingClient, world: FakeWorld
) -> None:
    wrong = client.post(
        f"/v1/local/plugs/{MAC1}/factory-reset", json={"confirm_mac": MAC2}
    )
    assert wrong.status_code == 409
    assert wrong.json()["code"] == "confirmation_mismatch"
    reset = client.post(
        f"/v1/local/plugs/{MAC1}/factory-reset", json={"confirm_mac": "00005e005301"}
    )
    assert reset.status_code == 202
    back = client.get(f"/v1/local/plugs/{MAC1}/wait?until=online&timeout_s=30").json()
    assert back["satisfied"] is True
    assert back["plug"]["host"] == world.by_mac(MAC1).host == "192.0.2.110"


def test_factory_reset_by_host_reads_mac_first(client: ValidatingClient) -> None:
    reset = client.post(
        "/v1/local/plugs/192.0.2.11/factory-reset", json={"confirm_mac": MAC2}
    )
    assert reset.status_code == 202
    assert reset.json()["plug"]["mac"] == MAC2


def test_info(client: ValidatingClient, kind: str) -> None:
    login(client)
    info = client.get(f"/v1/{kind}/plugs/{MAC1}/info").json()
    assert info["has_energy_monitoring"] is True
    assert info["hw_version"] == "1.0"
    if kind == "local":
        assert info["cloud_link"]["bound"] is True
        assert info["cloud_link"]["username_masked"] == "u***@example.com"
        assert info["account_entry"] is None
    else:
        assert info["cloud_link"] is None
        assert info["account_entry"]["online"] is True


def test_schedule(client: ValidatingClient, kind: str) -> None:
    login(client)
    rules = client.get(f"/v1/{kind}/plugs/{MAC1}/schedule").json()["rules"]
    assert rules[0]["days"] == ["mon", "tue", "wed", "thu", "fri"]
    assert rules[0]["start"]["action"] == "on"
    assert rules[0]["start"]["minutes"] == 1080
    # python-kasa's Rule drops offsets (FINDINGS F20); tplink-cloud-api keeps them.
    assert rules[0]["start"]["offset_minutes"] == (None if kind == "local" else 0)


def test_settings_led(client: ValidatingClient, kind: str) -> None:
    login(client)
    assert client.get(f"/v1/{kind}/plugs/{MAC1}/settings").json()["led_on"] is True
    changed = client.patch(f"/v1/{kind}/plugs/{MAC1}/settings", json={"led_on": False})
    if kind == "local":
        assert changed.status_code == 200
        assert changed.json()["led_on"] is False
    else:  # tplink-cloud-api's set_led_state never yields a result (FINDINGS F14)
        assert changed.status_code == 502
        assert changed.json()["code"] == "cloud_error"


def test_local_rename(client: ValidatingClient) -> None:
    changed = client.patch(f"/v1/local/plugs/{MAC1}/settings", json={"alias": "Desk"})
    assert changed.json()["alias"] == "Desk"


def test_cloud_rename_is_unsupported(client: ValidatingClient) -> None:
    login(client)
    response = client.patch(f"/v1/cloud/plugs/{MAC1}/settings", json={"alias": "Desk"})
    assert response.status_code == 501
    assert "F10" in response.json()["message"]


def test_settings_update_needs_a_field(client: ValidatingClient) -> None:
    response = client.patch(f"/v1/local/plugs/{MAC1}/settings", json={})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_energy_unsupported_without_emeter(
    client: ValidatingClient, world: FakeWorld, kind: str
) -> None:
    plug = world.by_mac(MAC2)
    plug.has_emeter = False
    plug.model = "HS103(US)"
    login(client)
    response = client.get(f"/v1/{kind}/plugs/{MAC2}/energy")
    assert response.status_code == 501
    assert response.json()["code"] == "unsupported"


def test_invalid_refs(client: ValidatingClient) -> None:
    bad = client.get("/v1/local/plugs/lamp!")
    assert bad.status_code == 400
    assert bad.json()["code"] == "invalid_plug_ref"
    ip_on_cloud = client.get("/v1/cloud/plugs/192.0.2.10")
    assert ip_on_cloud.status_code == 400
    wait_bad = client.get("/v1/local/plugs/lamp!/wait?until=on")
    assert wait_bad.status_code == 400


def test_unknown_mac_is_404(client: ValidatingClient, kind: str) -> None:
    login(client)
    response = client.get(f"/v1/{kind}/plugs/00:00:5E:00:53:99")
    assert response.status_code == 404
    assert response.json()["code"] == "plug_not_found"


def test_local_offline_host_is_502(client: ValidatingClient) -> None:
    response = client.get("/v1/local/plugs/192.0.2.99")
    assert response.status_code == 502
    body = response.json()
    assert body["code"] == "device_unreachable"
    assert body["upstream"]["calls"][0]["op"] == "discover_single"


def test_local_auth_failure_is_502_with_details(
    client: ValidatingClient, world: FakeWorld
) -> None:
    world.accept_local_credentials = False
    response = client.get(f"/v1/local/plugs/{MAC1}")
    assert response.status_code == 502
    body = response.json()
    assert body["code"] == "device_auth_failed"
    assert body["upstream"]["connection"]["encrypt_type"] == "KLAP"
    assert body["upstream"]["connection"]["login_version"] == 2
    assert body["upstream"]["calls"][-1]["error_type"] == "AuthenticationError"
    waited = client.get(f"/v1/local/plugs/{MAC1}/wait?until=online&timeout_s=2").json()
    assert waited["satisfied"] is True
    assert waited["last_observed"]["authenticated"] is False


def test_cloud_offline_device_is_502(
    client: ValidatingClient, world: FakeWorld
) -> None:
    login(client)
    world.reboot(world.by_mac(MAC1), 0)
    response = client.get(f"/v1/cloud/plugs/{MAC1}?timeout_s=5")
    assert response.status_code == 502
    assert response.json()["code"] == "device_offline"


def test_cloud_requires_login(client: ValidatingClient) -> None:
    client.delete("/v1/cloud/session")
    response = client.get(f"/v1/cloud/plugs/{MAC1}")
    assert response.status_code == 503
    assert response.json()["code"] == "cloud_login_required"
    listed = client.get("/v1/cloud/plugs")
    assert listed.status_code == 503
    waited = client.get(f"/v1/cloud/plugs/{MAC1}/wait?until=on")
    assert waited.status_code == 503


def test_discover_and_list(client: ValidatingClient) -> None:
    discovered = client.get("/v1/local/plugs?timeout_s=2").json()
    assert [plug["plug"]["mac"] for plug in discovered["plugs"]] == [MAC1, MAC2]
    assert all(plug["auth"] == "ok" for plug in discovered["plugs"])
    login(client)
    listed = client.get("/v1/cloud/plugs").json()
    assert listed["cached"] is False
    assert client.get("/v1/cloud/plugs").json()["cached"] is True
    assert client.get("/v1/cloud/plugs?refresh=true").json()["cached"] is False


def test_rediscovery_finds_moved_plug(
    client: ValidatingClient, world: FakeWorld
) -> None:
    client.get(f"/v1/local/plugs/{MAC2}")
    world.factory_reset(world.by_mac(MAC2))
    waited = client.get(f"/v1/local/plugs/{MAC2}/wait?until=online&timeout_s=30").json()
    assert waited["satisfied"] is True
    assert waited["plug"]["host"] == "192.0.2.111"
    assert waited["poll_errors"] == 0


def test_energy_and_schedule_report_learned_identity(client: ValidatingClient) -> None:
    energy = client.get("/v1/local/plugs/192.0.2.10/energy").json()
    assert energy["plug"]["mac"] == MAC1
    assert energy["plug"]["alias"] == "Bench lamp"
    schedule = client.get("/v1/local/plugs/192.0.2.11/schedule").json()
    assert schedule["plug"]["mac"] == MAC2
