"""Alert rules and their state machine.

A rule watches one channel of one device. Its *condition* is either true
(something is wrong), false (healthy) or unknown (no usable data). The state
machine around it adds the behaviour that makes alerts trustworthy:

* **auto-arm** - a rule only becomes active after its condition has been healthy
  for ``arm_after`` seconds. A warm fridge doesn't spam "T4K too high"; once it
  has been cold for a while, the rule arms itself.
* **for** - the condition must hold for this long before the alert fires
  (debounce against single bad readings).
* **clear_for** - the condition must be healthy this long before the alert
  resolves (prevents flapping around a threshold).
* **repeat_interval** - reminders while the alert keeps firing (0 = never).

Rule kinds are selected by which keys appear in the rule's config:

=======================  ====================================================
``above`` / ``below``    threshold (optional ``hysteresis``)
``rate_above`` /         rate of change in units per minute over ``window``
``rate_below``           seconds (e.g. T4K rising faster than 0.05 K/min)
``equals`` /             state of an enumeration, e.g. ``not_equals: 1`` on
``not_equals``           the pulse tube (1 = on). Lists are allowed.
``max_age``              data older than N seconds (controller stopped updating)
=======================  ====================================================
"""

from __future__ import annotations

import abc
import collections
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from labwatch.models import Reading, Severity, format_quantity, parse_duration  # noqa: F401


class RuleConfigError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Evaluation context
# ---------------------------------------------------------------------------


class History:
    """Recent (timestamp, value) samples per channel, for rate rules."""

    def __init__(self, keep_seconds: float = 3600.0) -> None:
        self.keep = keep_seconds
        self._data: dict[str, collections.deque[tuple[float, float]]] = collections.defaultdict(collections.deque)

    def add(self, reading: Reading, fallback_ts: float) -> None:
        if reading.value is None:
            return
        ts = reading.timestamp.timestamp() if reading.timestamp else fallback_ts
        dq = self._data[reading.channel]
        if dq and ts <= dq[-1][0]:
            return  # same sample polled twice (controller hasn't updated yet)
        dq.append((ts, reading.value))
        while dq and dq[0][0] < ts - self.keep:
            dq.popleft()

    def window(self, channel: str, seconds: float, now: float) -> list[tuple[float, float]]:
        return [(t, v) for t, v in self._data.get(channel, ()) if t >= now - seconds]


