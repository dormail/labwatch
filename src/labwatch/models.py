"""Core data types shared by devices, rules, the engine and notifiers."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def parse_duration(v: Any) -> float:
    """Seconds from a number or a string like '90s', '5m', '2h', '1d'."""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s and s[-1] in mult:
        return float(s[:-1]) * mult[s[-1]]
    return float(s)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Severity(enum.IntEnum):
    INFO = 10
    WARNING = 20
    CRITICAL = 30

    @classmethod
    def parse(cls, value: str | int | Severity) -> Severity:
        if isinstance(value, Severity):
            return value
        if isinstance(value, int):
            return cls(value)
        try:
            return cls[str(value).strip().upper()]
        except KeyError:
            names = ", ".join(s.name.lower() for s in cls)
            raise ValueError(f"unknown severity {value!r} (expected one of: {names})") from None

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class Reading:
    """One value read from a device.

    ``value`` is None when the device reports the value as invalid/unavailable
    (e.g. a RuOx sensor out of its calibration range at room temperature).
    ``timestamp`` is when the *device* measured it (may be older than the poll).
    """

    device: str
    channel: str  # friendly name, e.g. "T4K"
    source: str  # device-native identifier, e.g. "mapper.bf.temperatures.t4k"
    value: float | None
    timestamp: datetime | None
    unit: str | None = None
    raw: str | None = None
    status: str | None = None
    outdated: bool = False
    label: str | None = None  # human-readable state for enumerations, e.g. "on"/"off"

    @property
    def valid(self) -> bool:
        return self.value is not None

    def age(self, now: datetime | None = None) -> float | None:
        """Seconds since the device measured this value."""
        if self.timestamp is None:
            return None
        return ((now or utcnow()) - self.timestamp).total_seconds()

    def format_value(self) -> str:
        if self.label is not None:
            return self.label
        if self.value is None:
            if self.raw is not None and self.status not in {"INVALID", "NOT_FOUND"}:
                return self.raw.strip() or "-"  # text channel
            return f"invalid ({self.status or 'n/a'})"
        return format_quantity(self.value, self.unit)


def format_quantity(value: float | None, unit: str | None = None) -> str:
    if value is None:
        return "n/a"
    if value == 0:
        value = 0.0  # avoid "-0"
    a = abs(value)
    if a != 0 and (a < 1e-3 or a >= 1e5):
        s = f"{value:.3e}"
    elif a < 1:
        s = f"{value:.4g}"
    else:
        s = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{s} {unit}" if unit else s


class EventKind(str, enum.Enum):
    FIRING = "firing"  # a rule just started firing
    REMINDER = "reminder"  # a rule is still firing (periodic reminder)
    RESOLVED = "resolved"  # a firing rule recovered
    INFO = "info"  # informational (startup, rule armed, ...)
    TEST = "test"  # `labwatch test-notify`


@dataclass
class AlertEvent:
    """Something a notifier should tell a human about."""

    kind: EventKind
    rule_id: str  # stable id, e.g. "Flash/T4K above 6"
    device: str
    title: str  # one line, e.g. "Flash: T4K above 6 K"
    message: str  # details, may be multi-line
    severity: Severity
    timestamp: datetime = field(default_factory=utcnow)
    channel: str | None = None
    value: float | None = None
    unit: str | None = None
    since: datetime | None = None  # when the condition started

    @property
    def headline(self) -> str:
        prefix = {
            EventKind.FIRING: f"[{self.severity.name}]",
            EventKind.REMINDER: f"[STILL {self.severity.name}]",
            EventKind.RESOLVED: "[RESOLVED]",
            EventKind.INFO: "[INFO]",
            EventKind.TEST: "[TEST]",
        }[self.kind]
        return f"{prefix} {self.title}"
