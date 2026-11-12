"""Local driver: python-kasa, public API only (SPEC §7.5).

The library is used as-is: its transport selection (FINDINGS F1), its internal
retries (F18) and its error wrapping (F17) are left alone and surfaced.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from kasa import (
    AuthenticationError,
    Credentials,
    Discover,
    Module,
    UnsupportedDeviceError,
)

from equipment_tplink.core.directory import Directory
from equipment_tplink.core.errors import PlugError
from equipment_tplink.core.identity import PlugRef, normalize_mac
from equipment_tplink.core.models import (
    AuthResult,
    CloudLink,
    DiscoveredPlug,
    LocalConnection,
    LocalPlugList,
    PlugIdentity,
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
from equipment_tplink.kit.clock import Clock
from equipment_tplink.kit.redact import mask_email, redact_text
from equipment_tplink.kit.upstream import current_recorder, record

SUPPORTED_MODELS = ("KP115", "HS110", "KP125")
OFFLINE_CODES = frozenset({"device_unreachable", "timeout", "plug_not_found"})

DiscoverFn = Callable[..., Awaitable[Mapping[str, Any]]]
DiscoverSingleFn = Callable[..., Awaitable[Any]]


@dataclass
class _Cached:
    device: Any
    last_used: float


class LocalKasaDriver:
    """Talks to plugs on the LAN through python-kasa."""

    kind: Literal["local"] = "local"

    def __init__(
        self,
        *,
        directory: Directory,
        clock: Clock,
        credentials: Credentials | None,
        discovery_target: str,
        discovery_timeout_s: float,
        call_timeout_s: float,
        discover: DiscoverFn | None = None,
        discover_single: DiscoverSingleFn | None = None,
        idle_s: float = 300.0,
        probe_concurrency: int = 4,
    ) -> None:
        self._directory = directory
        self._clock = clock
        self._credentials = credentials
        self.discovery_target = discovery_target
        self.discovery_timeout_s = discovery_timeout_s
        self._call_timeout_s = call_timeout_s
        self._discover: DiscoverFn = discover or Discover.discover
        self._discover_single: DiscoverSingleFn = (
            discover_single or Discover.discover_single
        )
        self._idle_s = idle_s
        self._probe_concurrency = probe_concurrency
        self._devices: dict[str, _Cached] = {}
        self._background: set[asyncio.Task[None]] = set()

    # -- lifecycle -----------------------------------------------------------------

    async def ensure_ready(self) -> None:
        """The local driver is always ready."""

    async def close(self) -> None:
        """Disconnect every cached device."""
        for key in list(self._devices):
            await self._drop_key(key)

    # -- discovery and resolution --------------------------------------------------

    async def discover_plugs(
        self, *, timeout_s: float, target: str | None = None, probe_auth: bool = True
    ) -> LocalPlugList:
        """Broadcast discovery, then (optionally) probe each device's auth."""
        target = target or self.discovery_target
        start = self._clock.monotonic()
        unsupported: list[DiscoveredPlug] = []

        async def on_unsupported(error: UnsupportedDeviceError) -> None:
            unsupported.append(self._unsupported_entry(error))

        try:
            async with record("discover") as call:
                found = await self._discover(
                    target=target,
                    discovery_timeout=max(1, round(timeout_s)),
                    credentials=self._credentials,
                    timeout=self._library_timeout(),
                    on_unsupported=on_unsupported,
                )
                call.response = {"hosts": sorted(found)}
        except Exception as exc:
            raise classify_kasa_error(exc, plug=None) from exc

        semaphore = asyncio.Semaphore(self._probe_concurrency)

        async def describe(device: Any) -> DiscoveredPlug:
            identity = self._identity(device, PlugIdentity(), updated=False)
            auth: AuthResult = "not_attempted"
            auth_error = None
            if probe_auth:
                async with semaphore:
                    auth, auth_error, identity = await self._probe_auth(
                        device, identity
                    )
            self._directory.upsert_local(identity, auth=auth if probe_auth else None)
            self._remember(identity, device)
            return DiscoveredPlug(
                plug=identity,
                supported=_supported(identity.model),
                connection=_connection(device),
                auth=auth,
                auth_error=auth_error,
                discovered_at=self._clock.now(),
            )

        plugs = await asyncio.gather(*(describe(device) for device in found.values()))
        entries = sorted([*plugs, *unsupported], key=lambda p: p.plug.mac or "")
        return LocalPlugList(
            target=target,
            duration_ms=round((self._clock.monotonic() - start) * 1000),
            plugs=entries,
        )

    async def resolve(
        self, ref: PlugRef, *, rediscover: bool = False, budget_s: float | None = None
    ) -> PlugIdentity:
        """Map a MAC (via the directory and discovery) or a host to a plug.

        A discovery run for an unknown MAC is shortened to fit ``budget_s``.
        """
        if ref.kind != "mac":
            mac = self._directory.mac_for_host(ref.value)
            entry = self._directory.local_by_mac(mac) if mac else None
            return PlugIdentity(
                mac=mac,
                host=ref.value,
                device_id=entry.device_id if entry else None,
                alias=entry.alias if entry else None,
                model=entry.model if entry else None,
            )
        entry = None if rediscover else self._directory.local_by_mac(ref.value)
        if entry is None:
            duration = self.discovery_timeout_s
            if budget_s is not None:
                duration = max(1.0, min(duration, budget_s - 1.0))
            await self.discover_plugs(timeout_s=duration, probe_auth=False)
            entry = self._directory.local_by_mac(ref.value)
        if entry is None:
            raise PlugError(
                "plug_not_found",
                f"no plug with MAC {ref.value} answered LAN discovery",
                driver="local",
                plug=PlugIdentity(mac=ref.value),
            )
        return PlugIdentity(
            mac=entry.mac,
            host=entry.host,
            device_id=entry.device_id,
            alias=entry.alias,
            model=entry.model,
        )

    # -- operations ----------------------------------------------------------------

    async def read_state(self, plug: PlugIdentity) -> StateReading:
        """``update()`` then the public state properties."""
        device, identity = await self._updated(plug)
        return StateReading(
            identity=identity,
            is_on=bool(device.is_on),
            on_since=_utc(device.on_since),
            rssi=device.rssi,
        )

    async def probe(self, plug: PlugIdentity) -> WaitObservation:
        """Online means the plug answered, even if it rejected the credentials."""
        try:
            reading = await self.read_state(plug)
        except PlugError as error:
            if error.code == "device_auth_failed":
                if plug.mac:
                    self._directory.set_local_auth(plug.mac, "failed")
                return WaitObservation(online=True, authenticated=False)
            if error.code in OFFLINE_CODES:
                return WaitObservation(online=False)
            raise
        return WaitObservation(is_on=reading.is_on, online=True, authenticated=True)

    async def set_relay(self, plug: PlugIdentity, on: bool) -> None:
        """``turn_on`` / ``turn_off``."""
        device = await self._device_for(plug)
        if on:
            await self._call("turn_on", plug, device.turn_on)
        else:
            await self._call("turn_off", plug, device.turn_off)

    async def read_energy(self, plug: PlugIdentity) -> EnergyValues:
        """The ``Energy`` module after ``update()``."""
        device, identity = await self._updated(plug)
        energy = device.modules.get(Module.Energy)
        if energy is None:
            raise PlugError(
                "unsupported",
                "this plug has no energy monitoring",
                driver="local",
                plug=identity,
            )
        if energy.current_consumption is None:
            raise PlugError(
                "device_error",
                "python-kasa reported no power reading",
                driver="local",
                plug=identity,
            )
        return EnergyValues(
            identity=identity,
            power_w=float(energy.current_consumption),
            voltage_v=_float(energy.voltage),
            current_a=_float(energy.current),
            energy_total_kwh=_float(energy.consumption_total),
        )

    async def read_info(self, plug: PlugIdentity) -> InfoValues:
        """``hw_info`` and the ``IotCloud`` module after ``update()``."""
        device, identity = await self._updated(plug)
        hw_info = device.hw_info
        cloud = device.modules.get(Module.IotCloud)
        link = None
        if cloud is not None:
            info = cloud.info
            link = CloudLink(
                bound=bool(info.provisioned),
                connected=bool(info.cloud_connected),
                server=info.server or None,
                username_masked=mask_email(info.username or None),
            )
        return InfoValues(
            identity=identity,
            hw_version=hw_info.get("hw_ver"),
            sw_version=hw_info.get("sw_ver"),
            has_energy_monitoring=Module.Energy in device.modules,
            cloud_link=link,
            account_entry=None,
        )

    async def read_schedule(self, plug: PlugIdentity) -> ScheduleValues:
        """The ``IotSchedule`` module's rules after ``update()``."""
        device, identity = await self._updated(plug)
        schedule = device.modules.get(Module.IotSchedule)
        if schedule is None:
            raise PlugError(
                "unsupported",
                "this plug has no schedule",
                driver="local",
                plug=identity,
            )
        rules = [parse_rule(rule.to_dict()) for rule in schedule.rules]
        return ScheduleValues(identity=identity, rules=rules)

    async def read_settings(self, plug: PlugIdentity) -> SettingsValues:
        """Alias and the ``Led`` module after ``update()``."""
        device, identity = await self._updated(plug)
        led = device.modules.get(Module.Led)
        led_on = (
            bool(led.led) if led is not None else not device.sys_info.get("led_off")
        )
        return SettingsValues(
            identity=identity, alias=device.alias or "", led_on=bool(led_on)
        )

    async def write_settings(
        self, plug: PlugIdentity, *, alias: str | None, led_on: bool | None
    ) -> None:
        """``set_alias`` and ``Led.set_led``."""
        device = await self._device_for(plug)
        if alias is not None:
            await self._call("set_alias", plug, lambda: device.set_alias(alias))
        if led_on is not None:
            device, identity = await self._updated(plug)
            led = device.modules.get(Module.Led)
            if led is None:
                raise PlugError(
                    "unsupported",
                    "this plug has no LED control",
                    driver="local",
                    plug=identity,
                )
            await self._call("set_led", plug, lambda: led.set_led(led_on))

    async def reboot(self, plug: PlugIdentity, *, delay_s: int) -> None:
        """``reboot(delay)``; the cached connection is dropped afterwards."""
        device = await self._device_for(plug)
        await self._call("reboot", plug, lambda: device.reboot(delay_s))
        await self._drop(plug)

    async def factory_reset(self, plug: PlugIdentity) -> None:
        """``factory_reset()``; the plug will come back only after re-provisioning."""
        device = await self._device_for(plug)
        await self._call("factory_reset", plug, device.factory_reset)
        await self._drop(plug)
        if plug.mac:
            self._directory.forget_local(plug.mac)

    # -- helpers -------------------------------------------------------------------

    async def _updated(self, plug: PlugIdentity) -> tuple[Any, PlugIdentity]:
        device = await self._device_for(plug)
        await self._call(
            "update", plug, device.update, response=lambda _: device.internal_state
        )
        identity = self._identity(device, plug, updated=True)
        self._directory.upsert_local(identity, auth="ok")
        return device, identity

    async def _device_for(self, plug: PlugIdentity) -> Any:
        self._evict_idle()
        key = plug.mac or plug.host
        cached = self._devices.get(key) if key else None
        if cached is not None and (
            plug.host is None or cached.device.host == plug.host
        ):
            cached.last_used = self._clock.monotonic()
            self._note_connection(cached.device)
            return cached.device
        if plug.host is None:
            raise PlugError(
                "plug_not_found",
                "no host known for this plug",
                driver="local",
                plug=plug,
            )
        host = plug.host
        device = await self._call(
            "discover_single",
            plug,
            lambda: self._discover_single(
                host,
                discovery_timeout=max(1, round(self.discovery_timeout_s)),
                credentials=self._credentials,
                timeout=self._library_timeout(),
            ),
            response=lambda found: found.internal_state,
            timeout_code="device_unreachable",
        )
        identity = self._identity(device, plug, updated=False)
        self._directory.upsert_local(identity)
        self._remember(identity, device)
        self._note_connection(device)
        return device

    async def _call(
        self,
        op: str,
        plug: PlugIdentity,
        fn: Callable[[], Awaitable[Any]],
        *,
        response: Callable[[Any], Any] | None = None,
        timeout_code: str = "timeout",
    ) -> Any:
        try:
            async with record(op) as call:
                result = await fn()
                call.response = response(result) if response else result
        except PlugError:
            raise
        except Exception as exc:
            error = classify_kasa_error(exc, plug=plug)
            if error.code == "timeout":
                error = PlugError(
                    timeout_code,
                    error.message,
                    driver="local",
                    plug=plug,
                    detail=error.fields["detail"],
                )
            if error.code in OFFLINE_CODES:
                await self._drop(plug)
            raise error from exc
        return result

    async def _probe_auth(
        self, device: Any, identity: PlugIdentity
    ) -> tuple[AuthResult, str | None, PlugIdentity]:
        try:
            async with asyncio.timeout(self._call_timeout_s):
                await device.update()
        except Exception as exc:
            error = classify_kasa_error(exc, plug=identity)
            if error.code == "device_auth_failed":
                return "failed", error.message, identity
            return "not_attempted", f"{error.code}: {error.message}", identity
        return "ok", None, self._identity(device, identity, updated=True)

    def _identity(
        self, device: Any, base: PlugIdentity, *, updated: bool
    ) -> PlugIdentity:
        if updated:
            sys_info = device.sys_info
            return PlugIdentity(
                mac=normalize_mac(device.mac) or base.mac,
                host=device.host or base.host,
                device_id=sys_info.get("deviceId") or base.device_id,
                alias=device.alias or base.alias,
                model=sys_info.get("model") or base.model,
            )
        state = device.internal_state or {}
        return PlugIdentity(
            mac=normalize_mac(device.mac) or base.mac,
            host=device.host or base.host,
            device_id=base.device_id,
            alias=base.alias,
            model=state.get("device_model") or base.model,
        )

    def _remember(self, identity: PlugIdentity, device: Any) -> None:
        key = identity.mac or identity.host
        if key is None:
            return
        previous = self._devices.get(key)
        if previous is not None and previous.device is not device:
            if previous.device.host == device.host:
                # Same plug, same address: keep the established session.
                previous.last_used = self._clock.monotonic()
                self._disconnect_later(device)
                return
            self._disconnect_later(previous.device)
        self._devices[key] = _Cached(device, self._clock.monotonic())

    async def _drop(self, plug: PlugIdentity) -> None:
        for key in {plug.mac, plug.host}:
            if key:
                await self._drop_key(key)

    async def _drop_key(self, key: str) -> None:
        cached = self._devices.pop(key, None)
        if cached is not None:
            await _disconnect(cached.device)

    def _evict_idle(self) -> None:
        now = self._clock.monotonic()
        for key, cached in list(self._devices.items()):
            if now - cached.last_used > self._idle_s:
                del self._devices[key]
                self._disconnect_later(cached.device)

    def _disconnect_later(self, device: Any) -> None:
        task = asyncio.get_running_loop().create_task(_disconnect(device))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _note_connection(self, device: Any) -> None:
        recorder = current_recorder()
        if recorder is not None:
            recorder.context["connection"] = _connection(device)

    def _library_timeout(self) -> int:
        return max(1, round(self._call_timeout_s))

    def _unsupported_entry(self, error: UnsupportedDeviceError) -> DiscoveredPlug:
        result = getattr(error, "discovery_result", None) or {}
        host = getattr(error, "host", None) or result.get("ip") or ""
        return DiscoveredPlug(
            plug=PlugIdentity(
                mac=normalize_mac(result.get("mac")),
                host=host or None,
                model=result.get("device_model") or result.get("model"),
            ),
            supported=False,
            connection=LocalConnection(host=host),
            auth="not_attempted",
            auth_error=redact_text(str(error)),
            discovered_at=self._clock.now(),
        )


