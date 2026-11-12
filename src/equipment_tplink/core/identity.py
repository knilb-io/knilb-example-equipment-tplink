"""Parse the ``{plug}`` path parameter (SPEC §5)."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Literal

from equipment_tplink.core.errors import PlugError

RefKind = Literal["mac", "ipv4", "hostname", "device_id"]

_DEVICE_ID = re.compile(r"^[0-9A-Fa-f]{40}$")
_MAC_SEPARATORS = re.compile(r"[:\-.]")
_HEX12 = re.compile(r"^[0-9A-Fa-f]{12}$")
_DOTTED_QUAD = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")

_ACCEPTED: dict[str, set[RefKind]] = {
    "local": {"mac", "ipv4", "hostname"},
    "cloud": {"mac", "device_id"},
}


@dataclass(frozen=True)
class PlugRef:
    """A parsed plug reference; ``value`` is normalized."""

    kind: RefKind
    value: str


def normalize_mac(value: str | None) -> str | None:
    """Return ``AA:BB:CC:DD:EE:FF`` for any common MAC spelling, else ``None``."""
    if not value:
        return None
    digits = _MAC_SEPARATORS.sub("", value.strip())
    if not _HEX12.match(digits):
        return None
    digits = digits.upper()
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def classify(raw: str) -> PlugRef | None:
    """Classify ``raw`` without namespace rules, or return ``None``."""
    value = raw.strip()
    if _DEVICE_ID.match(value):
        return PlugRef("device_id", value.upper())
    mac = normalize_mac(value)
    if mac is not None:
        return PlugRef("mac", mac)
    if _DOTTED_QUAD.match(value):
        try:
            return PlugRef("ipv4", str(ipaddress.IPv4Address(value)))
        except ValueError:
            return None
    if _is_hostname(value):
        return PlugRef("hostname", value.lower())
    return None


def parse_plug_ref(raw: str, *, namespace: Literal["local", "cloud"]) -> PlugRef:
    """Parse ``raw`` for ``namespace`` or raise ``400 invalid_plug_ref``."""
    ref = classify(raw)
    if ref is None:
        raise PlugError(
            "invalid_plug_ref",
            f"{raw!r} is not a MAC, IPv4 address, hostname or cloud deviceId",
            driver=namespace,
        )
    if ref.kind not in _ACCEPTED[namespace]:
        accepted = ", ".join(sorted(_ACCEPTED[namespace]))
        raise PlugError(
            "invalid_plug_ref",
            f"a {ref.kind} is not accepted by the {namespace} driver (use {accepted})",
            driver=namespace,
        )
    return ref


def _is_hostname(value: str) -> bool:
    if not value or len(value) > 253 or value.replace(".", "").isdigit():
        return False
    labels = value.rstrip(".").split(".")
    return all(_LABEL.match(label) for label in labels)
