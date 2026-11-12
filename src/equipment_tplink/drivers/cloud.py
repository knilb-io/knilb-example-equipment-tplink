"""Cloud driver: tplink-cloud-api, public API only (SPEC §7.6).

Limitations are surfaced, not patched: device calls return ``None`` without a reason
(FINDINGS F11), a failed list looks empty (F12), tokens refresh only while listing
(F13), and there is no public rename, reboot or reset (F10).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Literal

from tplinkcloud.exceptions import TPLinkCloudError, TPLinkTokenExpiredError

from equipment_tplink.core.directory import CloudEntry, Directory
from equipment_tplink.core.errors import PlugError
from equipment_tplink.core.identity import PlugRef, normalize_mac
from equipment_tplink.core.models import (
    AccountEntry,
    CloudPlug,
    CloudPlugList,
    PlugIdentity,
    Upstream,
    WaitObservation,
)
from equipment_tplink.core.rules import parse_rule
from equipment_tplink.drivers.base import (
    EnergyValues,
    InfoValues,
    ScheduleValues,
    SettingsValues,
    StateReading,
)
from equipment_tplink.drivers.cloud_session import CloudSessionManager
from equipment_tplink.kit.clock import Clock
from equipment_tplink.kit.redact import redact_text
from equipment_tplink.kit.upstream import Recorder, record, recording, to_jsonable

SUPPORTED_MODELS = ("KP115", "HS110", "KP125")
LIST_TTL_S = 60.0


class CloudTPLinkDriver:
    """Talks to plugs through the TP-Link account API."""

    kind: Literal["cloud"] = "cloud"

    def __init__(
        self,
        *,
        session: CloudSessionManager,
        directory: Directory,
        clock: Clock,
        list_ttl_s: float = LIST_TTL_S,
    ) -> None:
        self.session = session
        self._directory = directory
        self._clock = clock
        self._ttl = list_ttl_s
        self._devices: list[Any] = []
        self._fetched_at: datetime | None = None
        self._fetched_mono: float | None = None
        self._generation = -1

    async def ensure_ready(self) -> None:
        """Raise the 503 matching the session state unless logged in."""
        self.session.require_manager()

    # -- listing and resolution ----------------------------------------------------

    async def list_plugs(self, *, refresh: bool) -> CloudPlugList:
        """The account's device list (cached for 60 s)."""
        recorder = Recorder(self._clock)
        with recording(recorder):
            devices, cached = await self._device_list(refresh=refresh)
        assert self._fetched_at is not None
        return CloudPlugList(
            cached=cached,
            fetched_at=self._fetched_at,
            plugs=[self._cloud_plug(device) for device in devices],
            upstream=Upstream(driver="cloud", calls=recorder.calls),
        )

    async def resolve(
        self, ref: PlugRef, *, rediscover: bool = False, budget_s: float | None = None
    ) -> PlugIdentity:
        """Find the device by MAC or deviceId, refreshing the list once on a miss."""
        device = await self._find(ref, refresh=rediscover)
        return _identity(device)

    # -- operations ----------------------------------------------------------------

    async def read_state(self, plug: PlugIdentity) -> StateReading:
        """``get_sys_info``."""
        device = await self._device_for(plug)
        sys_info = await self._sys_info(device, plug)
        identity = _identity(device, sys_info)
        is_on = _int(sys_info.get("relay_state")) == 1
        on_time = sys_info.get("on_time")
        on_since = None
        if is_on and isinstance(on_time, int | float):
            on_since = self._clock.now() - timedelta(seconds=float(on_time))
        return StateReading(
            identity=identity, is_on=is_on, on_since=on_since, rssi=sys_info.get("rssi")
        )

    async def probe(self, plug: PlugIdentity) -> WaitObservation:
        """Online and listed, per a fresh account device list."""
        devices, _ = await self._device_list(refresh=True)
        device = _match(devices, plug.device_id, plug.mac)
        if device is None:
            return WaitObservation(online=False, listed=False)
        return WaitObservation(online=_online(device), listed=True)

    async def set_relay(self, plug: PlugIdentity, on: bool) -> None:
        """``power_on`` / ``power_off``."""
        device = await self._device_for(plug)
        if on:
            await self._call("power_on", plug, device, device.power_on)
        else:
            await self._call("power_off", plug, device, device.power_off)

    async def read_energy(self, plug: PlugIdentity) -> EnergyValues:
        """``get_power_usage_realtime`` (units as the library reports them, F15)."""
        device = await self._device_for(plug)
        if not device.has_emeter():
            raise PlugError(
                "unsupported",
                "tplink-cloud-api reports no energy monitoring for this model",
                driver="cloud",
                plug=_identity(device),
            )
        power = await self._call(
            "get_power_usage_realtime", plug, device, device.get_power_usage_realtime
        )
        if power.power_mw is None:
            raise PlugError(
                "device_error",
                "tplink-cloud-api reported no power value",
                driver="cloud",
                plug=_identity(device),
            )
        return EnergyValues(
            identity=_identity(device),
            power_w=float(power.power_mw) / 1000,
            voltage_v=_milli(power.voltage_mv),
            current_a=_milli(power.current_ma),
            energy_total_kwh=_milli(power.total_wh),
        )

    async def read_info(self, plug: PlugIdentity) -> InfoValues:
        """``get_sys_info`` plus the account list entry."""
        device = await self._device_for(plug)
        sys_info = await self._sys_info(device, plug)
        info = device.device_info
        return InfoValues(
            identity=_identity(device, sys_info),
            hw_version=sys_info.get("hw_ver") or getattr(info, "device_hw_ver", None),
            sw_version=sys_info.get("sw_ver") or getattr(info, "fw_ver", None),
            has_energy_monitoring=bool(device.has_emeter()),
            cloud_link=None,
            account_entry=AccountEntry(
                online=_online(device),
                region=getattr(info, "device_region", None),
                app_server_url=getattr(info, "app_server_url", None),
            ),
        )

    async def read_schedule(self, plug: PlugIdentity) -> ScheduleValues:
        """``get_schedule_rules``."""
        device = await self._device_for(plug)
        rules = await self._call(
            "get_schedule_rules", plug, device, device.get_schedule_rules
        )
        return ScheduleValues(
            identity=_identity(device),
            rules=[parse_rule(rule.to_json()) for rule in rules.rules],
        )

    async def read_settings(self, plug: PlugIdentity) -> SettingsValues:
        """Alias and LED state from ``get_sys_info``."""
        device = await self._device_for(plug)
        sys_info = await self._sys_info(device, plug)
        return SettingsValues(
            identity=_identity(device, sys_info),
            alias=str(sys_info.get("alias") or ""),
            led_on=_int(sys_info.get("led_off")) != 1,
        )

    async def write_settings(
        self, plug: PlugIdentity, *, alias: str | None, led_on: bool | None
    ) -> None:
        """``set_led_state``; renaming has no public API (F10)."""
        device = await self._device_for(plug)
        if alias is not None:
            raise PlugError(
                "unsupported",
                "tplink-cloud-api has no public API to rename a device (FINDINGS F10)",
                driver="cloud",
                plug=_identity(device),
            )
        if led_on is not None:
            await self._call(
                "set_led_state", plug, device, lambda: device.set_led_state(led_on)
            )

    async def reboot(self, plug: PlugIdentity, *, delay_s: int) -> None:
        """Not available: tplink-cloud-api has no public reboot (F10)."""
        raise PlugError(
            "unsupported",
            "tplink-cloud-api has no public reboot (FINDINGS F10)",
            driver="cloud",
        )

    async def factory_reset(self, plug: PlugIdentity) -> None:
        """Not available: tplink-cloud-api has no public factory reset (F10)."""
        raise PlugError(
            "unsupported",
            "tplink-cloud-api has no public factory reset (FINDINGS F10)",
            driver="cloud",
        )

    # -- helpers -------------------------------------------------------------------

    async def _device_list(self, *, refresh: bool) -> tuple[list[Any], bool]:
        manager = self.session.require_manager()
        fresh = (
            self._fetched_mono is not None
            and self._generation == self.session.generation
            and self._clock.monotonic() - self._fetched_mono < self._ttl
        )
        if fresh and not refresh:
            return self._devices, True
        try:
            async with record("get_devices") as call:
                devices = await asyncio.to_thread(_run, manager.get_devices)
                call.response = {"devices": [_summary(device) for device in devices]}
        except TPLinkTokenExpiredError as exc:
            message = redact_text(str(exc))
            self.session.mark_login_required(message)
            raise PlugError(
                "cloud_login_required",
                f"the TP-Link session expired and could not be refreshed: {message}",
                driver="cloud",
                detail={"error_code": exc.error_code},
            ) from exc
        except Exception as exc:
            raise _cloud_error(exc, "get_devices") from exc
        self._devices = list(devices)
        self._fetched_at = self._clock.now()
        self._fetched_mono = self._clock.monotonic()
        self._generation = self.session.generation
        self._directory.replace_cloud(
            [_entry(device, self._fetched_at) for device in self._devices]
        )
        return self._devices, False

    async def _find(self, ref: PlugRef, *, refresh: bool) -> Any:
        device_id = ref.value if ref.kind == "device_id" else None
        mac = ref.value if ref.kind == "mac" else None
        devices, cached = await self._device_list(refresh=refresh)
        device = _match(devices, device_id, mac)
        if device is None and cached:
            devices, _ = await self._device_list(refresh=True)
            device = _match(devices, device_id, mac)
        if device is None:
            raise PlugError(
                "plug_not_found",
                f"{ref.value} is not in the TP-Link account device list "
                "(an empty list can also mean a swallowed cloud error, FINDINGS F12)",
                driver="cloud",
                plug=PlugIdentity(mac=mac, device_id=device_id),
            )
        return device

    async def _device_for(self, plug: PlugIdentity) -> Any:
        if plug.device_id:
            ref = PlugRef("device_id", plug.device_id)
        elif plug.mac:
            ref = PlugRef("mac", plug.mac)
        else:
            raise PlugError(
                "plug_not_found", "no cloud identity for this plug", driver="cloud"
            )
        return await self._find(ref, refresh=False)

    async def _sys_info(self, device: Any, plug: PlugIdentity) -> dict[str, Any]:
        sys_info = await self._call("get_sys_info", plug, device, device.get_sys_info)
        data = to_jsonable(sys_info)
        return data if isinstance(data, dict) else {}

    async def _call(
        self, op: str, plug: PlugIdentity, device: Any, fn: Callable[[], Awaitable[Any]]
    ) -> Any:
        try:
            async with record(op) as call:
                result = await fn()
                call.response = result
        except Exception as exc:
            raise _cloud_error(exc, op, plug=_identity(device)) from exc
        if result is None:
            raise self._no_result(op, device)
        return result

    def _no_result(self, op: str, device: Any) -> PlugError:
        identity = _identity(device)
        if not _online(device):
            return PlugError(
                "device_offline",
                f"{op} returned no result and the account list shows the plug offline",
                driver="cloud",
                plug=identity,
            )
        return PlugError(
            "cloud_error",
            f"tplink-cloud-api returned no result for {op}; the library does not "
            "expose TP-Link's error code (FINDINGS F11)",
            driver="cloud",
            plug=identity,
        )

    def _cloud_plug(self, device: Any) -> CloudPlug:
        info = device.device_info
        identity = _identity(device)
        return CloudPlug(
            plug=identity,
            supported=bool(
                identity.model and identity.model.upper().startswith(SUPPORTED_MODELS)
            ),
            online=_online(device),
            sw_version=getattr(info, "fw_ver", None),
            hw_version=getattr(info, "device_hw_ver", None),
            region=getattr(info, "device_region", None),
        )


