"""The contract between the service and a driver (SPEC §8)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from equipment_tplink.core.identity import PlugRef
from equipment_tplink.core.models import (
    AccountEntry,
    CloudLink,
    PlugIdentity,
    ScheduleRule,
    WaitObservation,
)


@dataclass(frozen=True)
class StateReading:
    """Relay state plus whatever the read taught us about the plug."""

    identity: PlugIdentity
    is_on: bool
    on_since: datetime | None
    rssi: int | None


@dataclass(frozen=True)
class EnergyValues:
    """An instantaneous energy reading in SI units."""

    identity: PlugIdentity
    power_w: float
    voltage_v: float | None
    current_a: float | None
    energy_total_kwh: float | None


@dataclass(frozen=True)
class InfoValues:
    """Hardware, firmware and cloud-link details."""

    identity: PlugIdentity
    hw_version: str | None
    sw_version: str | None
    has_energy_monitoring: bool
    cloud_link: CloudLink | None
    account_entry: AccountEntry | None


@dataclass(frozen=True)
class ScheduleValues:
    """Schedule rules plus what the read taught us about the plug."""

    identity: PlugIdentity
    rules: list[ScheduleRule]


@dataclass(frozen=True)
class SettingsValues:
    """User-changeable settings."""

    identity: PlugIdentity
    alias: str
    led_on: bool


class PlugDriver(Protocol):
    """Primitive, single-purpose calls; the service composes them."""

    @property
    def kind(self) -> Literal["local", "cloud"]:
        """Which driver this is."""
        ...

    async def ensure_ready(self) -> None:
        """Raise if the driver cannot serve requests at all (e.g. no cloud login)."""
        ...

    async def resolve(
        self, ref: PlugRef, *, rediscover: bool = False, budget_s: float | None = None
    ) -> PlugIdentity:
        """Map a parsed reference to a known plug within ``budget_s`` seconds."""
        ...

    async def read_state(self, plug: PlugIdentity) -> StateReading:
        """Read the relay state."""
        ...

    async def probe(self, plug: PlugIdentity) -> WaitObservation:
        """Observe reachability and state for ``wait`` without raising on outages."""
        ...

    async def set_relay(self, plug: PlugIdentity, on: bool) -> None:
        """Switch the relay."""
        ...

    async def read_energy(self, plug: PlugIdentity) -> EnergyValues:
        """Read instantaneous energy values."""
        ...

    async def read_info(self, plug: PlugIdentity) -> InfoValues:
        """Read hardware, firmware and cloud-link details."""
        ...

    async def read_schedule(self, plug: PlugIdentity) -> ScheduleValues:
        """Read schedule rules."""
        ...

    async def read_settings(self, plug: PlugIdentity) -> SettingsValues:
        """Read alias and LED state."""
        ...

    async def write_settings(
        self, plug: PlugIdentity, *, alias: str | None, led_on: bool | None
    ) -> None:
        """Change alias and/or LED state."""
        ...

    async def reboot(self, plug: PlugIdentity, *, delay_s: int) -> None:
        """Reboot the plug."""
        ...

    async def factory_reset(self, plug: PlugIdentity) -> None:
        """Factory-reset the plug."""
        ...
