"""A deterministic, clock-driven world of fake Kasa plugs.

Both fake library surfaces (``fakes.kasa`` and ``fakes.cloud``) read and change this
one world, so a cloud command is visible to the local driver, just like on a bench.
Documentation-range identifiers are used throughout (RFC 7042 MACs, RFC 5737 IPs).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from equipment_tplink.kit.clock import Clock


@dataclass
class FakePlug:
    """One fake plug and its physical state."""

    mac: str
    host: str
    device_id: str
    alias: str
    model: str = "KP115(US)"
    hw_ver: str = "1.0"
    sw_ver: str = "1.0.21 Build 231129 Rel.171238"
    has_emeter: bool = True
    load_w: float = 60.0
    voltage_v: float = 120.0
    total_kwh: float = 12.345
    rssi: int = -52
    led_on: bool = True
    is_on: bool = False
    on_since_mono: float | None = None
    bound: bool = True
    cloud_connected: bool = True
    listed: bool = True
    cloud: str = "kasa"  # which TP-Link cloud lists the plug: "kasa" or "tapo"
    encrypt_type: str = "KLAP"
    login_version: int = 2
    offline_until: float | None = None
    next_host: str | None = None
    rules: list[dict[str, Any]] = field(default_factory=list)
    pending: list[tuple[float, bool]] = field(default_factory=list)


@dataclass
class FakeAccount:
    """The fake TP-Link account."""

    username: str = "user@example.com"
    password: str = "fake-password"
    mfa_code: str | None = None
    token_expired: bool = False
    list_error: Exception | None = None
    # The Kasa cloud asks for MFA inside a success reply, which tplink-cloud-api
    # misses: the login "succeeds" without a token (FINDINGS F23).
    kasa_nested_mfa: bool = False
    tapo_login_ok: bool = True


def default_plugs() -> list[FakePlug]:
    """Two plugs with documentation-range identifiers."""
    return [
        FakePlug(
            mac="00:00:5E:00:53:01",
            host="192.0.2.10",
            device_id="8006F1A2B3C4D5E6F708192A3B4C5D6E7F801234",
            alias="Bench lamp",
            rules=[
                {
                    "id": "5D9A6C1E2F3B4A5C6D7E8F9012345678",
                    "name": "Evening on",
                    "enable": 1,
                    "wday": [0, 1, 1, 1, 1, 1, 0],
                    "repeat": 1,
                    "sact": 1,
                    "stime_opt": 0,
                    "smin": 1080,
                    "soffset": 0,
                    "eact": -1,
                    "etime_opt": -1,
                    "emin": 0,
                    "eoffset": 0,
                }
            ],
        ),
        FakePlug(
            mac="00:00:5E:00:53:02",
            host="192.0.2.11",
            device_id="8006F1A2B3C4D5E6F708192A3B4C5D6E7F805678",
            alias="Spare plug",
            model="KP125(US)",
        ),
    ]


class FakeWorld:
    """State shared by the fake python-kasa and tplink-cloud-api surfaces."""

    def __init__(
        self,
        clock: Clock,
        *,
        plugs: list[FakePlug] | None = None,
        account: FakeAccount | None = None,
        cloud_latency_s: float = 0.0,
        reboot_offline_s: float = 2.0,
        reset_offline_s: float = 10.0,
        accept_local_credentials: bool = True,
    ) -> None:
        self.clock = clock
        self.plugs = plugs if plugs is not None else default_plugs()
        self.account = account or FakeAccount()
        self.cloud_latency_s = cloud_latency_s
        self.reboot_offline_s = reboot_offline_s
        self.reset_offline_s = reset_offline_s
        self.accept_local_credentials = accept_local_credentials
        self.calls: list[str] = []

    # -- lookup -------------------------------------------------------------------

    def by_host(self, host: str) -> FakePlug | None:
        """Return the plug currently at ``host``."""
        for plug in self.plugs:
            self.settle(plug)
            if plug.host == host:
                return plug
        return None

    def by_mac(self, mac: str) -> FakePlug:
        """Return the plug with ``mac`` (canonical form)."""
        return next(plug for plug in self.plugs if plug.mac == mac)

    # -- time ---------------------------------------------------------------------

    def settle(self, plug: FakePlug) -> None:
        """Apply everything that should have happened by now."""
        now = self.clock.monotonic()
        due = [item for item in plug.pending if item[0] <= now]
        plug.pending = [item for item in plug.pending if item[0] > now]
        for _, on in due:
            self._apply_relay(plug, on)
        if plug.offline_until is not None and now >= plug.offline_until:
            plug.offline_until = None
            if plug.next_host is not None:
                # "A person re-provisioned it with the Kasa app" and DHCP moved it.
                plug.host, plug.next_host = plug.next_host, None
                plug.bound = True
                plug.cloud_connected = True

    def online(self, plug: FakePlug) -> bool:
        """Whether the plug is on the network right now."""
        self.settle(plug)
        return plug.offline_until is None

    # -- actions ------------------------------------------------------------------

    def set_relay(self, plug: FakePlug, on: bool, *, via_cloud: bool = False) -> None:
        """Switch the relay, after the cloud latency when sent through the cloud."""
        self.settle(plug)
        if via_cloud and self.cloud_latency_s > 0:
            plug.pending.append((self.clock.monotonic() + self.cloud_latency_s, on))
        else:
            self._apply_relay(plug, on)

    def reboot(self, plug: FakePlug, delay_s: float = 1.0) -> None:
        """Go offline for ``reboot_offline_s`` (the relay turns off)."""
        plug.offline_until = self.clock.monotonic() + delay_s + self.reboot_offline_s
        self._apply_relay(plug, False)

    def factory_reset(self, plug: FakePlug) -> None:
        """Go offline, then come back re-provisioned on a new host."""
        plug.offline_until = self.clock.monotonic() + self.reset_offline_s
        plug.bound = False
        plug.cloud_connected = False
        plug.alias = f"TP-LINK_Smart Plug_{plug.mac[-5:].replace(':', '')}"
        plug.rules = []
        octets = plug.host.rsplit(".", 1)
        plug.next_host = f"{octets[0]}.{int(octets[1]) + 100}"
        self._apply_relay(plug, False)

    def _apply_relay(self, plug: FakePlug, on: bool) -> None:
        if on and not plug.is_on:
            plug.on_since_mono = self.clock.monotonic()
        if not on:
            plug.on_since_mono = None
        plug.is_on = on

    # -- readings -----------------------------------------------------------------

    def on_time_s(self, plug: FakePlug) -> int:
        """Seconds since the relay turned on (0 when off)."""
        if not plug.is_on or plug.on_since_mono is None:
            return 0
        return int(self.clock.monotonic() - plug.on_since_mono)

    def power_w(self, plug: FakePlug) -> float:
        """Instantaneous power draw."""
        return plug.load_w if plug.is_on else 0.0

    def sysinfo(self, plug: FakePlug) -> dict[str, Any]:
        """Raw IOT ``system.get_sysinfo`` for ``plug``."""
        self.settle(plug)
        return {
            "sw_ver": plug.sw_ver,
            "hw_ver": plug.hw_ver,
            "model": plug.model,
            "deviceId": plug.device_id,
            "oemId": "FFF22CFF774A0B89F7624BFC6F50D5DE",
            "hwId": "60FF6B258734EA6880E186F8C96DDC61",
            "rssi": plug.rssi,
            "latitude_i": 0,
            "longitude_i": 0,
            "alias": plug.alias,
            "status": "new",
            "mic_type": "IOT.SMARTPLUGSWITCH",
            "feature": "TIM:ENE" if plug.has_emeter else "TIM",
            "mac": plug.mac,
            "updating": 0,
            "led_off": 0 if plug.led_on else 1,
            "relay_state": 1 if plug.is_on else 0,
            "on_time": self.on_time_s(plug),
            "icon_hash": "",
            "dev_name": "Smart Wi-Fi Plug Mini",
            "active_mode": "none",
            "next_action": {"type": -1},
            "err_code": 0,
        }
