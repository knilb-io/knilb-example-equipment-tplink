"""Local driver against the fake python-kasa surface, plus error classification."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from kasa import AuthenticationError, Credentials, KasaException, UnsupportedDeviceError
from kasa import TimeoutError as KasaTimeoutError

from equipment_tplink.core.directory import Directory
from equipment_tplink.core.errors import PlugError
from equipment_tplink.core.identity import PlugRef
from equipment_tplink.core.models import PlugIdentity
from equipment_tplink.drivers.local import (
    LocalKasaDriver,
    _disconnect,
    _utc,
    classify_kasa_error,
)
from equipment_tplink.fakes.kasa import (
    FakeKasaDevice,
    fake_discover,
    fake_discover_single,
)
from equipment_tplink.fakes.world import FakeWorld
from equipment_tplink.kit.clock import FakeClock
from tests.conftest import MAC1, MAC2


def make_driver(world: FakeWorld, clock: FakeClock, **kwargs: Any) -> LocalKasaDriver:
    return LocalKasaDriver(
        directory=Directory(clock),
        clock=clock,
        credentials=Credentials("user@example.com", "pw"),
        discovery_target="192.0.2.255",
        discovery_timeout_s=1,
        call_timeout_s=5,
        discover=kwargs.pop("discover", fake_discover(world)),
        discover_single=fake_discover_single(world),
        **kwargs,
    )


class _ConnectionError(KasaException):
    """Named like python-kasa's private connection error."""


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (AuthenticationError("bad hash"), "device_auth_failed"),
        (UnsupportedDeviceError("nope"), "unsupported"),
        (KasaTimeoutError("slow"), "timeout"),
        (_ConnectionError("gone"), "device_unreachable"),
        (KasaException("weird"), "device_error"),
    ],
)
def test_classify(exc: Exception, code: str) -> None:
    assert classify_kasa_error(exc, plug=None).code == code


def test_classify_walks_the_chain() -> None:
    try:
        try:
            raise AuthenticationError("inner")
        except AuthenticationError as inner:
            raise KasaException(
                "Communication error on system:set_relay_state"
            ) from inner
    except KasaException as outer:
        error = classify_kasa_error(outer, plug=None)
    assert error.code == "device_auth_failed"
    assert error.fields["detail"]["chain"] == ["KasaException", "AuthenticationError"]


async def test_discovery_with_unsupported_device(
    world: FakeWorld, clock: FakeClock
) -> None:
    inner = fake_discover(world)

    async def discover(**kwargs: Any) -> dict[str, Any]:
        error = UnsupportedDeviceError("SMART.TAPOPLUG not supported")
        error.host = "192.0.2.50"
        error.discovery_result = {
            "mac": "00-00-5E-00-53-50",
            "device_model": "P110(US)",
        }
        await kwargs["on_unsupported"](error)
        return await inner(**kwargs)

    driver = make_driver(world, clock, discover=discover)
    result = await driver.discover_plugs(timeout_s=1)
    unsupported = [p for p in result.plugs if not p.supported]
    assert unsupported[0].plug.model == "P110(US)"
    assert unsupported[0].auth == "not_attempted"


async def test_probe_and_errors(world: FakeWorld, clock: FakeClock) -> None:
    driver = make_driver(world, clock)
    plug = await driver.resolve(PlugRef("mac", MAC1))
    assert (await driver.probe(plug)).online
    world.accept_local_credentials = False
    observed = await driver.probe(plug)
    assert observed.online and observed.authenticated is False
    world.accept_local_credentials = True
    world.reboot(world.by_mac(MAC1), 0)
    assert not (await driver.probe(plug)).online
    await clock.sleep(5)

    async def broken() -> None:
        raise KasaException("garbled reply")

    device = await driver._device_for(plug)
    device.update = broken
    with pytest.raises(PlugError) as error:
        await driver.probe(plug)
    assert error.value.code == "device_error"


async def test_modules_missing(
    world: FakeWorld, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = make_driver(world, clock)
    plug = await driver.resolve(PlugRef("mac", MAC2))
    monkeypatch.setattr(FakeKasaDevice, "modules", property(lambda self: {}))
    for call in (
        driver.read_schedule(plug),
        driver.write_settings(plug, alias=None, led_on=True),
    ):
        with pytest.raises(PlugError) as error:
            await call
        assert error.value.code == "unsupported"
    settings = await driver.read_settings(plug)
    assert settings.led_on is True
    info = await driver.read_info(plug)
    assert info.cloud_link is None and info.has_energy_monitoring is False


async def test_energy_without_value(
    world: FakeWorld, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = make_driver(world, clock)
    plug = await driver.resolve(PlugRef("mac", MAC1))
    energy = SimpleNamespace(current_consumption=None)
    monkeypatch.setattr(
        FakeKasaDevice, "modules", property(lambda self: {"Energy": energy})
    )
    with pytest.raises(PlugError) as error:
        await driver.read_energy(plug)
    assert error.value.code == "device_error"


async def test_no_host_and_idle_eviction(world: FakeWorld, clock: FakeClock) -> None:
    driver = make_driver(world, clock, idle_s=10)
    with pytest.raises(PlugError) as error:
        await driver.read_state(PlugIdentity(mac=MAC1))
    assert error.value.code == "plug_not_found"
    plug = await driver.resolve(PlugRef("ipv4", "192.0.2.10"))
    first = await driver._device_for(plug)
    clock.advance(11)
    second = await driver._device_for(plug)
    assert first is not second
    await driver.close()


async def test_disconnect_is_best_effort() -> None:
    class Broken:
        async def disconnect(self) -> None:
            raise OSError("already closed")

    await _disconnect(Broken())


def test_utc() -> None:
    converted = _utc(datetime(2026, 1, 1))
    assert converted is not None and converted.tzinfo is UTC


async def test_discovery_fits_the_budget(world: FakeWorld, clock: FakeClock) -> None:
    durations: list[int] = []
    inner = fake_discover(world)

    async def discover(**kwargs: Any) -> dict[str, Any]:
        durations.append(kwargs["discovery_timeout"])
        return await inner(**kwargs)

    driver = make_driver(world, clock, discover=discover)
    driver.discovery_timeout_s = 5
    await driver.resolve(PlugRef("mac", MAC1), budget_s=2.5)
    await driver.resolve(PlugRef("mac", MAC2), rediscover=True)
    assert durations == [2, 5]


async def test_rediscovery_keeps_the_live_session(
    world: FakeWorld, clock: FakeClock
) -> None:
    driver = make_driver(world, clock)
    plug = await driver.resolve(PlugRef("mac", MAC1))
    live = await driver._device_for(plug)
    await driver.discover_plugs(timeout_s=1)
    assert await driver._device_for(plug) is live
