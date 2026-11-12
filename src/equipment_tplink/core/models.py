"""API schemas (SPEC §6.3). Response models serialize every field, nulls included."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

from equipment_tplink.kit.timestamps import UtcTimestamp
from equipment_tplink.kit.upstream import UpstreamCall

Driver = Literal["local", "cloud"]
Until = Literal["on", "off", "online", "offline"]
AuthResult = Literal["ok", "failed", "not_attempted"]


class ResponseModel(BaseModel):
    """Base for responses: every field is required in the schema (may be null)."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class RequestModel(BaseModel):
    """Base for request bodies: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class CloudState(StrEnum):
    """Cloud session state."""

    NOT_CONFIGURED = "not_configured"
    AUTHENTICATED = "authenticated"
    MFA_REQUIRED = "mfa_required"
    LOGIN_REQUIRED = "login_required"
    ERROR = "error"


class ErrorSummary(ResponseModel):
    """A short error description."""

    code: str
    message: str


class PlugIdentity(ResponseModel):
    """What is known about a physical plug."""

    mac: str | None = None
    host: str | None = None
    device_id: str | None = None
    alias: str | None = None
    model: str | None = None


class LocalConnection(ResponseModel):
    """How python-kasa reaches the plug (public ``config.connection_type``)."""

    host: str
    port: int | None = None
    encrypt_type: str | None = None
    login_version: int | None = None


class Upstream(ResponseModel):
    """The library calls behind a response."""

    driver: Driver
    calls: list[UpstreamCall]
    connection: LocalConnection | None = None


class Error(ResponseModel):
    """Every error response."""

    code: str
    message: str
    driver: Literal["local", "cloud", "none"] = "none"
    plug: PlugIdentity | None = None
    upstream: Upstream | None = None
    detail: dict[str, Any] | None = None


class PlugState(ResponseModel):
    """Relay state of one plug."""

    driver: Driver
    plug: PlugIdentity
    is_on: bool
    on_since: UtcTimestamp | None
    rssi: int | None
    observed_at: UtcTimestamp
    upstream: Upstream


class WaitObservation(ResponseModel):
    """What the last poll of a wait saw."""

    is_on: bool | None = None
    online: bool
    authenticated: bool | None = None
    listed: bool | None = None


class WaitResult(ResponseModel):
    """Outcome of a server-side wait."""

    driver: Driver
    plug: PlugIdentity
    until: Until
    satisfied: bool
    started_at: UtcTimestamp
    satisfied_at: UtcTimestamp | None
    waited_ms: int
    polls: int
    poll_errors: int
    last_observed: WaitObservation
    last_error: ErrorSummary | None
    upstream: Upstream


class RelayCommand(RequestModel):
    """Switch the relay."""

    state: Literal["on", "off", "toggle"]
    verify: bool = True
    settle_ms: int | SkipJsonSchema[None] = Field(
        default=None,
        ge=0,
        le=10_000,
        description="Wait before the verify read. Defaults to the service setting.",
    )


class RelayResult(ResponseModel):
    """Outcome of a relay command."""

    driver: Driver
    plug: PlugIdentity
    requested: Literal["on", "off", "toggle"]
    expected_on: bool
    sent_at: UtcTimestamp
    acknowledged_at: UtcTimestamp
    latency_ms: float
    is_on: bool | None
    confirmed: bool | None
    upstream: Upstream


class EnergyReading(ResponseModel):
    """One instantaneous energy reading."""

    driver: Driver
    plug: PlugIdentity
    power_w: float
    voltage_v: float | None
    current_a: float | None
    energy_total_kwh: float | None
    observed_at: UtcTimestamp
    upstream: Upstream


class CloudLink(ResponseModel):
    """The plug's own view of its cloud connection (local only)."""

    bound: bool
    connected: bool
    server: str | None
    username_masked: str | None


class AccountEntry(ResponseModel):
    """The account device list's view of the plug (cloud only)."""

    online: bool
    region: str | None
    app_server_url: str | None


class DeviceInfo(ResponseModel):
    """Hardware, firmware and cloud-link details."""

    driver: Driver
    plug: PlugIdentity
    hw_version: str | None
    sw_version: str | None
    has_energy_monitoring: bool
    cloud_link: CloudLink | None
    account_entry: AccountEntry | None
    upstream: Upstream