def slope_per_minute(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope in units per minute."""
    n = len(points)
    if n < 2:
        return None
    mt = sum(t for t, _ in points) / n
    mv = sum(v for _, v in points) / n
    den = sum((t - mt) ** 2 for t, _ in points)
    if den == 0:
        return None
    return 60.0 * sum((t - mt) * (v - mv) for t, v in points) / den


@dataclass
class Context:
    now: float  # epoch seconds
    readings: dict[str, Reading]  # latest reading per channel
    history: History


@dataclass
class Verdict:
    active: bool | None  # None = no usable data, keep state unchanged
    value: float | None = None
    detail: str = ""  # e.g. "T4K = 7.31 K"


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


@dataclass
class RuleState:
    armed: bool = False
    firing: bool = False
    delivered: bool = False  # FIRING notification actually sent (False while muted)
    pending_since: float | None = None  # condition true since
    healthy_since: float | None = None  # condition false since
    fired_at: float | None = None
    last_notified: float | None = None
    last_value: float | None = None
    last_detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RuleState:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


class Transition:
    ARMED = "armed"
    FIRING = "firing"
    REMINDER = "reminder"
    RESOLVED = "resolved"


@dataclass
class RuleOptions:
    severity: Severity = Severity.WARNING
    for_s: float = 0.0
    clear_for: float = 60.0
    auto_arm: bool = True
    arm_after: float = 300.0
    repeat_interval: float = 3600.0
    message: str = ""  # extra text for humans, e.g. what to check
    detail_channel: str | None = None  # append this channel's text (e.g. alarm text)
    on_invalid: str = "ignore"  # "alarm": an unreadable value (DISCONNECTED, INVALID) counts as a problem


class Rule(abc.ABC):
    kind: str = ""

    def __init__(self, device: str, channel: str, name: str | None, opts: RuleOptions) -> None:
        self.device = device
        self.channel = channel
        self.opts = opts
        self.name = name or f"{channel} {self.describe()}"
        self.rule_id = f"{device}/{self.name}"
        self.state = RuleState(armed=not opts.auto_arm)

    # -- to implement ---------------------------------------------------------

    @abc.abstractmethod
    def describe(self) -> str:
        """Condition in words, e.g. 'above 6 K'."""

    @abc.abstractmethod
    def check(self, ctx: Context) -> Verdict: ...

    # -- helpers --------------------------------------------------------------

    def _reading(self, ctx: Context) -> Reading | None:
        r = ctx.readings.get(self.channel)
        return r

    def _invalid(self, r: Reading | None) -> Verdict:
        """Verdict for a missing/unreadable value: no data, or an alarm with ``on_invalid: alarm``."""
        if r is not None and self.opts.on_invalid == "alarm":
            return Verdict(True, None, f"{self.channel} unreadable ({r.status or 'no value'})")
        return Verdict(None)

    @property
    def title(self) -> str:
        return f"{self.device}: {self.name}"

    def step(self, ctx: Context) -> tuple[Verdict, str | None]:
        """Evaluate and advance the state machine. Returns (verdict, transition)."""
        v = self.check(ctx)
        st = self.state
        now = ctx.now
        if v.active is None:
            return v, None
        st.last_value = v.value
        st.last_detail = v.detail
        if v.active:
            st.healthy_since = None
            if not st.armed:
                return v, None
            if st.pending_since is None:
                st.pending_since = now
            if not st.firing:
                if now - st.pending_since >= self.opts.for_s:
                    st.firing = True
                    st.delivered = False
                    st.fired_at = now
                    st.last_notified = now
                    return v, Transition.FIRING
                return v, None
            if (
                self.opts.repeat_interval > 0
                and st.last_notified is not None
                and now - st.last_notified >= self.opts.repeat_interval
            ):
                st.last_notified = now
                return v, Transition.REMINDER
            return v, None
        # healthy
        st.pending_since = None
        if st.healthy_since is None:
            st.healthy_since = now
        healthy_for = now - st.healthy_since
        if st.firing:
            if healthy_for >= self.opts.clear_for:
                st.firing = False
                was_delivered = st.delivered
                st.delivered = False
                st.fired_at = None
                return v, (Transition.RESOLVED if was_delivered else None)
            return v, None
        if not st.armed and healthy_for >= self.opts.arm_after:
            st.armed = True
            return v, Transition.ARMED
        return v, None

    def status_text(self) -> str:
        st = self.state
        if st.firing:
            return "FIRING"
        if st.pending_since is not None and st.armed:
            return "pending"
        return "armed" if st.armed else "disarmed"


def _fmt(value: float | None, unit: str | None) -> str:
    return format_quantity(value, unit)


class ThresholdRule(Rule):
    kind = "threshold"

    def __init__(self, device, channel, name, opts, above=None, below=None, hysteresis=0.0, unit=None):
        if above is None and below is None:
            raise RuleConfigError("threshold rule needs 'above' and/or 'below'")
        self.above = None if above is None else float(above)
        self.below = None if below is None else float(below)
        self.hysteresis = float(hysteresis or 0.0)
        self.unit = unit
        super().__init__(device, channel, name, opts)

    def describe(self) -> str:
        parts = []
        if self.above is not None:
            parts.append(f"above {_fmt(self.above, self.unit)}")
        if self.below is not None:
            parts.append(f"below {_fmt(self.below, self.unit)}")
        return " or ".join(parts)

    def check(self, ctx: Context) -> Verdict:
        r = self._reading(ctx)
        if r is None or r.value is None:
            return self._invalid(r)
        h = self.hysteresis if self.state.firing else 0.0
        x = r.value
        active = (self.above is not None and x > self.above - h) or (self.below is not None and x < self.below + h)
        return Verdict(active, x, f"{self.channel} = {r.format_value()}")


class RateRule(Rule):
    kind = "rate"

    def __init__(self, device, channel, name, opts, rate_above=None, rate_below=None, window=300.0, unit=None):
        if rate_above is None and rate_below is None:
            raise RuleConfigError("rate rule needs 'rate_above' and/or 'rate_below'")
        self.rate_above = None if rate_above is None else float(rate_above)
        self.rate_below = None if rate_below is None else float(rate_below)
        self.window = float(window)
        self.unit = unit
        super().__init__(device, channel, name, opts)

    def describe(self) -> str:
        u = f"{self.unit}/min" if self.unit else "/min"
        parts = []
        if self.rate_above is not None:
            parts.append(f"rising faster than {format_quantity(self.rate_above)} {u}")
        if self.rate_below is not None:
            parts.append(f"changing slower than {format_quantity(self.rate_below)} {u}")
        return " or ".join(parts)

    def check(self, ctx: Context) -> Verdict:
        pts = ctx.history.window(self.channel, self.window, ctx.now)
        if len(pts) < 3 or pts[-1][0] - pts[0][0] < 0.5 * self.window:
            return Verdict(None)
        slope = slope_per_minute(pts)
        if slope is None:
            return Verdict(None)
        active = (self.rate_above is not None and slope > self.rate_above) or (
            self.rate_below is not None and slope < self.rate_below
        )
        u = f"{self.unit}/min" if self.unit else "/min"
        r = self._reading(ctx)
        now_txt = f", now {r.format_value()}" if r is not None else ""
        return Verdict(active, slope, f"{self.channel} changing at {slope:+.3g} {u} over {self.window / 60:g} min{now_txt}")


class StateRule(Rule):
    kind = "state"

    def __init__(self, device, channel, name, opts, equals=None, not_equals=None, labels=None):
        if equals is None and not_equals is None:
            raise RuleConfigError("state rule needs 'equals' and/or 'not_equals'")
        self.equals = _as_float_set(equals)
        self.not_equals = _as_float_set(not_equals)
        self.labels = dict(labels or {})
        super().__init__(device, channel, name, opts)

    def _label(self, x: float) -> str:
        return self.labels.get(int(x), format_quantity(x)) if x == int(x) else format_quantity(x)

    def describe(self) -> str:
        parts = []
        if self.equals:
            parts.append("is " + "/".join(self._label(x) for x in sorted(self.equals)))
        if self.not_equals:
            parts.append("is not " + "/".join(self._label(x) for x in sorted(self.not_equals)))
        return " and ".join(parts)

    def check(self, ctx: Context) -> Verdict:
        r = self._reading(ctx)
        if r is None or r.value is None:
            return self._invalid(r)
        x = r.value
        active = (bool(self.equals) and x in self.equals) or (bool(self.not_equals) and x not in self.not_equals)
        return Verdict(active, x, f"{self.channel} = {r.format_value()}")


class MaxAgeRule(Rule):
    kind = "max_age"

    def __init__(self, device, channel, name, opts, max_age=900.0):
        self.max_age = float(max_age)
        super().__init__(device, channel, name, opts)

    def describe(self) -> str:
        return f"not updated for {self.max_age / 60:g} min"

    def check(self, ctx: Context) -> Verdict:
        r = self._reading(ctx)
        if r is None or r.timestamp is None:
            return Verdict(None)  # device unreachable / channel missing: other rules cover it
        age = ctx.now - r.timestamp.timestamp()
        return Verdict(age > self.max_age, age, f"{self.channel} last updated {age / 60:.1f} min ago")


class UnreachableRule(Rule):
    """Built-in per-device rule: polling has failed for ``unreachable_after`` seconds."""

    kind = "unreachable"

    def __init__(self, device: str, after: float, opts: RuleOptions) -> None:
        self.after = after
        self.last_success: float | None = None
        self.started: float | None = None
        self.last_error: str = ""
        super().__init__(device, "(connection)", "unreachable", opts)

    def describe(self) -> str:
        return f"unreachable for {self.after / 60:g} min"

    def check(self, ctx: Context) -> Verdict:
        if self.last_success is None and self.state.firing:
            # Restarted while the alert was firing and no successful poll since: still down.
            ref = self.started
            if ref is None:
                return Verdict(None)
            return Verdict(True, ctx.now - ref, f"still unreachable (no successful poll since labwatch "
                                                f"restarted {(ctx.now - ref) / 60:.1f} min ago)"
                                                + (f"; last error: {self.last_error}" if self.last_error else ""))
        ref = self.last_success if self.last_success is not None else self.started
        if ref is None:
            return Verdict(None)
        down_for = ctx.now - ref
        active = down_for > self.after
        if self.last_success is None:
            detail = f"no successful poll since labwatch started {down_for / 60:.1f} min ago"
        else:
            detail = f"last successful poll {down_for / 60:.1f} min ago"
        if active and self.last_error:
            detail += f"; last error: {self.last_error}"
        return Verdict(active, down_for, detail)


def _as_float_set(v: Any) -> set[float]:
    if v is None:
        return set()
    if isinstance(v, (list, tuple, set)):
        return {float(x) for x in v}
    return {float(v)}


# ---------------------------------------------------------------------------
# Building rules from config
# ---------------------------------------------------------------------------

_OPTION_KEYS = {
    "severity", "for", "clear_for", "auto_arm", "arm_after", "repeat_interval", "message", "detail_channel",
    "on_invalid",
}
_KIND_KEYS = {
    "threshold": {"above", "below", "hysteresis"},
    "rate": {"rate_above", "rate_below", "window"},
    "state": {"equals", "not_equals"},
    "max_age": {"max_age"},
}
_TRIGGER_KEYS = {
    "threshold": {"above", "below"},
    "rate": {"rate_above", "rate_below"},
    "state": {"equals", "not_equals"},
    "max_age": {"max_age"},
}


def build_options(cfg: dict[str, Any], defaults: dict[str, Any]) -> RuleOptions:
    merged = {**defaults, **{k: v for k, v in cfg.items() if k in _OPTION_KEYS}}
    o = RuleOptions()
    if "severity" in merged:
        o.severity = Severity.parse(merged["severity"])
    if "for" in merged:
        o.for_s = parse_duration(merged["for"])
    if "clear_for" in merged:
        o.clear_for = parse_duration(merged["clear_for"])
    if "auto_arm" in merged:
        o.auto_arm = bool(merged["auto_arm"])
    if "arm_after" in merged:
        o.arm_after = parse_duration(merged["arm_after"])
    if "repeat_interval" in merged:
        o.repeat_interval = parse_duration(merged["repeat_interval"])
    o.on_invalid = str(merged.get("on_invalid", "ignore")).lower()
    if o.on_invalid not in ("ignore", "alarm"):
        raise ValueError(f"on_invalid must be 'ignore' or 'alarm', got {o.on_invalid!r}")
    o.message = str(merged.get("message") or "")
    o.detail_channel = merged.get("detail_channel")
    return o


def build_rule(device: str, cfg: dict[str, Any], defaults: dict[str, Any], channel_specs: dict[str, Any]) -> Rule:
    where = f"device {device!r}, rule {cfg.get('name') or cfg.get('channel')!r}"
    channel = cfg.get("channel")
    if not channel:
        raise RuleConfigError(f"{where}: missing 'channel'")
    if channel not in channel_specs:
        known = ", ".join(sorted(channel_specs))
        raise RuleConfigError(f"{where}: unknown channel {channel!r} (known: {known})")
    kinds = [k for k, keys in _TRIGGER_KEYS.items() if keys & cfg.keys()]
    if len(kinds) != 1:
        raise RuleConfigError(
            f"{where}: need exactly one condition type (above/below, rate_above/rate_below, "
            f"equals/not_equals or max_age), found {kinds or 'none'}"
        )
    kind = kinds[0]
    unknown = set(cfg) - _OPTION_KEYS - _KIND_KEYS[kind] - {"channel", "name"}
    if unknown:
        raise RuleConfigError(f"{where}: unknown keys {sorted(unknown)}")
    spec = channel_specs[channel]
    unit = getattr(spec, "unit", None)
    name = cfg.get("name")
    try:
        opts = build_options(cfg, defaults)
        if kind == "threshold":
            return ThresholdRule(device, channel, name, opts, cfg.get("above"), cfg.get("below"),
                                 cfg.get("hysteresis", 0.0), unit)
        if kind == "rate":
            return RateRule(device, channel, name, opts, cfg.get("rate_above"), cfg.get("rate_below"),
                            parse_duration(cfg.get("window", 300)), unit)
        if kind == "state":
            return StateRule(device, channel, name, opts, cfg.get("equals"), cfg.get("not_equals"),
                             dict(getattr(spec, "labels", ()) or ()))
        return MaxAgeRule(device, channel, name, opts, parse_duration(cfg["max_age"]))
    except (TypeError, ValueError) as e:
        raise RuleConfigError(f"{where}: {e}") from None


def build_rules(device: str, cfgs: Iterable[dict[str, Any]], defaults: dict[str, Any], channel_specs) -> list[Rule]:
    rules = [build_rule(device, c, defaults, channel_specs) for c in cfgs or []]
    seen: set[str] = set()
    for r in rules:
        if r.rule_id in seen:
            raise RuleConfigError(f"duplicate rule {r.rule_id!r}; give one of them a distinct 'name'")
        seen.add(r.rule_id)
    return rules
