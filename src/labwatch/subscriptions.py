"""Who gets which alert: subscriber levels, personal mutes and mute targets.

Shared by the Telegram and Discord bots. A *subscriber* is one place alerts go
to (a person's Telegram chat, a Discord channel); each has its own level and
its own mutes. Mutes "for everyone" are handled by the engine before any of this.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from labwatch import report
from labwatch.models import AlertEvent, EventKind, Severity
from labwatch.notifiers.base import is_lifecycle
from labwatch.state import StateStore, Subscriber, mute_matches

if TYPE_CHECKING:
    from labwatch.engine import DeviceRuntime, Monitor

DEFAULT_MUTE = 2 * 3600.0
MAX_GLOBAL_MUTE = 14 * 86400.0
_DURATION = re.compile(r"^\d+(\.\d+)?[smhd]?$")
FOREVER = {"forever", "off", "indefinitely", "permanent", "permanently"}

CATEGORY_HELP = {
    "pulse_tube": "pulse tube & its compressor (CPA)",
    "temperature": "T50K, T4K, still, MXC",
    "circulation": "scroll/turbo pumps, flow",
    "cooling_water": "cooling water",
    "pressure": "P1–P6",
    "anomaly": "'unusual compared to the last 24 h'",
    "connection": "device / PC unreachable",
    "fse": "fast sample exchange",
    "labwatch": "started/stopped, alarms armed, mute announcements",
}


def is_duration(tok: str) -> bool:
    t = tok.lower()
    return t in FOREVER or bool(_DURATION.match(t))


def parse_duration(tok: str) -> float | None:
    """Seconds, or None for "forever". A bare number means hours."""
    t = tok.lower()
    if t in FOREVER:
        return None
    if t[-1].isdigit():
        return float(t) * 3600
    return float(t[:-1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[t[-1]]


def fmt_until(until: float | None) -> str:
    if until is None:
        return "until unmuted"
    return "until " + time.strftime("%a %H:%M", time.localtime(until))


def categories(monitor: Monitor) -> list[str]:
    cats = {r.category for rt in monitor.runtimes for r in rt.all_rules} | set(CATEGORY_HELP)
    return sorted(cats - {"other"})


def find_device(monitor: Monitor, name: str) -> DeviceRuntime | None:
    key = report.command_name(name)
    for rt in monitor.runtimes:
        if report.command_name(rt.name) == key:
            return rt
    return None


def parse_target(monitor: Monitor, tokens: list[str]) -> tuple[str | None, list[str], str]:
    """(target, remaining tokens, error). Device, alarm type, device + type, or all."""
    if not tokens:
        return None, [], "say what to mute"
    cats = categories(monitor)
    first = tokens[0].lower()
    if first in ("all", "*", "everything"):
        return "*", tokens[1:], ""
    if "/" in first:
        dev, _, cat = first.partition("/")
        rt = find_device(monitor, dev)
        if rt and cat in cats:
            return f"{rt.name}/{cat}", tokens[1:], ""
        return None, [], f"unknown target {tokens[0]!r}"
    rt = find_device(monitor, first)
    if rt is not None:
        if len(tokens) > 1 and tokens[1].lower() in cats:
            return f"{rt.name}/{tokens[1].lower()}", tokens[2:], ""
        return rt.name, tokens[1:], ""
    if first in cats:
        return first, tokens[1:], ""
    return None, [], f"unknown target {tokens[0]!r}"


def wants(sub: Subscriber, event: AlertEvent, store: StateStore, lifecycle: bool = True,
          now: float | None = None) -> bool:
    """Does this subscriber get this event (level, lifecycle setting, personal mutes)?"""
    if event.kind is EventKind.TEST:
        if sub.role != "admin":
            return False
    elif is_lifecycle(event):
        if not lifecycle:
            return False
    elif event.severity < Severity.parse(sub.level):
        return False
    now = time.time() if now is None else now
    return not any(mute_matches(m.target, event.device, event.rule_id, event.category)
                   for m in store.personal_mutes(sub.chat_id, now))
