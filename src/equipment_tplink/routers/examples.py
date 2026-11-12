"""Response examples for the OpenAPI document, keyed by ``(operationId, status)``.

Every documented response carries at least one example (SPEC §9); the contract
tests validate each example against its schema.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from equipment_tplink.core.models import (
    AccountEntry,
    ActionAccepted,
    CloudHealth,
    CloudLink,
    CloudPlug,
    CloudPlugList,
    CloudSession,
    CloudState,
    DeviceInfo,
    DirectoryCloud,
    DirectoryEntry,
    DirectoryLocal,
    DiscoveredPlug,
    Driver,
    EnergyReading,
    Error,
    Health,
    LocalConnection,
    LocalHealth,
    LocalPlugList,
    PlugDirectory,
    PlugIdentity,
    PlugState,
    RelayResult,
    RuleTime,
    Schedule,
    ScheduleRule,
    Settings,
    Upstream,
    Version,
    WaitObservation,
    WaitResult,
)
from equipment_tplink.kit.upstream import UpstreamCall

T0 = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
MAC = "00:00:5E:00:53:01"
HOST = "192.0.2.10"
DEVICE_ID = "8006F1A2B3C4D5E6F708192A3B4C5D6E7F801234"
SYSINFO: dict[str, Any] = {
    "alias": "Bench lamp",
    "model": "KP115(US)",
    "mac": MAC,
    "deviceId": DEVICE_ID,
    "sw_ver": "1.0.21 Build 231129 Rel.171238",
    "hw_ver": "1.0",
    "relay_state": 1,
    "on_time": 125,
    "rssi": -52,
    "led_off": 0,
}

ERROR_MESSAGES: dict[str, str] = {
    "invalid_plug_ref": (
        "'lamp!' is not a MAC, IPv4 address, hostname or cloud deviceId"
    ),
    "plug_not_found": f"no plug with MAC {MAC} answered LAN discovery",
    "confirmation_mismatch": (
        "confirm_mac '00:00:5E:00:53:02' does not match the plug's MAC " + MAC
    ),
    "mfa_not_pending": (
        "no login is waiting for an MFA code; POST without mfa_code first"
    ),
    "validation_error": "request validation failed",
    "internal_error": "internal error",
    "unsupported": "this plug has no energy monitoring",
    "device_unreachable": f"Unable to connect to the device: {HOST}:80",
    "device_auth_failed": (
        f"Device response did not match our challenge on ip {HOST}, check that your "
        "e-mail and password (both case-sensitive) are correct."
    ),
    "device_error": "Communication error on system:set_relay_state",
    "device_offline": (
        "get_sys_info returned no result and the account list shows the plug offline"
    ),
    "cloud_error": (
        "tplink-cloud-api returned no result for get_sys_info; the library does not "
        "expose TP-Link's error code (FINDINGS F11)"
    ),
    "cloud_not_configured": "TPLINK_USERNAME/TPLINK_PASSWORD are not set",
    "cloud_login_required": "cloud login required: Incorrect email or password",
    "cloud_mfa_required": (
        "the cloud login is waiting for an MFA code; POST /v1/cloud/session with "
        '{"mfa_code": "…"}'
    ),
    "timeout": "the local driver did not answer within 10 s",
}

_CODES_BY_STATUS: dict[int, tuple[str, ...]] = {
    400: ("invalid_plug_ref",),
    404: ("plug_not_found",),
    422: ("validation_error",),
    500: ("internal_error",),
    501: ("unsupported",),
    503: ("cloud_not_configured", "cloud_login_required", "cloud_mfa_required"),
    504: ("timeout",),
}
_409: dict[str, tuple[str, ...]] = {
    "factoryResetLocalPlug": ("confirmation_mismatch",),
    "loginCloud": ("mfa_not_pending",),
}
_502: dict[str, tuple[str, ...]] = {
    "local": ("device_unreachable", "device_auth_failed", "device_error"),
    "cloud": ("device_offline", "cloud_error"),
}


def build_registry(
    document: Mapping[str, Any],
) -> dict[tuple[str, int], dict[str, Any]]:
    """Examples for every response of every operation in ``document``."""
    success = success_examples()
    registry: dict[tuple[str, int], dict[str, Any]] = {}
    for item in document.get("paths", {}).values():
        for operation in item.values():
            operation_id = operation["operationId"]
            kind = _driver_of(operation.get("tags", []))
            for status_text in operation.get("responses", {}):
                status = int(status_text)
                if status < 400:
                    example = success.get(operation_id)
                    if example is not None:
                        registry[(operation_id, status)] = {
                            "example": example.model_dump(mode="json")
                        }
                    continue
                registry[(operation_id, status)] = {
                    "examples": {
                        code: {"summary": code, "value": error_example(code, kind)}
                        for code in _codes(operation_id, status, kind)
                    }
                }
    return registry


def error_example(code: str, kind: Driver | None) -> dict[str, Any]:
    """A representative error body for ``code``."""
    generic = code in ("validation_error", "internal_error")
    driver = "none" if generic or kind is None else kind
    upstream = None
    detail: dict[str, Any] | None = None
    plug = None if generic or kind is None else _identity(kind)
    if code == "device_auth_failed":
        upstream = Upstream(
            driver="local",
            calls=[
                _call(
                    "update",
                    None,
                    outcome="error",
                    error_type="AuthenticationError",
                    error_message=ERROR_MESSAGES[code],
                )
            ],
            connection=_connection(),
        )
        detail = {"exception": "AuthenticationError", "chain": ["AuthenticationError"]}
    elif code == "validation_error":
        detail = {
            "errors": [
                {
                    "loc": ["body", "state"],
                    "msg": "Input should be 'on', 'off' or 'toggle'",
                    "type": "literal_error",
                }
            ]
        }
    elif code == "internal_error":
        detail = {"type": "RuntimeError"}
    return Error(
        code=code,
        message=ERROR_MESSAGES[code],
        driver=driver,
        plug=plug,
        upstream=upstream,
        detail=detail,
    ).model_dump(mode="json")


def success_examples() -> dict[str, BaseModel]:
    """One success example per operationId."""
    examples: dict[str, BaseModel] = {}
    for kind in ("local", "cloud"):
        examples.update(_plug_examples(kind))
    local, cloud = _identity("local"), _identity("cloud")
    examples["rebootLocalPlug"] = ActionAccepted(
        driver="local",
        plug=local,
        action="reboot",
        accepted_at=T0,
        upstream=_upstream("local", _call("reboot", None)),
    )
    examples["factoryResetLocalPlug"] = ActionAccepted(
        driver="local",
        plug=local,
        action="factory_reset",
        accepted_at=T0,
        upstream=_upstream("local", _call("factory_reset", None)),
    )
    examples["discoverLocalPlugs"] = LocalPlugList(
        target="255.255.255.255",
        duration_ms=5021,
        plugs=[
            DiscoveredPlug(
                plug=local,
                supported=True,
                connection=_connection(),
                auth="failed",
                auth_error=ERROR_MESSAGES["device_auth_failed"],
                discovered_at=T0,
            )
        ],
    )
    examples["listCloudPlugs"] = CloudPlugList(
        cached=False,
        fetched_at=T0,
        plugs=[
            CloudPlug(
                plug=cloud,
                supported=True,
                online=True,
                sw_version=SYSINFO["sw_ver"],
                hw_version=SYSINFO["hw_ver"],
                region="us-east-1",
            )
        ],
        upstream=_upstream(
            "cloud",
            _call(
                "get_devices",
                {
                    "devices": [
                        {"device_id": DEVICE_ID, "alias": "Bench lamp", "status": 1}
                    ]
                },
            ),
        ),
    )
    examples["listPlugs"] = PlugDirectory(
        plugs=[
            DirectoryEntry(
                mac=MAC,
                alias="Bench lamp",
                model="KP115(US)",
                local=DirectoryLocal(host=HOST, auth="failed", seen_at=T0),
                cloud=DirectoryCloud(device_id=DEVICE_ID, online=True, seen_at=T0),
            )
        ],
        local_error=None,
        cloud_error=None,
    )
    session = CloudSession(
        state=CloudState.AUTHENTICATED,
        username_masked="u***@example.com",
        logged_in_at=T0,
        last_error=None,
    )
    examples["getCloudSession"] = session
    examples["loginCloud"] = session
    examples["logoutCloud"] = CloudSession(
        state=CloudState.LOGIN_REQUIRED,
        username_masked="u***@example.com",
        logged_in_at=None,
        last_error=None,
    )
    examples["getHealth"] = Health(
        status="ok",
        version="0.1.0",
        local=LocalHealth(
            status="ready", discovery_target="255.255.255.255", plugs_known=1
        ),
        cloud=CloudHealth(status=CloudState.AUTHENTICATED, last_error=None),
    )
    examples["getVersion"] = Version(
        version="0.1.0",
        python="3.12.14",
        python_kasa="0.12.0",
        tplink_cloud_api="5.2.1",
    )
    return examples


def _plug_examples(kind: Driver) -> dict[str, BaseModel]:
    name = kind.capitalize()
    plug = _identity(kind)
    read = (
        _call("update", {"system": {"get_sysinfo": SYSINFO}})
        if kind == "local"
        else _call("get_sys_info", SYSINFO)
    )
    upstream = _upstream(kind, read)
    command = _call("turn_on" if kind == "local" else "power_on", {})
    settings = Settings(
        driver=kind, plug=plug, alias="Bench lamp", led_on=True, upstream=upstream
    )
    return {
        f"get{name}PlugState": PlugState(
            driver=kind,
            plug=plug,
            is_on=True,
            on_since=T0 - timedelta(seconds=125),
            rssi=-52,
            observed_at=T0,
            upstream=upstream,
        ),
        f"wait{name}Plug": WaitResult(
            driver=kind,
            plug=plug,
            until="on",
            satisfied=True,
            started_at=T0,
            satisfied_at=T0 + timedelta(milliseconds=1200),
            waited_ms=1200,
            polls=13 if kind == "local" else 2,
            poll_errors=0,
            last_observed=WaitObservation(
                is_on=True,
                online=True,
                authenticated=True if kind == "local" else None,
                listed=True if kind == "cloud" else None,
            ),
            last_error=None,
            upstream=upstream,
        ),
        f"set{name}PlugRelay": RelayResult(
            driver=kind,
            plug=plug,
            requested="on",
            expected_on=True,
            sent_at=T0,
            acknowledged_at=T0 + timedelta(milliseconds=180),
            latency_ms=180.0,
            is_on=True,
            confirmed=True,
            upstream=_upstream(kind, command, read),
        ),
        f"get{name}PlugEnergy": EnergyReading(
            driver=kind,
            plug=plug,
            power_w=60.1,
            voltage_v=120.2,
            current_a=0.5,
            energy_total_kwh=12.345,
            observed_at=T0,
            upstream=_upstream(
                kind,
                read
                if kind == "local"
                else _call(
                    "get_power_usage_realtime",
                    {"power_mw": 60100, "voltage_mv": 120200, "current_ma": 500},
                ),
            ),
        ),
        f"get{name}PlugInfo": DeviceInfo(
            driver=kind,
            plug=plug,
            hw_version="1.0",
            sw_version=SYSINFO["sw_ver"],
            has_energy_monitoring=True,
            cloud_link=CloudLink(
                bound=True,
                connected=True,
                server="n-devs.tplinkcloud.com",
                username_masked="u***@example.com",
            )
            if kind == "local"
            else None,
            account_entry=AccountEntry(
                online=True,
                region="us-east-1",
                app_server_url="https://n-use1-wap.tplinkcloud.com",
            )
            if kind == "cloud"
            else None,
            upstream=upstream,
        ),
        f"get{name}PlugSchedule": Schedule(
            driver=kind,
            plug=plug,
            rules=[
                ScheduleRule(
                    id="5D9A6C1E2F3B4A5C6D7E8F9012345678",
                    name="Evening on",
                    enabled=True,
                    repeat=True,
                    days=["mon", "tue", "wed", "thu", "fri"],
                    start=RuleTime(
                        action="on",
                        at="time",
                        minutes=1080,
                        offset_minutes=None if kind == "local" else 0,
                    ),
                    end=None,
                )
            ],
            upstream=upstream,
        ),
        f"get{name}PlugSettings": settings,
        f"update{name}PlugSettings": settings,
    }


def _identity(kind: Driver) -> PlugIdentity:
    return PlugIdentity(
        mac=MAC,
        host=HOST if kind == "local" else None,
        device_id=DEVICE_ID,
        alias="Bench lamp",
        model="KP115(US)",
    )


def _connection() -> LocalConnection:
    return LocalConnection(host=HOST, port=80, encrypt_type="KLAP", login_version=2)


def _call(
    op: str,
    response: dict[str, Any] | None,
    *,
    outcome: str = "ok",
    error_type: str | None = None,
    error_message: str | None = None,
) -> UpstreamCall:
    return UpstreamCall.model_validate(
        {
            "op": op,
            "started_at": T0,
            "duration_ms": 41.3,
            "outcome": outcome,
            "error_code": None,
            "error_type": error_type,
            "error_message": error_message,
            "response": response,
        }
    )


def _upstream(kind: Driver, *calls: UpstreamCall) -> Upstream:
    return Upstream(
        driver=kind,
        calls=list(calls),
        connection=_connection() if kind == "local" else None,
    )


def _driver_of(tags: list[str]) -> Driver | None:
    if "local" in tags:
        return "local"
    if "cloud" in tags:
        return "cloud"
    return None


def _codes(operation_id: str, status: int, kind: Driver | None) -> tuple[str, ...]:
    if status == 409:
        return _409.get(operation_id, ("confirmation_mismatch",))
    if status == 502:
        if operation_id == "loginCloud":
            return ("cloud_error",)
        return _502.get(kind or "local", ("device_error",))
    return _CODES_BY_STATUS.get(status, ("internal_error",))
