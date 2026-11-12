"""SPEC §10 hardware scenario on the LAN (python-kasa), plus the cross-driver check."""

from __future__ import annotations

import pytest

from tests.conftest import ValidatingClient
from tests.hardware.conftest import env, expect, settled_energy

pytestmark = pytest.mark.hardware


@pytest.fixture(scope="module")
def local(live_client: ValidatingClient, plug_mac: str) -> ValidatingClient:
    if env("TEST_LOCAL") != "1":
        pytest.skip("set TEST_LOCAL=1 to run local hardware tests")
    expect(live_client.get(f"/v1/local/plugs/{plug_mac}"))
    return live_client


def test_discovery_sees_the_plug(live_client: ValidatingClient, plug_mac: str) -> None:
    if env("TEST_LOCAL") != "1":
        pytest.skip("set TEST_LOCAL=1 to run local hardware tests")
    plugs = expect(live_client.get("/v1/local/plugs"))["plugs"]
    macs = {entry["plug"]["mac"]: entry for entry in plugs}
    assert plug_mac in macs, f"{plug_mac} not discovered: {sorted(macs)}"
    assert macs[plug_mac]["supported"] is True


def test_relay_and_energy_scenario(local: ValidatingClient, plug_mac: str) -> None:
    base = f"/v1/local/plugs/{plug_mac}"
    original = expect(local.get(base))["is_on"]
    try:
        assert expect(local.post(f"{base}/relay", json={"state": "on"}))["confirmed"]
        loaded = settled_energy(local, base, loaded=True)
        assert loaded["power_w"] > 1.0, f"plug a lamp into the test plug: {loaded}"
        assert expect(local.post(f"{base}/relay", json={"state": "off"}))["confirmed"]
        idle = settled_energy(local, base, loaded=False)
        assert idle["power_w"] < 1.0, idle
    finally:
        local.post(f"{base}/relay", json={"state": "on" if original else "off"})


def test_settings_round_trip(local: ValidatingClient, plug_mac: str) -> None:
    base = f"/v1/local/plugs/{plug_mac}/settings"
    before = expect(local.get(base))
    try:
        changed = expect(local.patch(base, json={"led_on": not before["led_on"]}))
        assert changed["led_on"] is (not before["led_on"])
    finally:
        local.patch(base, json={"led_on": before["led_on"]})


def test_reboot_recovery(local: ValidatingClient, plug_mac: str) -> None:
    base = f"/v1/local/plugs/{plug_mac}"
    assert local.post(f"{base}/reboot", json={"delay_s": 1}).status_code == 202
    offline = local.get(f"{base}/wait?until=offline&timeout_s=30").json()
    assert offline["satisfied"], offline
    online = local.get(f"{base}/wait?until=online&timeout_s=120").json()
    assert online["satisfied"], online


def test_cloud_command_reaches_the_plug(local: ValidatingClient, plug_mac: str) -> None:
    if not (env("TPLINK_USERNAME") and env("TPLINK_PASSWORD")):
        pytest.skip("set TPLINK_USERNAME and TPLINK_PASSWORD")
    if local.post("/v1/cloud/session", json={}).status_code != 200:
        pytest.skip("cloud login unavailable (MFA or credentials)")
    local.post(f"/v1/local/plugs/{plug_mac}/relay", json={"state": "off"})
    try:
        sent = local.post(
            f"/v1/cloud/plugs/{plug_mac}/relay", json={"state": "on", "verify": False}
        ).json()
        waited = local.get(
            f"/v1/local/plugs/{plug_mac}/wait?until=on&timeout_s=10"
        ).json()
        assert waited["satisfied"], waited
        assert waited["satisfied_at"] >= sent["sent_at"]
    finally:
        local.post(f"/v1/local/plugs/{plug_mac}/relay", json={"state": "off"})
