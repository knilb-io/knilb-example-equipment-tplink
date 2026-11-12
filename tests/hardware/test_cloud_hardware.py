"""SPEC §10 hardware scenario through the TP-Link cloud (tplink-cloud-api)."""

from __future__ import annotations

import pytest

from tests.conftest import ValidatingClient
from tests.hardware.conftest import env, expect, settled_energy

pytestmark = pytest.mark.hardware


@pytest.fixture(scope="module")
def cloud(live_client: ValidatingClient) -> ValidatingClient:
    if not (env("TPLINK_USERNAME") and env("TPLINK_PASSWORD")):
        pytest.skip("set TPLINK_USERNAME and TPLINK_PASSWORD")
    response = live_client.post("/v1/cloud/session", json={})
    if response.status_code == 503 and response.json()["code"] == "cloud_mfa_required":
        pytest.skip("this account uses MFA; the hardware tests cannot type the code")
    expect(response)
    return live_client


def test_plug_is_in_the_account(cloud: ValidatingClient, plug_mac: str) -> None:
    listed = expect(cloud.get("/v1/cloud/plugs?refresh=true"))
    plugs = {entry["plug"]["mac"]: entry for entry in listed["plugs"]}
    assert plug_mac in plugs, (
        f"{plug_mac} is in neither the Kasa nor the Tapo cloud list: {sorted(plugs)}"
    )
    assert plugs[plug_mac]["online"] is True


def test_relay_and_energy_scenario(cloud: ValidatingClient, plug_mac: str) -> None:
    base = f"/v1/cloud/plugs/{plug_mac}"
    original = expect(cloud.get(base))["is_on"]
    try:
        on = expect(
            cloud.post(f"{base}/relay", json={"state": "on", "settle_ms": 2000})
        )
        assert on["confirmed"] is True, on
        loaded = settled_energy(cloud, base, loaded=True)
        assert loaded["power_w"] > 1.0, f"plug a lamp into the test plug: {loaded}"
        off = expect(
            cloud.post(f"{base}/relay", json={"state": "off", "settle_ms": 2000})
        )
        assert off["confirmed"] is True, off
        idle = settled_energy(cloud, base, loaded=False)
        assert idle["power_w"] < 1.0, idle
    finally:
        cloud.post(f"{base}/relay", json={"state": "on" if original else "off"})


def test_info_and_schedule(cloud: ValidatingClient, plug_mac: str) -> None:
    info = expect(cloud.get(f"/v1/cloud/plugs/{plug_mac}/info"))
    assert info["has_energy_monitoring"] is True
    assert info["account_entry"]["online"] is True
    expect(cloud.get(f"/v1/cloud/plugs/{plug_mac}/schedule"))


def test_f14_led_change_through_the_library(
    cloud: ValidatingClient, plug_mac: str
) -> None:
    """Record what the real plug does with tplink-cloud-api's LED payload (F14)."""
    base = f"/v1/cloud/plugs/{plug_mac}/settings"
    before = expect(cloud.get(base))["led_on"]
    response = cloud.patch(base, json={"led_on": not before})
    after = expect(cloud.get(base))["led_on"]
    assert response.status_code == 502, response.json()
    assert after == before, "the LED changed: update FINDINGS F14"