class RuleTime(ResponseModel):
    """Start or end of a schedule rule."""

    action: Literal["on", "off"] | None
    at: Literal["time", "sunrise", "sunset"] | None
    minutes: int | None
    offset_minutes: int | None


class ScheduleRule(ResponseModel):
    """One schedule rule stored on the plug."""

    id: str
    name: str | None
    enabled: bool
    repeat: bool
    days: list[Literal["sun", "mon", "tue", "wed", "thu", "fri", "sat"]]
    start: RuleTime
    end: RuleTime | None


class Schedule(ResponseModel):
    """The plug's schedule rules."""

    driver: Driver
    plug: PlugIdentity
    rules: list[ScheduleRule]
    upstream: Upstream


class Settings(ResponseModel):
    """User-changeable settings."""

    driver: Driver
    plug: PlugIdentity
    alias: str
    led_on: bool
    upstream: Upstream


class SettingsUpdate(RequestModel):
    """Change one or more settings."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"minProperties": 1})

    alias: str | SkipJsonSchema[None] = Field(default=None, min_length=1, max_length=64)
    led_on: bool | SkipJsonSchema[None] = None

    @model_validator(mode="after")
    def _at_least_one(self) -> SettingsUpdate:
        if self.alias is None and self.led_on is None:
            raise ValueError("set at least one of alias, led_on")
        return self


class RebootRequest(RequestModel):
    """Reboot options."""

    delay_s: int = Field(default=1, ge=1, le=60)


class FactoryResetRequest(RequestModel):
    """Factory reset confirmation."""

    confirm_mac: str = Field(
        description="Must equal the resolved plug's MAC (any accepted format)."
    )


class ActionAccepted(ResponseModel):
    """A reboot or factory reset was accepted by the plug."""

    driver: Driver
    plug: PlugIdentity
    action: Literal["reboot", "factory_reset"]
    accepted_at: UtcTimestamp
    upstream: Upstream


class DiscoveredPlug(ResponseModel):
    """A Kasa device that answered LAN discovery."""

    plug: PlugIdentity
    supported: bool
    connection: LocalConnection
    auth: AuthResult
    auth_error: str | None
    discovered_at: UtcTimestamp


class LocalPlugList(ResponseModel):
    """Result of LAN discovery."""

    target: str
    duration_ms: int
    plugs: list[DiscoveredPlug]


class CloudPlug(ResponseModel):
    """A device in the TP-Link account."""

    plug: PlugIdentity
    supported: bool
    online: bool
    sw_version: str | None
    hw_version: str | None
    region: str | None


class CloudPlugList(ResponseModel):
    """The account device list."""

    cached: bool
    fetched_at: UtcTimestamp
    plugs: list[CloudPlug]
    upstream: Upstream


class DirectoryLocal(ResponseModel):
    """Where the plug was last seen on the LAN."""

    host: str
    auth: AuthResult
    seen_at: UtcTimestamp


class DirectoryCloud(ResponseModel):
    """Where the plug was last seen in the account."""

    device_id: str
    online: bool
    seen_at: UtcTimestamp


class DirectoryEntry(ResponseModel):
    """One physical plug, merged across drivers by MAC."""

    mac: str
    alias: str | None
    model: str | None
    local: DirectoryLocal | None
    cloud: DirectoryCloud | None


class PlugDirectory(ResponseModel):
    """All known plugs."""

    plugs: list[DirectoryEntry]
    local_error: ErrorSummary | None
    cloud_error: ErrorSummary | None


class CloudSession(ResponseModel):
    """Cloud login state."""

    state: CloudState
    username_masked: str | None
    logged_in_at: UtcTimestamp | None
    last_error: ErrorSummary | None


class CloudLoginRequest(RequestModel):
    """Start a cloud login, or supply the MFA code a pending login waits for."""

    mfa_code: str | None = Field(default=None, min_length=1, max_length=16)


class LocalHealth(ResponseModel):
    """Local driver health."""

    status: Literal["ready"]
    discovery_target: str
    plugs_known: int


class CloudHealth(ResponseModel):
    """Cloud driver health."""

    status: CloudState
    last_error: ErrorSummary | None


class Health(ResponseModel):
    """Service health."""

    status: Literal["ok"]
    version: str
    local: LocalHealth
    cloud: CloudHealth


class Version(ResponseModel):
    """Service and library versions."""

    version: str
    python: str
    python_kasa: str
    tplink_cloud_api: str