def _run(factory: Callable[[], Awaitable[Any]]) -> Any:
    """Run a blocking-inside coroutine (F4) on a private loop in a worker thread."""
    return asyncio.run(_await(factory))


async def _await(factory: Callable[[], Awaitable[Any]]) -> Any:
    return await factory()


def _cloud_error(
    exc: BaseException, op: str, *, plug: PlugIdentity | None = None
) -> PlugError:
    code = "timeout" if _is_timeout(exc) else "cloud_error"
    detail: dict[str, Any] = {"exception": type(exc).__name__, "op": op}
    if isinstance(exc, TPLinkCloudError) and exc.error_code is not None:
        detail["error_code"] = exc.error_code
    return PlugError(
        code,
        redact_text(str(exc)) or type(exc).__name__,
        driver="cloud",
        plug=plug,
        detail=detail,
    )


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) or any(
        cls.__name__ in ("Timeout", "ServerTimeoutError") for cls in type(exc).__mro__
    )


def _match(devices: list[Any], device_id: str | None, mac: str | None) -> Any:
    for device in devices:
        if (
            device_id
            and str(getattr(device, "device_id", "")).upper() == device_id.upper()
        ):
            return device
        if (
            mac
            and normalize_mac(getattr(device.device_info, "device_mac", None)) == mac
        ):
            return device
    return None


