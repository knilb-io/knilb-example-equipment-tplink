"""Fake python-kasa: ``Discover.discover``/``discover_single`` and ``IotPlug``."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from kasa import AuthenticationError, KasaException, Module
from kasa import TimeoutError as KasaTimeoutError

from equipment_tplink.fakes.world import FakePlug, FakeWorld


class FakeKasaDevice:
    """Mimics the parts of ``kasa.iot.IotPlug`` the local driver uses."""

    def __init__(self, world: FakeWorld, plug: FakePlug) -> None:
        self._world = world
        self._plug = plug
        self._host = plug.host
        self._last_update: dict[str, Any] | None = None
        self.config = SimpleNamespace(
            connection_type=SimpleNamespace(
                encryption_type=SimpleNamespace(value=plug.encrypt_type),
                login_version=plug.login_version,
                http_port=80,
            )
        )
        self.disconnected = False

    # -- identity (valid before update, like python-kasa) --------------------------

    @property
    def host(self) -> str:
        """IP address the device was reached at."""
        return self._host

    @property
    def port(self) -> int:
        """KLAP HTTP port."""
        return 80

    @property
    def mac(self) -> str:
        """MAC address, colon-separated."""
        return self._plug.mac

    @property
    def model(self) -> str:
        """Short model name."""
        return self._plug.model.split("(")[0]

    @property
    def alias(self) -> str | None:
        """Alias, only known after update."""
        return self._sys().get("alias") if self._last_update else None

    @property
    def internal_state(self) -> dict[str, Any]:
        """Last update, or the discovery info before the first update."""
        if self._last_update is not None:
            return self._last_update
        return {
            "device_type": "IOT.SMARTPLUGSWITCH",
            "device_model": self._plug.model,
            "device_id": hashlib.md5(self._plug.device_id.encode()).hexdigest(),
            "ip": self._host,
            "mac": self._plug.mac.replace(":", "-"),
            "mgt_encrypt_schm": {
                "is_support_https": False,
                "encrypt_type": self._plug.encrypt_type,
                "http_port": 80,
                "lv": self._plug.login_version,
            },
            "owner": "7A9F9C1B3D5E7F90A1B2C3D4E5F60718",
            "model": self.model,
        }

    # -- state (requires update) ---------------------------------------------------

    @property
    def sys_info(self) -> dict[str, Any]:
        """Raw sysinfo from the last update."""
        return self._sys()

    @property
    def is_on(self) -> bool:
        """Relay state from the last update."""
        return bool(self._sys()["relay_state"])

    @property
    def on_since(self) -> datetime | None:
        """When the relay turned on."""
        sys = self._sys()
        if not sys["relay_state"]:
            return None
        return self._world.clock.now() - timedelta(seconds=sys["on_time"])

    @property
    def rssi(self) -> int | None:
        """Wi-Fi signal strength."""
        value: int | None = self._sys().get("rssi")
        return value

    @property
    def hw_info(self) -> dict[str, Any]:
        """Hardware details."""
        sys = self._sys()
        return {
            key: sys[key]
            for key in ("sw_ver", "hw_ver", "mac", "hwId", "oemId", "dev_name")
        }

    @property
    def modules(self) -> dict[Any, Any]:
        """Modules registered after update."""
        self._sys()
        plug = self._plug
        modules: dict[Any, Any] = {
            Module.Led: _FakeLed(self),
            Module.IotSchedule: _FakeSchedule(self),
            Module.IotCloud: _FakeCloud(self),
        }
        if plug.has_emeter:
            modules[Module.Energy] = _FakeEnergy(self)
        return modules

    # -- I/O -----------------------------------------------------------------------

    async def update(self) -> None:
        """Query the plug, raising like python-kasa when it can't."""
        self._reach("update")
        plug = self._plug
        realtime = {
            "voltage_mv": int(plug.voltage_v * 1000),
            "current_ma": int(self._world.power_w(plug) / plug.voltage_v * 1000),
            "power_mw": int(self._world.power_w(plug) * 1000),
            "total_wh": int(plug.total_kwh * 1000),
            "err_code": 0,
        }
        self._last_update = {
            "system": {"get_sysinfo": self._world.sysinfo(plug)},
            "emeter": {"get_realtime": realtime} if plug.has_emeter else {},
            "schedule": {"get_rules": {"rule_list": list(plug.rules), "enable": 1}},
            "cnCloud": {
                "get_info": {
                    "binded": 1 if plug.bound else 0,
                    "cld_connection": 1 if plug.cloud_connected else 0,
                    "server": "n-devs.tplinkcloud.com",
                    "username": self._world.account.username if plug.bound else "",
                }
            },
        }

    async def turn_on(self) -> dict[str, Any]:
        """Switch on (errors are wrapped like python-kasa's ``_query_helper``)."""
        self._command("system", "set_relay_state")
        self._world.set_relay(self._plug, True)
        return {}

    async def turn_off(self) -> dict[str, Any]:
        """Switch off."""
        self._command("system", "set_relay_state")
        self._world.set_relay(self._plug, False)
        return {}

    async def set_alias(self, alias: str) -> dict[str, Any]:
        """Rename."""
        self._command("system", "set_dev_alias")
        self._plug.alias = alias
        return {}

    async def reboot(self, delay: int = 1) -> None:
        """Reboot after ``delay`` seconds."""
        self._command("system", "reboot")
        self._world.reboot(self._plug, delay)

    async def factory_reset(self) -> None:
        """Factory reset."""
        self._command("system", "reset")
        self._world.factory_reset(self._plug)

    async def disconnect(self) -> None:
        """Close the transport."""
        self.disconnected = True

    # -- helpers -------------------------------------------------------------------

    def _sys(self) -> dict[str, Any]:
        if self._last_update is None:
            raise KasaException("You need to await update() to access the data")
        value: dict[str, Any] = self._last_update["system"]["get_sysinfo"]
        return value

    def _reach(self, what: str) -> None:
        self._world.calls.append(f"local:{what}:{self._plug.mac}")
        plug = self._world.by_host(self._host)
        if plug is None or plug is not self._plug or not self._world.online(plug):
            raise KasaException(
                f"Unable to connect to the device: {self._host}:80"
            ) from OSError(113, "No route to host")
        if not self._world.accept_local_credentials:
            raise AuthenticationError(
                f"Device response did not match our challenge on ip {self._host}, "
                "check that your e-mail and password (both case-sensitive) are correct."
            )

    def _command(self, module: str, method: str) -> None:
        try:
            self._reach(f"{module}.{method}")
        except KasaException as exc:
            raise KasaException(f"Communication error on {module}:{method}") from exc


