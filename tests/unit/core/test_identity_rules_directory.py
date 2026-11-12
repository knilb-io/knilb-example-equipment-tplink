"""Plug references, schedule rule parsing and the directory."""

from __future__ import annotations

from datetime import timedelta

import pytest

from equipment_tplink.core.directory import CloudEntry, Directory
from equipment_tplink.core.errors import PlugError
from equipment_tplink.core.identity import classify, normalize_mac, parse_plug_ref
from equipment_tplink.core.models import PlugIdentity
from equipment_tplink.core.rules import parse_rule
from equipment_tplink.kit.clock import FakeClock


@pytest.mark.parametrize(
    ("raw", "kind", "value"),
    [
        ("00-00-5E-00-53-01", "mac", "00:00:5E:00:53:01"),
        ("0000.5e00.5301", "mac", "00:00:5E:00:53:01"),
        ("192.0.2.24", "ipv4", "192.0.2.24"),
        ("KP115-Bench.lan", "hostname", "kp115-bench.lan"),
        (
            "8006f1a2b3c4d5e6f708192a3b4c5d6e7f801234",
            "device_id",
            "8006F1A2B3C4D5E6F708192A3B4C5D6E7F801234",
        ),
    ],
)
def test_classify(raw: str, kind: str, value: str) -> None:
    ref = classify(raw)
    assert ref is not None
    assert (ref.kind, ref.value) == (kind, value)


@pytest.mark.parametrize("raw", ["", "999.1.1.1", "lamp!", "-bad", "a" * 254, "123"])
def test_classify_rejects(raw: str) -> None:
    assert classify(raw) is None


def test_normalize_mac() -> None:
    assert normalize_mac(None) is None
    assert normalize_mac("zz:zz") is None


def test_namespace_rules() -> None:
    assert parse_plug_ref("192.0.2.1", namespace="local").kind == "ipv4"
    with pytest.raises(PlugError) as error:
        parse_plug_ref("192.0.2.1", namespace="cloud")
    assert error.value.code == "invalid_plug_ref"
    assert "device_id, mac" in error.value.message
    with pytest.raises(PlugError):
        parse_plug_ref("8006F1A2B3C4D5E6F708192A3B4C5D6E7F801234", namespace="local")
    with pytest.raises(PlugError):
        parse_plug_ref("no way", namespace="local")


def test_parse_rule_variants() -> None:
    rule = parse_rule(
        {
            "id": 7,
            "name": "",
            "enable": "1",
            "repeat": 0,
            "wday": [1, 0, 0, 0, 0, 0, 1],
            "sact": 0,
            "stime_opt": 2,
            "smin": 1100,
            "soffset": -15,
            "eact": 1,
            "etime_opt": 1,
            "emin": 400,
            "eoffset": 10,
        }
    )
    assert rule.id == "7" and rule.name is None and rule.enabled and not rule.repeat
    assert rule.days == ["sun", "sat"]
    assert (rule.start.action, rule.start.at, rule.start.offset_minutes) == (
        "off",
        "sunset",
        -15,
    )
    assert rule.end is not None and rule.end.action == "on" and rule.end.at == "sunrise"
    odd = parse_rule({"sact": object(), "stime_opt": None, "smin": None, "wday": None})
    assert (
        odd.start.action is None and odd.start.at is None and odd.start.minutes is None
    )
    assert odd.end is None and odd.days == []


def test_directory(clock: FakeClock) -> None:
    directory = Directory(clock, local_ttl_s=10)
    directory.upsert_local(PlugIdentity(mac=None, host="192.0.2.1"))
    assert directory.local_count() == 0
    plug = PlugIdentity(mac="00:00:5E:00:53:01", host="192.0.2.1", alias="A", model="M")
    directory.upsert_local(plug, auth="failed")
    directory.upsert_local(PlugIdentity(mac=plug.mac, host="192.0.2.1"))
    entry = directory.local_by_mac("00:00:5E:00:53:01")
    assert entry is not None and entry.alias == "A" and entry.auth == "failed"
    directory.set_local_auth(plug.mac or "", "ok")
    directory.set_local_auth("00:00:5E:00:53:99", "ok")
    assert directory.mac_for_host("192.0.2.1") == plug.mac
    assert directory.mac_for_host("192.0.2.2") is None
    directory.replace_cloud(
        [
            CloudEntry(
                mac="00:00:5E:00:53:02",
                device_id="D2",
                alias=None,
                model=None,
                online=False,
                seen_at=clock.now(),
            )
        ]
    )
    macs = [item.mac for item in directory.entries()]
    assert macs == ["00:00:5E:00:53:01", "00:00:5E:00:53:02"]
    clock.advance(11)
    assert directory.local_by_mac("00:00:5E:00:53:01") is None
    assert [item.mac for item in directory.entries()] == ["00:00:5E:00:53:02"]
    directory.forget_local("nothing")
    assert clock.now() - entry.seen_at == timedelta(seconds=11)
