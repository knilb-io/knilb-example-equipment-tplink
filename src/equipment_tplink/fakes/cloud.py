"""Fake tplink-cloud-api: ``TPLinkDeviceManager`` and Kasa device objects.

Faithful to the library's public behaviour, including its limitations: failed device
calls return ``None`` without a reason (FINDINGS F11), an unauthenticated device list
is empty (F12), and MFA completes only through ``mfa_callback`` inside ``login()``.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from tplinkcloud.exceptions import (
    TPLinkAuthError,
    TPLinkMFARequiredError,
    TPLinkTokenExpiredError,
)

from equipment_tplink.fakes.world import FakePlug, FakeWorld

_MODELS_WITH_EMETER = ("HS110", "KP115", "KP125")


class FakeCloudDevice:
    """Mimics ``tplinkcloud.kp115.KP115`` (and friends)."""

    def __init__(self, world: FakeWorld, plug: FakePlug, token: str) -> None:
        self._world = world
        self._plug = plug
        self._token = token
        self.cloud_type = plug.cloud
        self.device_id = plug.device_id
        self.device_info = SimpleNamespace(
            device_id=plug.device_id,
            alias=plug.alias,
            device_mac=plug.mac.replace(":", ""),
            device_model=plug.model,
            device_hw_ver=plug.hw_ver,
            fw_ver=plug.sw_ver,
            device_region="us-east-1",
            app_server_url="https://n-use1-wap.tplinkcloud.com",
            status=1 if world.online(plug) else 0,
        )

    def get_alias(self) -> str:
        """Alias as listed."""
        alias: str = self.device_info.alias
        return alias

    def has_emeter(self) -> bool:
        """Energy monitoring, decided by model like the library's class map."""
        return self._plug.model.startswith(_MODELS_WITH_EMETER)

    async def get_sys_info(self) -> Any:
        """A ``KP115SysInfo``-like object, or ``None``."""
        if not self._reachable("get_sys_info"):
            return None
        raw = self._world.sysinfo(self._plug)
        return SimpleNamespace(
            err_code=0,
            sw_ver=raw["sw_ver"],
            hw_ver=raw["hw_ver"],
            model=raw["model"],
            mac=raw["mac"],
            device_id=raw["deviceId"],
            alias=raw["alias"],
            relay_state=raw["relay_state"],
            on_time=raw["on_time"],
            rssi=raw["rssi"],
            led_off=raw["led_off"],
            feature=raw["feature"],
        )

    async def power_on(self) -> dict[str, Any] | None:
        """Switch on through the cloud."""
        if not self._reachable("power_on"):
            return None
        self._world.set_relay(self._plug, True, via_cloud=True)
        return {"err_code": 0}

    async def power_off(self) -> dict[str, Any] | None:
        """Switch off through the cloud."""
        if not self._reachable("power_off"):
            return None
        self._world.set_relay(self._plug, False, via_cloud=True)
        return {"err_code": 0}

    async def get_power_usage_realtime(self) -> Any:
        """A ``CurrentPower``-like object, or ``None``."""
        if not self._reachable("get_power_usage_realtime"):
            return None
        power = self._world.power_w(self._plug)
        return SimpleNamespace(
            voltage_mv=int(self._plug.voltage_v * 1000),
            current_ma=int(power / self._plug.voltage_v * 1000),
            power_mw=int(power * 1000),
            total_wh=int(self._plug.total_kwh * 1000),
            err_code=0,
        )

    async def get_schedule_rules(self) -> Any:
        """A ``DeviceScheduleRules``-like object, or ``None``."""
        if not self._reachable("get_schedule_rules"):
            return None
        rules = [_FakeCloudRule(rule) for rule in self._plug.rules]
        return SimpleNamespace(rules=rules, enable=1, version=2, err_code=0)

    async def set_led_state(self, on: bool) -> dict[str, Any] | None:
        """Always ``None``, like the library (FINDINGS F14).

        tplink-cloud-api sends ``{"set_led_off": {"off": n}}`` and reads
        ``response["set_led_off"]["off"]``, which a Kasa plug never returns.
        """
        self._reachable("set_led_state")
        return None

    def _reachable(self, what: str) -> bool:
        self._world.calls.append(f"cloud:{what}:{self._plug.mac}")
        account = self._world.account
        return (
            not account.token_expired
            and self._token == _token_for(account.username, self._plug.cloud)
            and self._world.online(self._plug)
        )