class _FakeEnergy:
    def __init__(self, device: FakeKasaDevice) -> None:
        realtime = device.internal_state["emeter"]["get_realtime"]
        self.current_consumption = realtime["power_mw"] / 1000
        self.voltage = realtime["voltage_mv"] / 1000
        self.current = realtime["current_ma"] / 1000
        self.consumption_total = realtime["total_wh"] / 1000


class _FakeLed:
    def __init__(self, device: FakeKasaDevice) -> None:
        self._device = device
        self.led = not device.sys_info["led_off"]

    async def set_led(self, state: bool) -> dict[str, Any]:
        self._device._command("system", "set_led_off")
        self._device._plug.led_on = state
        return {}


class _FakeRule:
    """Like python-kasa's ``Rule``: no ``soffset``/``eoffset`` (FINDINGS F20)."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = {k: v for k, v in raw.items() if k not in ("soffset", "eoffset")}

    def to_dict(self) -> dict[str, Any]:
        return dict(self._raw)


class _FakeSchedule:
    def __init__(self, device: FakeKasaDevice) -> None:
        raw = device.internal_state["schedule"]["get_rules"]["rule_list"]
        self.rules = [_FakeRule(rule) for rule in raw]


class _FakeCloud:
    def __init__(self, device: FakeKasaDevice) -> None:
        raw = device.internal_state["cnCloud"]["get_info"]
        self.info = SimpleNamespace(
            provisioned=raw["binded"],
            cloud_connected=raw["cld_connection"],
            server=raw["server"],
            username=raw["username"],
        )


def fake_discover(world: FakeWorld) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Return a stand-in for ``kasa.Discover.discover`` over ``world``."""

    async def discover(
        *,
        target: str = "255.255.255.255",
        discovery_timeout: int = 5,
        credentials: Any = None,
        timeout: int | None = None,
        on_unsupported: Any = None,
        **_: Any,
    ) -> dict[str, Any]:
        world.calls.append(f"local:discover:{target}")
        await world.clock.sleep(0)
        return {
            plug.host: FakeKasaDevice(world, plug)
            for plug in world.plugs
            if world.online(plug)
        }

    return discover


def fake_discover_single(world: FakeWorld) -> Callable[..., Awaitable[Any]]:
    """Return a stand-in for ``kasa.Discover.discover_single`` over ``world``."""

    async def discover_single(
        host: str,
        *,
        discovery_timeout: int = 5,
        credentials: Any = None,
        timeout: int | None = None,
        **_: Any,
    ) -> Any:
        world.calls.append(f"local:discover_single:{host}")
        plug = world.by_host(host)
        if plug is None or not world.online(plug):
            raise KasaTimeoutError(f"Timed out getting discovery response for {host}")
        return FakeKasaDevice(world, plug)

    return discover_single
