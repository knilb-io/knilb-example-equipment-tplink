"""The local driver against *real* python-kasa device classes over a fake transport.

Pins FINDINGS F17 (hidden error codes), F18 (internal retries), F20 (rule offsets)
and F21 (device id), and catches drift between ``fakes.kasa`` and python-kasa.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from kasa import TimeoutError as KasaTimeoutError

from equipment_tplink.app import LibraryHooks, create_app
from equipment_tplink.config import Settings
from equipment_tplink.fakes.cloud import fake_manager_factory
from equipment_tplink.fakes.world import FakeWorld
from equipment_tplink.kit.clock import FakeClock
from tests.conformance.kasa_transport import WorldTransport, real_plug
from tests.conftest import MAC1, MAC2, OpenApiValidator, ValidatingClient


class Library:
    """Real IotPlug objects, created the way python-kasa discovery would."""

    def __init__(self, world: FakeWorld) -> None:
        self.world = world

    async def discover(self, **_: Any) -> dict[str, Any]:
        found = {}
        for plug in self.world.plugs:
            if self.world.online(plug):
                found[plug.host] = self._make(plug)
        return found

    async def discover_single(self, host: str, **_: Any) -> Any:
        plug = self.world.by_host(host)
        if plug is None or not self.world.online(plug):
            raise KasaTimeoutError(f"Timed out getting discovery response for {host}")
        return self._make(plug)

    def _make(self, plug: Any) -> Any:
        return real_plug(self.world, plug)


def transport_in_use(client: ValidatingClient, mac: str) -> WorldTransport:
    """The transport of the device the local driver has cached for ``mac``."""
    app: Any = client.client.app
    driver = app.state.services["local"].driver
    transport: WorldTransport = driver._devices[mac].device.protocol._transport
    return transport


@pytest.fixture
def library(world: FakeWorld) -> Library:
    return Library(world)


@pytest.fixture
def real_client(
    library: Library, world: FakeWorld, clock: FakeClock, validator: OpenApiValidator
) -> Iterator[ValidatingClient]:
    app = create_app(
        Settings(),
        clock=clock,
        hooks=LibraryHooks(
            discover=library.discover,
            discover_single=library.discover_single,
            manager_factory=fake_manager_factory(world),
        ),
    )
    with TestClient(app) as test_client:
        yield ValidatingClient(test_client, validator)


def test_state_relay_energy(real_client: ValidatingClient, clock: FakeClock) -> None:
    relay = real_client.post(
        f"/v1/local/plugs/{MAC1}/relay", json={"state": "on"}
    ).json()
    assert relay["confirmed"] is True
    ops = [c["op"] for c in relay["upstream"]["calls"]]
    assert ops == ["discover", "turn_on", "update"]  # resolve, command, verify
    clock.advance(90)
    state = real_client.get(f"/v1/local/plugs/{MAC1}").json()
    assert state["is_on"] is True and state["rssi"] == -52
    # python-kasa computes on_since from the plug's local clock (UTC-5 here).
    assert state["on_since"] == "2026-09-22T12:00:00.000Z"
    assert state["upstream"]["connection"] == {
        "host": "192.0.2.10",
        "port": 80,
        "encrypt_type": "KLAP",
        "login_version": 2,
    }
    energy = real_client.get(f"/v1/local/plugs/{MAC1}/energy").json()
    assert energy["power_w"] == pytest.approx(60.0)
    assert energy["current_a"] == pytest.approx(0.5)
    assert energy["energy_total_kwh"] == pytest.approx(12.345)


def test_info_schedule_and_settings(real_client: ValidatingClient) -> None:
    info = real_client.get(f"/v1/local/plugs/{MAC1}/info").json()
    assert info["cloud_link"] == {
        "bound": True,
        "connected": True,
        "server": "n-devs.tplinkcloud.com",
        "username_masked": "u***@example.com",
    }
    # F21: the device id comes from sysinfo, not python-kasa's device_id (the MAC).
    assert info["plug"]["device_id"] == "8006F1A2B3C4D5E6F708192A3B4C5D6E7F801234"
    rules = real_client.get(f"/v1/local/plugs/{MAC1}/schedule").json()["rules"]
    assert rules[0]["start"]["offset_minutes"] is None  # F20
    assert rules[0]["days"] == ["mon", "tue", "wed", "thu", "fri"]
    changed = real_client.patch(
        f"/v1/local/plugs/{MAC1}/settings", json={"alias": "Desk", "led_on": False}
    ).json()
    assert (changed["alias"], changed["led_on"]) == ("Desk", False)


def test_discovery_and_auth_failure(
    real_client: ValidatingClient, world: FakeWorld
) -> None:
    world.accept_local_credentials = False
    discovered = real_client.get("/v1/local/plugs").json()["plugs"]
    assert {p["plug"]["mac"] for p in discovered} == {MAC1, MAC2}
    assert all(p["auth"] == "failed" for p in discovered)
    assert discovered[0]["connection"]["login_version"] == 2
    response = real_client.get(f"/v1/local/plugs/{MAC1}")
    assert response.status_code == 502
    assert response.json()["code"] == "device_auth_failed"


def test_f17_command_errors_are_wrapped(
    real_client: ValidatingClient, library: Library, world: FakeWorld
) -> None:
    real_client.get(f"/v1/local/plugs/{MAC1}")
    transport_in_use(real_client, MAC1).alias_error = -3
    response = real_client.patch(
        f"/v1/local/plugs/{MAC1}/settings", json={"alias": "x"}
    )
    body = response.json()
    assert body["code"] == "device_error"
    assert body["upstream"]["calls"][-1]["error_code"] is None  # -3 is not exposed
    assert "-3" in body["upstream"]["calls"][-1]["error_message"]
    world.accept_local_credentials = False
    wrapped = real_client.post(f"/v1/local/plugs/{MAC1}/relay", json={"state": "on"})
    assert wrapped.json()["code"] == "device_auth_failed"
    assert wrapped.json()["detail"]["chain"][:2] == [
        "KasaException",
        "AuthenticationError",
    ]


def test_f18_library_retries_connection_errors(
    real_client: ValidatingClient, library: Library, world: FakeWorld
) -> None:
    real_client.get(f"/v1/local/plugs/{MAC1}")
    transport = transport_in_use(real_client, MAC1)
    sent_before = len(transport.sent)
    world.by_mac(MAC1).offline_until = 999
    response = real_client.get(f"/v1/local/plugs/{MAC1}")
    assert response.json()["code"] == "device_unreachable"
    assert len(transport.sent) - sent_before == 4  # 1 try + 3 python-kasa retries


def test_reboot_and_factory_reset(
    real_client: ValidatingClient, world: FakeWorld
) -> None:
    assert real_client.post(f"/v1/local/plugs/{MAC2}/reboot").status_code == 202
    back = real_client.get(
        f"/v1/local/plugs/{MAC2}/wait?until=online&timeout_s=10"
    ).json()
    assert back["satisfied"] is True
    reset = real_client.post(
        f"/v1/local/plugs/{MAC1}/factory-reset", json={"confirm_mac": MAC1}
    )
    assert reset.status_code == 202
    assert world.by_mac(MAC1).bound is False
