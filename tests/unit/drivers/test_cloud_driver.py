"""Cloud driver and session against the fake tplink-cloud-api surface."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from tplinkcloud.exceptions import TPLinkCloudError

from equipment_tplink.core.directory import Directory
from equipment_tplink.core.errors import PlugError
from equipment_tplink.core.identity import PlugRef
from equipment_tplink.core.models import CloudState, PlugIdentity
from equipment_tplink.drivers.cloud import CloudTPLinkDriver
from equipment_tplink.drivers.cloud_session import CloudSessionManager, default_term_id
from equipment_tplink.fakes.cloud import FakeDeviceManager, fake_manager_factory
from equipment_tplink.fakes.world import FakePlug, FakeWorld
from equipment_tplink.kit.clock import FakeClock
from tests.conftest import MAC1, MAC2


@pytest.fixture
async def session(
    world: FakeWorld, clock: FakeClock
) -> AsyncIterator[CloudSessionManager]:
    manager = CloudSessionManager(
        username=world.account.username,
        password=world.account.password,
        clock=clock,
        manager_factory=fake_manager_factory(world),
        mfa_wait_s=0.2,
    )
    yield manager
    await manager.close()


@pytest.fixture
async def driver(
    session: CloudSessionManager, world: FakeWorld, clock: FakeClock
) -> CloudTPLinkDriver:
    await session.login(None, timeout=5)
    return CloudTPLinkDriver(session=session, directory=Directory(clock), clock=clock)


class Timeout(Exception):
    """Named like ``requests.exceptions.Timeout``."""


async def test_list_cache_and_directory(
    driver: CloudTPLinkDriver, clock: FakeClock
) -> None:
    first = await driver.list_plugs(refresh=False)
    assert not first.cached and [p.plug.mac for p in first.plugs] == [MAC1, MAC2]
    assert (await driver.list_plugs(refresh=False)).cached
    clock.advance(61)
    assert not (await driver.list_plugs(refresh=False)).cached


async def test_resolve_refreshes_once_on_miss(
    driver: CloudTPLinkDriver, world: FakeWorld
) -> None:
    await driver.list_plugs(refresh=False)
    world.plugs.append(
        FakePlug(
            mac="00:00:5E:00:53:03",
            host="192.0.2.12",
            device_id="8006F1A2B3C4D5E6F708192A3B4C5D6E7F809999",
            alias="New plug",
        )
    )
    found = await driver.resolve(PlugRef("mac", "00:00:5E:00:53:03"))
    assert found.alias == "New plug"
    with pytest.raises(PlugError) as error:
        await driver.resolve(PlugRef("mac", "00:00:5E:00:53:99"))
    assert error.value.code == "plug_not_found"
    assert "F12" in error.value.message


async def test_token_expiry_requires_login(
    driver: CloudTPLinkDriver, world: FakeWorld, session: CloudSessionManager
) -> None:
    world.account.token_expired = True
    with pytest.raises(PlugError) as error:
        await driver.list_plugs(refresh=True)
    assert error.value.code == "cloud_login_required"
    assert session.state is CloudState.LOGIN_REQUIRED
    with pytest.raises(PlugError):
        await driver.ensure_ready()


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (TPLinkCloudError("server says no", error_code=-20002), "cloud_error"),
        (Timeout("read timed out"), "timeout"),
        (RuntimeError("?"), "cloud_error"),
    ],
)
async def test_list_errors(
    driver: CloudTPLinkDriver, world: FakeWorld, exc: Exception, code: str
) -> None:
    world.account.list_error = exc
    with pytest.raises(PlugError) as error:
        await driver.list_plugs(refresh=True)
    assert error.value.code == code
    if isinstance(exc, TPLinkCloudError):
        assert error.value.fields["detail"]["error_code"] == -20002


async def test_device_call_failures(
    driver: CloudTPLinkDriver, world: FakeWorld
) -> None:
    plug = await driver.resolve(PlugRef("mac", MAC1))
    world.account.token_expired = True  # device objects now return None (F11/F13)
    with pytest.raises(PlugError) as error:
        await driver.read_state(plug)
    assert error.value.code == "cloud_error" and "F11" in error.value.message

    world.account.token_expired = False
    devices, _ = await driver._device_list(refresh=True)

    async def explode() -> None:
        raise OSError("connection reset")

    devices[0].get_sys_info = explode
    with pytest.raises(PlugError) as error:
        await driver.read_state(plug)
    assert error.value.code == "cloud_error"


async def test_energy_without_value(driver: CloudTPLinkDriver) -> None:
    plug = await driver.resolve(PlugRef("mac", MAC1))
    devices, _ = await driver._device_list(refresh=False)

    async def empty_reading() -> Any:
        return type("CurrentPower", (), {"power_mw": None})()

    devices[0].get_power_usage_realtime = empty_reading
    with pytest.raises(PlugError) as error:
        await driver.read_energy(plug)
    assert error.value.code == "device_error"


async def test_unsupported_operations(driver: CloudTPLinkDriver) -> None:
    plug = await driver.resolve(PlugRef("mac", MAC1))
    for call in (driver.reboot(plug, delay_s=1), driver.factory_reset(plug)):
        with pytest.raises(PlugError) as error:
            await call
        assert error.value.code == "unsupported"
    with pytest.raises(PlugError) as error:
        await driver.read_state(PlugIdentity())
    assert error.value.code == "plug_not_found"


async def test_state_with_raw_dict_sysinfo(driver: CloudTPLinkDriver) -> None:
    plug = await driver.resolve(
        PlugRef("device_id", "8006F1A2B3C4D5E6F708192A3B4C5D6E7F801234")
    )
    devices, _ = await driver._device_list(refresh=False)

    async def raw() -> dict[str, Any]:
        return {"relay_state": 1, "on_time": "n/a", "alias": "Raw"}

    devices[0].get_sys_info = raw
    reading = await driver.read_state(plug)
    assert (
        reading.is_on and reading.on_since is None and reading.identity.alias == "Raw"
    )


async def test_mfa_timeout_and_logout_race(world: FakeWorld, clock: FakeClock) -> None:
    world.account.mfa_code = "111111"
    session = CloudSessionManager(
        username=world.account.username,
        password=world.account.password,
        clock=clock,
        manager_factory=fake_manager_factory(world),
        mfa_wait_s=0.05,
    )
    with pytest.raises(PlugError) as error:
        await session.login(None, timeout=5)
    assert error.value.code == "cloud_mfa_required"
    assert session._task is not None
    await asyncio.wait({session._task}, timeout=2)
    assert session.state is CloudState.LOGIN_REQUIRED
    assert session.last_error is not None and "0 s" in session.last_error.message

    session._mfa_wait_s = 5
    with pytest.raises(PlugError):
        await session.login(None, timeout=5)
    await session.logout()
    await asyncio.wait({session._task}, timeout=2)
    assert session.state is CloudState.LOGIN_REQUIRED
    assert session.last_error is None  # the cancelled login did not overwrite state
    await session.close()


async def test_login_errors(world: FakeWorld, clock: FakeClock) -> None:
    def broken_factory(**kwargs: Any) -> FakeDeviceManager:
        raise TPLinkCloudError("region lookup failed", error_code=-20104)

    session = CloudSessionManager(
        username="user@example.com",
        password="pw",
        clock=clock,
        manager_factory=broken_factory,
    )
    with pytest.raises(PlugError) as error:
        await session.login(None, timeout=5)
    assert error.value.code == "cloud_error"
    assert session.state is CloudState.ERROR
    upstream = error.value.fields["upstream"]
    assert upstream.calls[0].error_code == -20104
    with pytest.raises(PlugError) as ready:
        session.require_manager()
    assert ready.value.code == "cloud_login_required"

    def crashing_factory(**kwargs: Any) -> FakeDeviceManager:
        raise ConnectionError("dns lookup failed")

    session._factory = crashing_factory
    with pytest.raises(PlugError):
        await session.login(None, timeout=5)
    assert session.state is CloudState.ERROR


async def test_login_without_any_token(world: FakeWorld, clock: FakeClock) -> None:
    """F23: a Kasa MFA request nested in a success reply leaves no token."""
    world.account.kasa_nested_mfa = True
    world.account.tapo_login_ok = False
    session = CloudSessionManager(
        username=world.account.username,
        password=world.account.password,
        clock=clock,
        manager_factory=fake_manager_factory(world),
    )
    with pytest.raises(PlugError) as error:
        await session.login(None, timeout=5)
    assert error.value.code == "cloud_login_required"
    assert "F23" in error.value.message
    login_call = error.value.fields["upstream"].calls[0]
    assert login_call.response == {"kasa_token": False, "tapo_token": False}


async def test_tapo_cloud_carries_the_kasa_plug(
    world: FakeWorld, clock: FakeClock
) -> None:
    world.account.kasa_nested_mfa = True
    for fake_plug in world.plugs:
        fake_plug.cloud = "tapo"
    session = CloudSessionManager(
        username=world.account.username,
        password=world.account.password,
        clock=clock,
        manager_factory=fake_manager_factory(world),
    )
    await session.login(None, timeout=5)
    driver = CloudTPLinkDriver(session=session, directory=Directory(clock), clock=clock)
    listed = await driver.list_plugs(refresh=True)
    assert [plug.plug.mac for plug in listed.plugs] == [MAC1, MAC2]
    plug = await driver.resolve(PlugRef("mac", MAC1))
    await driver.set_relay(plug, True)
    assert (await driver.read_state(plug)).is_on


async def test_login_timeout(world: FakeWorld, clock: FakeClock) -> None:
    def slow_factory(**kwargs: Any) -> FakeDeviceManager:
        import time

        time.sleep(0.3)
        return FakeDeviceManager(world, **kwargs)

    session = CloudSessionManager(
        username=world.account.username,
        password=world.account.password,
        clock=clock,
        manager_factory=slow_factory,
    )
    with pytest.raises(PlugError) as error:
        await session.login(None, timeout=0.01)
    assert error.value.code == "timeout"
    await session.close()


async def test_start_without_credentials_and_term_id(clock: FakeClock) -> None:
    session = CloudSessionManager(username=None, password=None, clock=clock)
    session.start()
    assert session.state is CloudState.NOT_CONFIGURED
    assert default_term_id("a@example.com") == default_term_id("a@example.com")
    assert default_term_id("a@example.com") != default_term_id("b@example.com")
