"""Normalize raw Kasa schedule rules (shared by both drivers)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, get_args

from equipment_tplink.core.models import RuleTime, ScheduleRule

Day = Literal["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
_DAYS: tuple[Day, ...] = get_args(Day)
_ACTIONS: dict[int, Literal["on", "off"]] = {1: "on", 0: "off"}
_TIME_OPTIONS: dict[int, Literal["time", "sunrise", "sunset"]] = {
    0: "time",
    1: "sunrise",
    2: "sunset",
}


def parse_rule(raw: Mapping[str, Any]) -> ScheduleRule:
    """Convert one raw IOT rule (``sact``, ``stime_opt``, ``smin``…) to the API shape.

    ``soffset``/``eoffset`` are optional: python-kasa's ``Rule`` omits them
    (FINDINGS F20), tplink-cloud-api's includes them.
    """
    wday = list(raw.get("wday") or [])
    days = [_DAYS[index] for index, flag in enumerate(wday[:7]) if _as_int(flag) == 1]
    end_action = _as_int(raw.get("eact"))
    end = None
    if end_action in _ACTIONS:
        end = _rule_time(
            raw.get("eact"), raw.get("etime_opt"), raw.get("emin"), raw.get("eoffset")
        )
    return ScheduleRule(
        id=str(raw.get("id", "")),
        name=raw.get("name") or None,
        enabled=_as_int(raw.get("enable")) == 1,
        repeat=_as_int(raw.get("repeat")) == 1,
        days=days,
        start=_rule_time(
            raw.get("sact"), raw.get("stime_opt"), raw.get("smin"), raw.get("soffset")
        ),
        end=end,
    )


def _rule_time(action: Any, option: Any, minutes: Any, offset: Any) -> RuleTime:
    return RuleTime(
        action=_ACTIONS.get(_as_int(action) if action is not None else -1),
        at=_TIME_OPTIONS.get(_as_int(option) if option is not None else -1),
        minutes=_as_int(minutes) if minutes is not None else None,
        offset_minutes=_as_int(offset) if offset is not None else None,
    )


def _as_int(value: Any) -> int:
    """Coerce ints, int-like enums and numeric strings; anything else is -1."""
    raw = getattr(value, "value", value)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1