def classify_kasa_error(exc: BaseException, *, plug: PlugIdentity | None) -> PlugError:
    """Map a python-kasa exception (walking its cause chain) to a service error.

    python-kasa wraps command failures as ``KasaException("Communication error…")``
    (FINDINGS F17), so the useful type is often further down the chain.
    """
    chain = list(_chain(exc))

    def has(*types: type[BaseException]) -> bool:
        return any(isinstance(item, types) for item in chain)

    def named(*names: str) -> bool:
        return any(
            cls.__name__ in names for item in chain for cls in type(item).__mro__
        )

    if has(AuthenticationError):
        code = "device_auth_failed"
    elif has(UnsupportedDeviceError):
        code = "unsupported"
    elif has(TimeoutError):
        code = "timeout"
    elif has(OSError) or named("_ConnectionError", "ClientConnectionError"):
        code = "device_unreachable"
    else:
        code = "device_error"
    return PlugError(
        code,
        redact_text(str(exc)) or type(exc).__name__,
        driver="local",
        plug=plug,
        detail={
            "exception": type(exc).__name__,
            "chain": [type(e).__name__ for e in chain],
        },
    )


def _chain(exc: BaseException | None) -> Iterator[BaseException]:
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _connection(device: Any) -> LocalConnection:
    connection_type = getattr(getattr(device, "config", None), "connection_type", None)
    encryption = getattr(connection_type, "encryption_type", None)
    return LocalConnection(
        host=device.host,
        port=getattr(device, "port", None),
        encrypt_type=getattr(encryption, "value", None),
        login_version=getattr(connection_type, "login_version", None),
    )


def _supported(model: str | None) -> bool:
    return bool(model) and str(model).upper().startswith(SUPPORTED_MODELS)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


async def _disconnect(device: Any) -> None:
    try:
        await device.disconnect()
    except Exception:  # best effort; the device may already be gone
        return
