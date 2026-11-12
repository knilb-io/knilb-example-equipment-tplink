"""One service per driver: resolution, timeouts, locks, relay verify and wait.

Every public method records the library calls it makes (``upstream``) and wraps
each driver call in its own ``asyncio.timeout`` (SPEC §7.3). The recorder is
installed *before* entering the timeout scope so its contents survive a timeout.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal, TypeVar

from equipment_tplink.core.errors import PlugError
from equipment_tplink.core.identity import PlugRef, normalize_mac, parse_plug_ref
from equipment_tplink.core.models import (
    ActionAccepted,
    DeviceInfo,
    Driver,
    EnergyReading,
    ErrorSummary,
    FactoryResetRequest,
    LocalConnection,
    PlugIdentity,
    PlugState,
    RebootRequest,
    RelayCommand,
    RelayResult,
    Schedule,
    Settings,
    SettingsUpdate,
    Until,
    Upstream,
    WaitObservation,
    WaitResult,
)
from equipment_tplink.drivers.base import PlugDriver
from equipment_tplink.kit.clock import Clock
from equipment_tplink.kit.request_log import annotate
from equipment_tplink.kit.upstream import Recorder, recording

T = TypeVar("T")

OFFLINE_ERRORS = frozenset(
    {"device_unreachable", "timeout", "plug_not_found", "device_offline"}
)
REDISCOVER_EVERY_S = 5.0


@dataclass(frozen=True)
class ServiceTimings:
    """Per-driver defaults."""

    call_timeout_s: float
    poll_interval_s: float
    settle_ms: int


class PlugService:
    """Operations on plugs through one driver."""

    def __init__(
        self, driver: PlugDriver, *, clock: Clock, timings: ServiceTimings
    ) -> None:
        self.driver = driver
        self.kind: Driver = driver.kind
        self._clock = clock
        self._timings = timings
        self._locks: dict[str, asyncio.Lock] = {}

    # -- read operations -----------------------------------------------------------

    async def state(self, raw_ref: str, *, timeout_s: float | None) -> PlugState:
        """Read the relay state."""
        with self._operation() as recorder:
            plug = await self._resolve(raw_ref, timeout_s)
            reading = await self._call(
                plug, timeout_s, lambda: self.driver.read_state(plug)
            )
            return PlugState(
                driver=self.kind,
                plug=reading.identity,
                is_on=reading.is_on,
                on_since=reading.on_since,
                rssi=reading.rssi,
                observed_at=self._clock.now(),
                upstream=self._upstream(recorder),
            )

    async def energy(self, raw_ref: str, *, timeout_s: float | None) -> EnergyReading:
        """Read one instantaneous energy value."""
        with self._operation() as recorder:
            plug = await self._resolve(raw_ref, timeout_s)
            values = await self._call(
                plug, timeout_s, lambda: self.driver.read_energy(plug)
            )
            return EnergyReading(
                driver=self.kind,
                plug=values.identity,
                power_w=values.power_w,
                voltage_v=values.voltage_v,
                current_a=values.current_a,
                energy_total_kwh=values.energy_total_kwh,
                observed_at=self._clock.now(),
                upstream=self._upstream(recorder),
            )

    async def info(self, raw_ref: str, *, timeout_s: float | None) -> DeviceInfo:
        """Read hardware, firmware and cloud-link details."""
        with self._operation() as recorder:
            plug = await self._resolve(raw_ref, timeout_s)
            values = await self._call(
                plug, timeout_s, lambda: self.driver.read_info(plug)
            )
            return DeviceInfo(
                driver=self.kind,
                plug=values.identity,
                hw_version=values.hw_version,
                sw_version=values.sw_version,
                has_energy_monitoring=values.has_energy_monitoring,
                cloud_link=values.cloud_link,
                account_entry=values.account_entry,
                upstream=self._upstream(recorder),
            )

    async def schedule(self, raw_ref: str, *, timeout_s: float | None) -> Schedule:
        """Read schedule rules."""
        with self._operation() as recorder:
            plug = await self._resolve(raw_ref, timeout_s)
            values = await self._call(
                plug, timeout_s, lambda: self.driver.read_schedule(plug)
            )
            return Schedule(
                driver=self.kind,
                plug=values.identity,
                rules=values.rules,
                upstream=self._upstream(recorder),
            )

    async def settings(self, raw_ref: str, *, timeout_s: float | None) -> Settings:
        """Read alias and LED state."""
        with self._operation() as recorder:
            plug = await self._resolve(raw_ref, timeout_s)
            values = await self._call(
                plug, timeout_s, lambda: self.driver.read_settings(plug)
            )
            return Settings(
                driver=self.kind,
                plug=values.identity,
                alias=values.alias,
                led_on=values.led_on,
                upstream=self._upstream(recorder),
            )

    # -- commands ------------------------------------------------------------------

    async def update_settings(
        self, raw_ref: str, update: SettingsUpdate, *, timeout_s: float | None
    ) -> Settings:
        """Change settings, then read them back."""
        with self._operation() as recorder:
            plug = await self._resolve(raw_ref, timeout_s)
            await self._call(
                plug,
                timeout_s,
                lambda: self.driver.write_settings(
                    plug, alias=update.alias, led_on=update.led_on
                ),
            )
            values = await self._call(
                plug, timeout_s, lambda: self.driver.read_settings(plug)
            )
            return Settings(
                driver=self.kind,
                plug=values.identity,
                alias=values.alias,
                led_on=values.led_on,
                upstream=self._upstream(recorder),
            )

    async def relay(
        self, raw_ref: str, command: RelayCommand, *, timeout_s: float | None
    ) -> RelayResult:
        """Switch the relay and (by default) verify it (SPEC §7.1)."""
        with self._operation() as recorder:
            plug = await self._resolve(raw_ref, timeout_s)
            if command.state == "toggle":
                before = await self._call(
                    plug, timeout_s, lambda: self.driver.read_state(plug)
                )
                expected = not before.is_on
            else:
                expected = command.state == "on"
            sent_at = self._clock.now()
            sent_mono = self._clock.monotonic()
            await self._call(
                plug, timeout_s, lambda: self.driver.set_relay(plug, expected)
            )
            acknowledged_at = self._clock.now()
            latency_ms = round((self._clock.monotonic() - sent_mono) * 1000, 3)
            is_on: bool | None = None
            confirmed: bool | None = None
            if command.verify:
                settle_ms = (
                    command.settle_ms
                    if command.settle_ms is not None
                    else self._timings.settle_ms
                )
                await self._clock.sleep(settle_ms / 1000)
                after = await self._call(
                    plug, timeout_s, lambda: self.driver.read_state(plug)
                )
                plug = after.identity
                is_on = after.is_on
                confirmed = is_on == expected
            return RelayResult(
                driver=self.kind,
                plug=plug,
                requested=command.state,
                expected_on=expected,
                sent_at=sent_at,
                acknowledged_at=acknowledged_at,
                latency_ms=latency_ms,
                is_on=is_on,
                confirmed=confirmed,
                upstream=self._upstream(recorder),
            )

    async def reboot(
        self, raw_ref: str, request: RebootRequest, *, timeout_s: float | None
    ) -> ActionAccepted:
        """Reboot; recovery is observed with ``wait``."""
        with self._operation() as recorder:
            plug = await self._resolve(raw_ref, timeout_s)
            await self._call(
                plug,
                timeout_s,
                lambda: self.driver.reboot(plug, delay_s=request.delay_s),
            )
            return ActionAccepted(
                driver=self.kind,
                plug=plug,
                action="reboot",
                accepted_at=self._clock.now(),
                upstream=self._upstream(recorder),
            )

    async def factory_reset(
        self, raw_ref: str, request: FactoryResetRequest, *, timeout_s: float | None
    ) -> ActionAccepted:
        """Factory-reset, guarded by ``confirm_mac``."""
        with self._operation() as recorder:
            plug = await self._resolve(raw_ref, timeout_s)
            if plug.mac is None:
                plug = (
                    await self._call(
                        plug, timeout_s, lambda: self.driver.read_state(plug)
                    )
                ).identity
            confirm = normalize_mac(request.confirm_mac)
            if confirm is None or confirm != plug.mac:
                raise PlugError(
                    "confirmation_mismatch",
                    f"confirm_mac {request.confirm_mac!r} does not match the plug's "
                    f"MAC {plug.mac}",
                    driver=self.kind,
                    plug=plug,
                    upstream=self._upstream(recorder),
                )
            await self._call(plug, timeout_s, lambda: self.driver.factory_reset(plug))
            return ActionAccepted(
                driver=self.kind,
                plug=plug,
                action="factory_reset",
                accepted_at=self._clock.now(),
                upstream=self._upstream(recorder),
            )

    # -- wait ----------------------------------------------------------------------

    async def wait(self, raw_ref: str, until: Until, *, window_s: float) -> WaitResult:
        """Poll until the condition holds or the window closes (SPEC §7.2)."""
        ref = parse_plug_ref(raw_ref, namespace=self.kind)
        await self.driver.ensure_ready()
        annotate(driver=self.kind)
        clock = self._clock
        started_at = clock.now()
        start = clock.monotonic()
        deadline = start + window_s
        plug = PlugIdentity(
            mac=ref.value if ref.kind == "mac" else None,
            host=ref.value if ref.kind in ("ipv4", "hostname") else None,
            device_id=ref.value if ref.kind == "device_id" else None,
        )
        polls = errors = 0
        last_rediscover = start
        satisfied_at = None
        observation = WaitObservation(online=False)
        last_error: ErrorSummary | None = None
        recorder = Recorder(clock)
        while True:
            poll_start = clock.monotonic()
            poll_started_at = clock.now()
            timeout = max(
                min(self._timings.call_timeout_s, deadline - poll_start), 0.05
            )
            rediscover = (
                ref.kind == "mac"
                and polls > 0
                and not observation.online
                and poll_start - last_rediscover >= REDISCOVER_EVERY_S
            )
            if rediscover:
                last_rediscover = poll_start
            recorder = Recorder(clock)
            with recording(recorder):
                try:
                    plug, observation = await self._observe(
                        ref, plug, until, timeout, rediscover
                    )
                    last_error = None
                except PlugError as error:
                    errors += 1
                    last_error = ErrorSummary(code=error.code, message=error.message)
                    observation = _observation_from_error(error, self.kind)
            polls += 1
            if _satisfied(until, observation):
                satisfied_at = poll_started_at
                break
            now = clock.monotonic()
            if now >= deadline:
                break
            pause = min(
                self._timings.poll_interval_s - (now - poll_start), deadline - now
            )
            await clock.sleep(max(pause, 0.0))
            if clock.monotonic() >= deadline:
                break
        annotate(plug=plug.mac, upstream_ms=recorder.total_ms())
        return WaitResult(
            driver=self.kind,
            plug=plug,
            until=until,
            satisfied=satisfied_at is not None,
            started_at=started_at,
            satisfied_at=satisfied_at,
            waited_ms=round((clock.monotonic() - start) * 1000),
            polls=polls,
            poll_errors=errors,
            last_observed=observation,
            last_error=last_error,
            upstream=self._upstream(recorder),
        )

    async def _observe(
        self,
        ref: PlugRef,
        plug: PlugIdentity,
        until: Until,
        timeout: float,
        rediscover: bool,
    ) -> tuple[PlugIdentity, WaitObservation]:
        if (
            rediscover
            or (plug.host is None and plug.device_id is None)
            or (self.kind == "cloud" and plug.device_id is None)
        ):
            try:
                plug = await self.bounded(
                    timeout,
                    lambda: self.driver.resolve(
                        ref, rediscover=rediscover, budget_s=timeout
                    ),
                )
            except PlugError as error:
                if error.code == "plug_not_found":
                    listed = False if self.kind == "cloud" else None
                    return plug, WaitObservation(online=False, listed=listed)
                raise
        if until in ("online", "offline"):
            observation = await self._call(
                plug, timeout, lambda: self.driver.probe(plug)
            )
            return plug, observation
        reading = await self._call(plug, timeout, lambda: self.driver.read_state(plug))
        return reading.identity, WaitObservation(
            is_on=reading.is_on,
            online=True,
            authenticated=True if self.kind == "local" else None,
            listed=True if self.kind == "cloud" else None,
        )

    # -- helpers -------------------------------------------------------------------

    @contextmanager
    def _operation(self) -> Iterator[Recorder]:
        recorder = Recorder(self._clock)
        annotate(driver=self.kind)
        with recording(recorder):
            try:
                yield recorder
            except PlugError as error:
                error.with_context(upstream=self._upstream(recorder))
                raise
            finally:
                annotate(upstream_ms=recorder.total_ms())

    async def _resolve(self, raw_ref: str, timeout_s: float | None) -> PlugIdentity:
        ref = parse_plug_ref(raw_ref, namespace=self.kind)
        await self.driver.ensure_ready()
        budget = self._timeout(timeout_s)
        plug = await self.bounded(
            budget, lambda: self.driver.resolve(ref, budget_s=budget)
        )
        annotate(plug=plug.mac or plug.host or plug.device_id)
        return plug

    async def _call(
        self,
        plug: PlugIdentity,
        timeout_s: float | None,
        fn: Callable[[], Awaitable[T]],
    ) -> T:
        # Resolved local plugs always have a host and cloud plugs a device id, so
        # the key is the same however the caller addressed the plug.
        key = plug.host or plug.device_id or plug.mac or "?"
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                return await self.bounded(self._timeout(timeout_s), fn)
            except PlugError as error:
                raise error.with_context(plug=plug) from None

    @property
    def call_timeout_s(self) -> float:
        """The default per-call timeout of this driver."""
        return self._timings.call_timeout_s

    async def bounded(self, timeout_s: float, fn: Callable[[], Awaitable[T]]) -> T:
        """Run ``fn`` under a timeout, mapping expiry to ``504 timeout``."""
        try:
            async with asyncio.timeout(timeout_s):
                return await fn()
        except TimeoutError as exc:
            raise PlugError(
                "timeout",
                f"the {self.kind} driver did not answer within {timeout_s:g} s",
                driver=self.kind,
            ) from exc

    def _timeout(self, timeout_s: float | None) -> float:
        return timeout_s if timeout_s is not None else self._timings.call_timeout_s

    def _upstream(self, recorder: Recorder) -> Upstream:
        connection = recorder.context.get("connection")
        return Upstream(
            driver=self.kind,
            calls=list(recorder.calls),
            connection=connection if isinstance(connection, LocalConnection) else None,
        )


def _satisfied(until: Until, observation: WaitObservation) -> bool:
    if until == "on":
        return observation.is_on is True
    if until == "off":
        return observation.is_on is False
    if until == "online":
        return observation.online
    return not observation.online


def _observation_from_error(
    error: PlugError, kind: Literal["local", "cloud"]
) -> WaitObservation:
    if error.code == "device_auth_failed":
        return WaitObservation(online=True, authenticated=False)
    listed: bool | None = None
    if kind == "cloud":
        listed = error.code != "plug_not_found"
    return WaitObservation(online=error.code not in OFFLINE_ERRORS, listed=listed)
