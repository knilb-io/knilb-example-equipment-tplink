"""A python-kasa transport that answers IOT requests from a :class:`FakeWorld` plug.

The *real* ``IotProtocol`` and ``IotPlug`` run on top of it: request batching,
module parsing, retries (F18), error wrapping (F17) and the ``Rule`` model (F20).
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from kasa import AuthenticationError, DeviceConfig, IotProtocol
from kasa.deviceconfig import DeviceConnectionParameters
from kasa.exceptions import _ConnectionError
from kasa.iot import IotPlug
from kasa.transports import BaseTransport

from equipment_tplink.fakes.world import FakePlug, FakeWorld

TIMEZONE_INDEX = 17  # Etc/GMT+5: the plug's clock runs at UTC-5
TZ_OFFSET = timedelta(hours=-5)


class WorldTransport(BaseTransport):
    """Kasa firmware behaviour for one plug, reached over an imaginary KLAP link."""

    def __init__(
        self, *, config: DeviceConfig, world: FakeWorld, plug: FakePlug
    ) -> None:
        super().__init__(config=config)
        self.world = world
        self.plug = plug
        self.sent: list[dict[str, Any]] = []
        self.alias_error: int | None = None

    @property
    def default_port(self) -> int:
        return 80

    @property
    def credentials_hash(self) -> str | None:
        return None

    async def close(self) -> None:
        return None

    async def reset(self) -> None:
        return None

    async def send(self, request: str) -> dict[str, Any]:
        payload = json.loads(request)
        self.sent.append(payload)
        world, plug = self.world, self.plug
        if not world.online(plug) or plug.host != self._host:
            raise _ConnectionError(f"Unable to connect to the device: {self._host}:80")
        if not world.accept_local_credentials:
            raise AuthenticationError(
                f"Device response did not match our challenge on ip {self._host}"
            )
        return {
            module: self._module(module, methods) for module, methods in payload.items()
        }

    def _module(self, module: str, methods: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "system",
            "emeter",
            "schedule",
            "anti_theft",
            "time",
            "cnCloud",
        }
        if module not in handlers:
            return {"err_code": -1, "err_msg": "module not support"}
        return {
            method: self._method(module, method, args)
            for method, args in methods.items()
        }

    def _method(self, module: str, method: str, args: Any) -> dict[str, Any]:
        world, plug = self.world, self.plug
        ok = {"err_code": 0}
        match (module, method):
            case ("system", "get_sysinfo"):
                return world.sysinfo(plug)
            case ("system", "set_relay_state"):
                world.set_relay(plug, bool(args["state"]))
                return ok
            case ("system", "set_dev_alias"):
                if self.alias_error is not None:
                    return {"err_code": self.alias_error, "err_msg": "invalid argument"}
                plug.alias = args["alias"]
                return ok
            case ("system", "set_led_off"):
                plug.led_on = not args["off"]
                return ok
            case ("system", "reboot"):
                world.reboot(plug, args.get("delay", 1))
                return ok
            case ("system", "reset"):
                world.factory_reset(plug)
                return ok
            case ("emeter", "get_realtime"):
                power = world.power_w(plug)
                return {
                    "power_mw": int(power * 1000),
                    "voltage_mv": int(plug.voltage_v * 1000),
                    "current_ma": int(power / plug.voltage_v * 1000),
                    "total_wh": int(plug.total_kwh * 1000),
                    **ok,
                }
            case ("emeter" | "schedule", "get_daystat"):
                return {"day_list": [], **ok}
            case ("emeter" | "schedule", "get_monthstat"):
                return {"month_list": [], **ok}
            case ("schedule", "get_rules"):
                return {"rule_list": plug.rules, "enable": 1, "version": 2, **ok}
            case ("anti_theft", "get_rules"):
                return {"rule_list": [], "enable": 0, "version": 2, **ok}
            case ("schedule", "get_next_action"):
                return {"type": -1, **ok}
            case ("time", "get_time"):
                local = world.clock.now() + TZ_OFFSET
                return {
                    "year": local.year,
                    "month": local.month,
                    "mday": local.day,
                    "hour": local.hour,
                    "min": local.minute,
                    "sec": local.second,
                    **ok,
                }
            case ("time", "get_timezone"):
                return {"index": TIMEZONE_INDEX, **ok}
            case ("cnCloud", "get_info"):
                return {
                    "username": world.account.username if plug.bound else "",
                    "server": "n-devs.tplinkcloud.com",
                    "binded": int(plug.bound),
                    "cld_connection": int(plug.cloud_connected),
                    "illegalType": 0,
                    "stopConnect": 0,
                    "tcspStatus": 1,
                    "fwDlPage": "",
                    "tcspInfo": "",
                    "fwNotifyType": -1,
                    **ok,
                }
        return {"err_code": -2, "err_msg": "member not support"}


def real_plug(world: FakeWorld, plug: FakePlug, host: str | None = None) -> IotPlug:
    """A real python-kasa ``IotPlug`` wired to ``plug`` in ``world``."""
    host = host or plug.host
    config = DeviceConfig(
        host=host,
        connection_type=DeviceConnectionParameters.from_values(
            "IOT.SMARTPLUGSWITCH", "KLAP", login_version=2, http_port=80
        ),
    )
    transport = WorldTransport(config=config, world=world, plug=plug)
    device = IotPlug(host, protocol=IotProtocol(transport=transport))
    device.update_from_discover_info(
        {
            "device_type": "IOT.SMARTPLUGSWITCH",
            "device_model": plug.model,
            "ip": host,
            "mac": plug.mac.replace(":", "-"),
            "mgt_encrypt_schm": {
                "is_support_https": False,
                "encrypt_type": "KLAP",
                "http_port": 80,
                "lv": 2,
            },
        }
    )
    return device