class _FakeCloudRule:
    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = dict(raw)

    def to_json(self) -> dict[str, Any]:
        return dict(self._raw)


class FakeDeviceManager:
    """Mimics ``tplinkcloud.TPLinkDeviceManager`` (Kasa cloud, optionally Tapo)."""

    def __init__(
        self,
        world: FakeWorld,
        username: str | None = None,
        password: str | None = None,
        prefetch: bool = True,
        cache_devices: bool = True,
        tplink_cloud_api_host: str | None = None,
        verbose: bool = False,
        term_id: str | None = None,
        mfa_callback: Callable[[str, str], str] | None = None,
        include_tapo: bool = True,
    ) -> None:
        self._world = world
        self._token: str | None = None
        self._tapo_token: str | None = None
        self.term_id = term_id
        self.include_tapo = include_tapo
        self.cache_devices = cache_devices
        if username and password:
            self.login(username, password, mfa_callback=mfa_callback)
            if include_tapo:
                self._tapo_login(username, password)

    def _tapo_login(self, username: str, password: str) -> None:
        """Like the library: a failed Tapo login is swallowed (FINDINGS F7)."""
        self._world.calls.append("cloud:tapo_login")
        account = self._world.account
        if account.tapo_login_ok and (username, password) == (
            account.username,
            account.password,
        ):
            self._tapo_token = _token_for(username, "tapo")

    def login(
        self,
        username: str,
        password: str,
        mfa_callback: Callable[[str, str], str] | None = None,
    ) -> str:
        """Blocking login, like the library (runs the MFA callback inline)."""
        self._world.calls.append("cloud:login")
        if not username or not password:
            raise ValueError("Username and password are required")
        account = self._world.account
        if username != account.username or password != account.password:
            raise TPLinkAuthError("Incorrect email or password", error_code=-20601)
        if account.kasa_nested_mfa:
            return None  # type: ignore[return-value]  # the library returns no token
        if account.mfa_code is not None:
            if mfa_callback is None:
                raise TPLinkMFARequiredError(
                    "MFA verification required. Provide an mfa_callback.",
                    error_code=-20677,
                    mfa_type="verifyCodeLogin",
                    email=username,
                )
            code = mfa_callback("verifyCodeLogin", username)
            if code != account.mfa_code:
                raise TPLinkAuthError("Incorrect verification code", error_code=-20676)
        self._token = _token_for(username, "kasa")
        return self._token

    def get_token(self) -> str | None:
        """Current Kasa cloud token."""
        return self._token

    def get_tapo_token(self) -> str | None:
        """Current Tapo cloud token."""
        return self._tapo_token

    async def get_devices(self) -> list[FakeCloudDevice]:
        """List devices of each logged-in cloud (none without a token, F12)."""
        self._world.calls.append("cloud:get_devices")
        account = self._world.account
        if account.list_error is not None:
            raise account.list_error
        tokens = {"kasa": self._token, "tapo": self._tapo_token}
        if not any(tokens.values()):
            return []
        if account.token_expired:
            raise TPLinkTokenExpiredError("Refresh token expired", error_code=-20655)
        return [
            FakeCloudDevice(self._world, plug, token)
            for plug in self._world.plugs
            if plug.listed and (token := tokens.get(plug.cloud))
        ]


def fake_manager_factory(world: FakeWorld) -> Callable[..., FakeDeviceManager]:
    """Return a stand-in for the ``TPLinkDeviceManager`` constructor."""

    def factory(**kwargs: Any) -> FakeDeviceManager:
        return FakeDeviceManager(world, **kwargs)

    return factory


def _token_for(username: str, cloud: str) -> str:
    return f"fake-{cloud}-token-for-{username}"
