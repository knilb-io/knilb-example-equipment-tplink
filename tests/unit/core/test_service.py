"""Service behaviour that is hard to reach through HTTP: timeouts and waits."""

from __future__ import annotations

import asyncio

import pytest

from equipment_tplink.core.errors import PlugError
from equipment_tplink.core.service import PlugService, ServiceTimings
from equipment_tplink.kit.clock import SystemClock


async def test_bounded_maps_timeout() -> None:
    service = PlugService.__new__(PlugService)
    service.kind = "local"

    async def slow() -> None:
        await asyncio.sleep(1)

    with pytest.raises(PlugError) as error:
        await service.bounded(0.01, slow)
    assert error.value.code == "timeout"


async def test_wait_counts_poll_errors(client_factory: None = None) -> None:
    from equipment_tplink.app import create_app
    from equipment_tplink.config import Settings
    from equipment_tplink.fakes.world import FakeWorld
    from equipment_tplink.kit.clock import FakeClock

    clock = FakeClock()
    world = FakeWorld(clock)
    app = create_app(Settings(), fake=True, clock=clock, world=world)
    service: PlugService = app.state.services["local"]
    world.reboot(world.by_mac("00:00:5E:00:53:01"), 0)
    world.reboot_offline_s = 999
    world.by_mac("00:00:5E:00:53:01").offline_until = 999
    result = await service.wait("192.0.2.10", "on", window_s=1)
    assert not result.satisfied
    assert result.poll_errors == result.polls
    assert (
        result.last_error is not None and result.last_error.code == "device_unreachable"
    )


def test_timings_are_frozen() -> None:
    timings = ServiceTimings(1, 0.1, 500)
    with pytest.raises(AttributeError):
        timings.call_timeout_s = 2  # type: ignore[misc]
    assert isinstance(SystemClock(), SystemClock)