def _identity(device: Any, sys_info: dict[str, Any] | None = None) -> PlugIdentity:
    info = device.device_info
    sys_info = sys_info or {}
    return PlugIdentity(
        mac=normalize_mac(getattr(info, "device_mac", None))
        or normalize_mac(sys_info.get("mac")),
        host=None,
        device_id=getattr(device, "device_id", None),
        alias=sys_info.get("alias") or getattr(info, "alias", None),
        model=sys_info.get("model") or getattr(info, "device_model", None),
    )


def _online(device: Any) -> bool:
    return _int(getattr(device.device_info, "status", None)) == 1


def _summary(device: Any) -> dict[str, Any]:
    info = device.device_info
    return {
        "device_id": getattr(device, "device_id", None),
        "alias": getattr(info, "alias", None),
        "model": getattr(info, "device_model", None),
        "mac": getattr(info, "device_mac", None),
        "status": getattr(info, "status", None),
    }


def _entry(device: Any, seen_at: datetime) -> CloudEntry:
    identity = _identity(device)
    return CloudEntry(
        mac=identity.mac or "",
        device_id=identity.device_id or "",
        alias=identity.alias,
        model=identity.model,
        online=_online(device),
        seen_at=seen_at,
    )


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _milli(value: Any) -> float | None:
    return None if value is None else float(value) / 1000
