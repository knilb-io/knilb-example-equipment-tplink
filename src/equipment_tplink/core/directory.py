"""In-memory plug directory: MAC ⇄ LAN host ⇄ cloud deviceId (SPEC §5)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from equipment_tplink.core.models import (
    AuthResult,
    DirectoryCloud,
    DirectoryEntry,
    DirectoryLocal,
    PlugIdentity,
)
from equipment_tplink.kit.clock import Clock

LOCAL_TTL_S = 300.0


@dataclass(frozen=True)
class LocalEntry:
    """Where a plug was last seen on the LAN."""

    mac: str
    host: str
    alias: str | None
    model: str | None
    device_id: str | None
    auth: AuthResult
    seen_at: datetime
    seen_mono: float


@dataclass(frozen=True)
class CloudEntry:
    """Where a plug was last seen in the account."""

    mac: str
    device_id: str
    alias: str | None
    model: str | None
    online: bool
    seen_at: datetime


class Directory:
    """Shared by both drivers; merged by MAC for ``listPlugs``."""

    def __init__(self, clock: Clock, *, local_ttl_s: float = LOCAL_TTL_S) -> None:
        self._clock = clock
        self._ttl = local_ttl_s
        self._local: dict[str, LocalEntry] = {}
        self._cloud: dict[str, CloudEntry] = {}

    def upsert_local(
        self,
        identity: PlugIdentity,
        *,
        auth: AuthResult | None = None,
    ) -> None:
        """Record that ``identity`` (with MAC and host) answered on the LAN."""
        if identity.mac is None or identity.host is None:
            return
        previous = self._local.get(identity.mac)
        self._local[identity.mac] = LocalEntry(
            mac=identity.mac,
            host=identity.host,
            alias=identity.alias or (previous.alias if previous else None),
            model=identity.model or (previous.model if previous else None),
            device_id=identity.device_id or (previous.device_id if previous else None),
            auth=auth or (previous.auth if previous else "not_attempted"),
            seen_at=self._clock.now(),
            seen_mono=self._clock.monotonic(),
        )

    def set_local_auth(self, mac: str, auth: AuthResult) -> None:
        """Update the auth result of a known local entry."""
        entry = self._local.get(mac)
        if entry is not None:
            self._local[mac] = replace(entry, auth=auth)

    def local_by_mac(self, mac: str) -> LocalEntry | None:
        """Return a fresh local entry for ``mac`` (expired entries are dropped)."""
        entry = self._local.get(mac)
        if entry is None:
            return None
        if self._clock.monotonic() - entry.seen_mono > self._ttl:
            del self._local[mac]
            return None
        return entry

    def mac_for_host(self, host: str) -> str | None:
        """Return the MAC last seen at ``host``."""
        for entry in self._local.values():
            if entry.host == host:
                return entry.mac
        return None

    def forget_local(self, mac: str) -> None:
        """Drop the local entry for ``mac``."""
        self._local.pop(mac, None)

    def replace_cloud(self, entries: list[CloudEntry]) -> None:
        """Replace the cloud view with a fresh account list."""
        self._cloud = {entry.mac: entry for entry in entries}

    def local_count(self) -> int:
        """Number of plugs currently known on the LAN."""
        return len(self._local)

    def entries(self) -> list[DirectoryEntry]:
        """Merge local and cloud views by MAC, sorted by MAC."""
        result = []
        for mac in sorted(set(self._local) | set(self._cloud)):
            local = self.local_by_mac(mac)
            cloud = self._cloud.get(mac)
            if local is None and cloud is None:
                continue
            result.append(
                DirectoryEntry(
                    mac=mac,
                    alias=(local.alias if local else None)
                    or (cloud.alias if cloud else None),
                    model=(local.model if local else None)
                    or (cloud.model if cloud else None),
                    local=DirectoryLocal(
                        host=local.host, auth=local.auth, seen_at=local.seen_at
                    )
                    if local
                    else None,
                    cloud=DirectoryCloud(
                        device_id=cloud.device_id,
                        online=cloud.online,
                        seen_at=cloud.seen_at,
                    )
                    if cloud
                    else None,
                )
            )
        return result
