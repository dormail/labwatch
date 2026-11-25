"""Anomaly detection (``deviation`` rules) end to end: a cold fridge on a fake clock, the
steady-state gate that keeps cooldowns and warm-ups out of the baseline, and restarts."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone

import pytest

from labwatch import engine
from labwatch.cli import main
from labwatch.config import Config, Section
from labwatch.devices.base import Device, DeviceUnreachable, parse_channels, register
from labwatch.devices.bluefors import BLUEFORS_STEADY_STATE, DEFAULT_CHANNELS
from labwatch.engine import Monitor, build_runtime
from labwatch.models import AlertEvent, EventKind, Reading, Severity
from labwatch.notifiers.base import Notifier
from labwatch.rules import Context, DeviationRule, History, RuleConfigError, RuleOptions, build_rules
from labwatch.state import StateStore

T0 = 1_800_000_000.0
H = 3600.0
DT = 300.0  # one poll every 5 min of fake time keeps multi-day scenarios fast
FIRST = T0 + DT  # time of the first poll (and first sample)

CHANNELS = {k: DEFAULT_CHANNELS[k] for k in ("T4K", "Tstill", "pulse_tube", "cpa_helium", "cpa_high_pressure")}
COLD = {"T4K": 3.2, "Tstill": 0.85, "pulse_tube": 1.0, "cpa_helium": 310.0, "cpa_high_pressure": 19.0}
WARMING = {"T4K": 6.0, "Tstill": 1.3}
DEFAULTS = {"arm_after": "10m", "clear_for": "10m", "repeat_interval": "2h"}
HELIUM = {"channel": "cpa_helium", "deviation": 5, "for": "15m"}
HELIUM_ID = "Fridge/cpa_helium unusual (more than 5 K from normal)"
ARMED_ID = "Fridge/armed"
FIRING, RESOLVED, REMINDER, INFO = EventKind.FIRING, EventKind.RESOLVED, EventKind.REMINDER, EventKind.INFO


def wobble(base: float, amp: float):
    """base-amp, base, base+amp, ... one step per poll: the median is ``base`` and sigma = 1.4826 * amp."""
    return lambda t: base + amp * (-1, 0, 1)[round(t / DT) % 3]


def settled() -> dict:
    return dict(COLD, cpa_helium=wobble(310.0, 0.5))


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def time(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t


@register("lwtest_anomaly_fridge")
class Fridge(Device):
    """A fridge whose values the test sets (numbers, None = invalid, or functions of time)."""

    summary_channels = ("T4K", "Tstill", "cpa_helium")
    default_steady_state = BLUEFORS_STEADY_STATE

    def __init__(self, name, channels, clock) -> None:
        super().__init__(name, channels, poll_interval=DT, unreachable_after=900.0)
        self.clock = clock
        self.values: dict = {}
        self.error: Exception | None = None

    @classmethod
    def from_config(cls, name, cfg):
        return cls(name, parse_channels(name, cfg, CHANNELS), cfg["clock"])

    async def poll(self) -> list[Reading]:
        if self.error is not None:
            raise self.error
        t = self.clock.t
        ts = datetime.fromtimestamp(t, timezone.utc)
        out = []
        for ch, v in self.values.items():
            if ch not in self.channels:
                continue
            v = v(t) if callable(v) else v
            spec = self.channels[ch]
            out.append(Reading(self.name, ch, spec.source, v, ts, spec.unit, raw=None if v is None else str(v),
                               status="OK" if v is not None else "INVALID", label=spec.label_for(v)))
        return out


class Inbox(Notifier):
    def __init__(self) -> None:
        super().__init__("inbox", Severity.INFO)
        self.events: list[AlertEvent] = []

    async def send(self, event: AlertEvent) -> None:
        self.events.append(event)

    @classmethod
    def from_config(cls, name, min_severity, cfg):
        return cls()


class Harness:
    def __init__(self, tmp_path, clock: FakeClock, rules=(HELIUM,), store: StateStore | None = None,
                 defaults=None, **device_cfg) -> None:
        self.clock = clock
        self.config = Config(
            path=tmp_path / "config.yaml", state_db=tmp_path / "state.sqlite", history_days=30,
            rule_defaults=dict(DEFAULTS if defaults is None else defaults), devices=[], notifiers=[],
            heartbeat_url=None, heartbeat_interval=60.0, daily_summary=None, notify_startup=False, secrets=set(),
        )
        sec = Section({"name": "Fridge", "type": "lwtest_anomaly_fridge", "clock": clock, "rules": list(rules),
                       **device_cfg}, set())
        self.config.devices.append(sec)
        self.rt = build_runtime(self.config, sec)
        self.dev: Fridge = self.rt.device
        self.store = store if store is not None else StateStore(":memory:")
        self.inbox = Inbox()
        self.monitor = Monitor(self.config, [self.rt], [self.inbox], self.store)
        self.rt.unreachable.started = clock.t  # what Monitor.run() does

    def rule(self, rule_id: str):
        return next(r for r in self.rt.all_rules if r.rule_id == rule_id)

    def ctx(self) -> Context:
        return self.rt.context(self.clock.t)

    def poll(self, dt: float = DT) -> list[AlertEvent]:
        self.clock.t += dt
        n = len(self.inbox.events)

        async def go():
            await self.monitor.poll_once(self.rt)
            while not self.monitor._queue.empty():
                await self.monitor.dispatch(self.monitor._queue.get_nowait())

        asyncio.run(go())
        return self.inbox.events[n:]

    def run_for(self, seconds: float, dt: float = DT) -> list[AlertEvent]:
        events: list[AlertEvent] = []
        for _ in range(round(seconds / dt)):
            events += self.poll(dt)
        return events

    def run_until(self, t: float) -> list[AlertEvent]:
        """Poll every DT up to and including time ``t``."""
        events: list[AlertEvent] = []
        while self.clock.t + DT <= t:
            events += self.poll()
        return events

    def poll_until(self, pred, limit: float = 24 * H) -> list[AlertEvent]:
        events: list[AlertEvent] = []
        start = self.clock.t
        while self.clock.t - start < limit:
            events += self.poll()
            if any(pred(e) for e in events):
                return events
        raise AssertionError(f"no matching event within {limit / H:g} h; got {kinds(events)}")

    def settle_and_arm(self, values: dict | None = None) -> None:
        """Cold from the first poll: steady after 6 h, baseline learned 7 h later, armed after 10 min."""
        self.dev.values = settled() if values is None else values
        events = self.poll_until(lambda e: e.rule_id == ARMED_ID)
        assert kinds(events) == [(INFO, ARMED_ID)]


def kinds(events) -> list[tuple[EventKind, str]]:
    return [(e.kind, e.rule_id) for e in events]


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    c = FakeClock()
    monkeypatch.setattr(engine, "time", c)
    return c


@pytest.fixture
def h(tmp_path, clock) -> Harness:
    return Harness(tmp_path, clock)


# ---------------------------------------------------------------------------
# DeviationRule: config
# ---------------------------------------------------------------------------


def test_deviation_rule_from_config_defaults():
    (rule,) = build_rules("Fridge", [{"channel": "cpa_helium", "deviation": 5}], {}, DEFAULT_CHANNELS)
    assert isinstance(rule, DeviationRule)
    assert (rule.relative, rule.deviation, rule.unit) == (False, 5.0, "K")
    assert (rule.baseline, rule.exclude_recent, rule.noise_factor, rule.min_baseline) == (24 * H, H, 4.0, 6 * H)
    assert rule.horizon == 25 * H
    assert rule.opts.steady_only  # anomaly rules learn and alert only in the steady state by default
    assert rule.name == "cpa_helium unusual (more than 5 K from normal)"
    assert rule.rule_id == "Fridge/cpa_helium unusual (more than 5 K from normal)"


def test_deviation_rule_percent_and_explicit_options():
    cfg = {"channel": "cpa_high_pressure", "deviation": "5%", "baseline": "12h", "exclude_recent": "30m",
           "noise_factor": 3, "min_baseline": "2h", "steady_only": False}
    (rule,) = build_rules("Fridge", [cfg], {"steady_only": True}, DEFAULT_CHANNELS)
    assert (rule.relative, rule.deviation) == (True, pytest.approx(0.05))
    assert (rule.baseline, rule.exclude_recent, rule.noise_factor, rule.min_baseline) == (12 * H, 1800, 3.0, 2 * H)
    assert rule.horizon == 12.5 * H
    assert rule.opts.steady_only is False
    assert rule.name == "cpa_high_pressure unusual (more than 5% from normal)"
    (rule,) = build_rules("Fridge", [{"channel": "cpa_oil", "deviation": " 2.5 "}], {}, DEFAULT_CHANNELS)
    assert rule.describe() == "unusual (more than 2.5 K from normal)"
    (rule,) = build_rules("Fridge", [{"channel": "cpa_oil", "deviation": 4, "baseline": "8h"}], {}, DEFAULT_CHANNELS)
    assert rule.min_baseline == 2 * H  # baseline / 4


@pytest.mark.parametrize(
    "cfg, match",
    [
        ({"deviation": 0}, "must be positive"),
        ({"deviation": -1}, "must be positive"),
        ({"deviation": "0%"}, "must be positive"),
        ({"deviation": "lots"}, "could not convert"),
        ({"deviation": 5, "window": "1h"}, "unknown keys"),
        ({"deviation": 5, "above": 300}, "exactly one condition type"),
        ({"baseline": "24h"}, "exactly one condition type"),
    ],
)
def test_deviation_rule_bad_config(cfg, match):
    with pytest.raises(RuleConfigError, match=match):
        build_rules("Fridge", [{"channel": "cpa_helium", **cfg}], {}, DEFAULT_CHANNELS)


# ---------------------------------------------------------------------------
# DeviationRule.check (direct)
# ---------------------------------------------------------------------------


def context(samples, now, value, steady_ready_at=None, unit="K") -> Context:
    hist = History(60 * H)
    for t, v in samples:
        hist.add(Reading("Fridge", "cpa_helium", "src", v, datetime.fromtimestamp(t, timezone.utc), unit), t)
    r = Reading("Fridge", "cpa_helium", "src", value, datetime.fromtimestamp(now, timezone.utc), unit)
    return Context(now=now, readings={"cpa_helium": r}, history=hist, steady_ready=True,
                   steady_ready_at=steady_ready_at)


def series(start, stop, f, step=DT):
    out, t = [], start
    while t <= stop:
        out.append((t, f(t) if callable(f) else f))
        t += step
    return out


def deviation(dev=5, steady_only=True, unit="K", **kw) -> DeviationRule:
    return DeviationRule("Fridge", "cpa_helium", None, RuleOptions(steady_only=steady_only), dev, unit=unit, **kw)


def test_check_needs_20_samples_and_min_baseline():
    now = T0 + 30 * H
    rule = deviation()
    few = series(now - 20 * H, now - H, 310.0, step=H)  # 20 h but only 20 samples ... minus one
    v = rule.check(context(few[1:], now, 310.0))
    assert v.active is None and v.detail == "learning what's normal: 18.0 h of 6 h collected"
    assert rule.check(context(few, now, 310.0)).active is False  # 20 samples: enough
    short = series(now - 6 * H, now - H, 310.0)  # 5 h of data older than exclude_recent
    v = rule.check(context(short, now, 330.0))
    assert v.active is None and v.detail == "learning what's normal: 5.0 h of 6 h collected"
    v = rule.check(context(series(now - 7 * H, now - H, 310.0), now, 330.0))
    assert v.active is True


def test_check_ignores_the_most_recent_samples_and_uses_the_median():
    now = T0 + 30 * H
    rule = deviation(baseline=2 * H, exclude_recent=H, min_baseline=H)
    samples = series(now - 3 * H, now - H, wobble(100.0, 0.5)) + series(now - H + DT, now, 200.0)
    v = rule.check(context(samples, now, 200.0))
    assert v.active is True
    assert v.value == 200.0
    assert v.detail == "cpa_helium = 200 K, normally 100 K (±5 K, learned over 2 h)"
    # the same data with exclude_recent 0: the last hour shifts what is "normal"
    v = deviation(baseline=2 * H, exclude_recent=0, min_baseline=H).check(context(samples, now, 200.0))
    assert "normally 100 K" not in v.detail


def test_check_learns_only_from_data_after_steady_ready_at():
    now = T0 + 30 * H
    ready_at = now - 10 * H
    samples = series(now - 25 * H, now - H, lambda t: 150.0 if t < ready_at else 100.0)
    v = deviation().check(context(samples, now, 101.0, steady_ready_at=ready_at))
    assert v.active is False and "normally 100 K" in v.detail and "learned over 9 h" in v.detail
    # without steady_only the whole baseline (mostly the 150 K "cooldown") counts
    v = deviation(steady_only=False).check(context(samples, now, 101.0, steady_ready_at=ready_at))
    assert v.active is True and "normally 150 K" in v.detail


def test_allowed_deviation_is_the_larger_of_deviation_and_noise():
    now = T0 + 30 * H
    samples = series(now - 25 * H, now - H, wobble(310.0, 2.0))  # MAD 2 K -> sigma 2.97 K, 4 sigma 11.86 K
    rule = deviation()
    assert rule.check(context(samples, now, 321.0)).active is False
    v = rule.check(context(samples, now, 322.0))
    assert v.active is True and "(±11.86 K," in v.detail
    rule = deviation(noise_factor=1)
    v = rule.check(context(samples, now, 316.0))
    assert v.active is True and "(±5 K," in v.detail
    assert deviation(dev=20).check(context(samples, now, 322.0)).active is False


def test_percent_deviation_uses_the_magnitude_of_normal():
    now = T0 + 30 * H
    samples = series(now - 25 * H, now - H, -50.0)
    rule = deviation(dev="10%", unit=None)
    assert rule.check(context(samples, now, -54.0, unit=None)).active is False
    v = rule.check(context(samples, now, -56.0, unit=None))
    assert v.active is True and v.detail == "cpa_helium = -56, normally -50 (±5, learned over 24 h)"


def test_check_without_a_value_is_no_data():
    now = T0 + 30 * H
    samples = series(now - 25 * H, now - H, 310.0)
    c = context(samples, now, None)
    assert deviation().check(c).active is None
    c.readings.clear()
    assert deviation().check(c).active is None


# ---------------------------------------------------------------------------
# 1. the life of an anomaly rule on a cold fridge
# ---------------------------------------------------------------------------


def test_anomaly_waits_for_the_gate_and_a_settled_baseline_then_fires_and_resolves(h):
    base = wobble(310.0, 0.5)
    # a big excursion while the fridge is still settling: never alerts, never learned
    h.dev.values = dict(COLD, cpa_helium=lambda t: 340.0 if FIRST + 2 * H <= t < FIRST + 4 * H else base(t))
    rule = h.rule(HELIUM_ID)

    # the gate: T4K < 4.5 K and Tstill < 1.5 K for 6 h
    assert h.run_until(FIRST + 6 * H - DT) == []
    c = h.ctx()
    assert (c.steady_ready, c.steady_detail) == (False, "settling: steady for 5.9 h of 6 h")
    assert rule.status_text() == "disarmed"
    assert h.poll() == []
    c = h.ctx()
    assert c.steady_ready and c.steady_ready_at == FIRST + 6 * H

    # learning: min_baseline (24 h / 4 = 6 h) of settled data that is older than exclude_recent (1 h)
    v = rule.check(c)
    assert v.active is None and v.detail == "learning what's normal: 0.0 h of 6 h collected"
    assert h.run_until(FIRST + 13 * H - DT) == []
    assert rule.check(h.ctx()).detail == "learning what's normal: 5.9 h of 6 h collected"
    assert rule.status_text() == "disarmed"

    # first verdict at 13 h; arms arm_after (10 min) later
    events = h.poll_until(lambda e: e.rule_id == ARMED_ID)
    assert h.clock.t == FIRST + 13 * H + 600
    assert kinds(events) == [(INFO, ARMED_ID)]
    assert "- cpa_helium unusual (more than 5 K from normal)" in events[0].message
    assert rule.status_text() == "armed"

    # a step change bigger than the allowed deviation fires after `for` (15 min)
    h.dev.values["cpa_helium"] = 330.0
    assert h.run_for(3 * DT) == []
    assert rule.status_text() == "pending"
    (ev,) = h.poll()
    assert (ev.kind, ev.rule_id, ev.severity) == (FIRING, HELIUM_ID, Severity.WARNING)
    assert ev.title == "Fridge: cpa_helium unusual (more than 5 K from normal)"
    assert ev.message.startswith("cpa_helium = 330 K, normally 310 K (±5 K, learned over ")  # 340 K not learned
    assert (ev.value, ev.unit, ev.channel) == (330.0, "K", "cpa_helium")

    # back to normal: resolved after clear_for (10 min), then quiet
    h.dev.values["cpa_helium"] = base
    assert h.run_for(2 * DT) == []
    (res,) = h.poll()
    assert (res.kind, res.rule_id) == (RESOLVED, HELIUM_ID)
    assert res.message.startswith("Back to normal: cpa_helium = 3")
    assert h.run_for(12 * H) == []


def test_noise_within_four_sigma_does_not_fire(h):
    h.settle_and_arm(dict(COLD, cpa_helium=wobble(310.0, 2.0)))  # sigma 2.97 K: allowed 11.86 K > 5 K
    h.dev.values["cpa_helium"] = 318.0  # 8 K off: more than `deviation`, but within the noise
    assert h.run_for(45 * 60) == []
    assert "normally 310 K (±11.86 K" in h.rule(HELIUM_ID).check(h.ctx()).detail
    h.dev.values["cpa_helium"] = 325.0
    events = h.run_for(4 * DT)
    assert kinds(events) == [(FIRING, HELIUM_ID)]
    assert "cpa_helium = 325 K, normally 310 K (±11.86 K" in events[0].message


def test_percent_deviation_end_to_end(tmp_path, clock):
    rule_cfg = {"channel": "cpa_high_pressure", "deviation": "5%", "for": "15m"}
    h = Harness(tmp_path, clock, rules=[rule_cfg])
    h.settle_and_arm(dict(COLD))
    rid = "Fridge/cpa_high_pressure unusual (more than 5% from normal)"
    h.dev.values["cpa_high_pressure"] = 19.8  # +4.2 %
    assert h.run_for(45 * 60) == []
    h.dev.values["cpa_high_pressure"] = 18.0  # -5.3 %
    events = h.run_for(4 * DT)
    assert kinds(events) == [(FIRING, rid)]
    assert events[0].message.startswith("cpa_high_pressure = 18, normally 19 (±0.95, learned over")


def test_exclude_recent_keeps_a_slow_drift_from_teaching_itself(tmp_path, clock):
    common = {"channel": "cpa_helium", "deviation": 1, "baseline": "2h", "min_baseline": "1h", "for": "15m"}
    rules = [dict(common, exclude_recent="3h", name="helium vs 3-5 h ago"),
             dict(common, exclude_recent=0, name="helium vs last 2 h")]
    h = Harness(tmp_path, clock, rules=rules, steady_state=None)  # no gate: just the baseline maths
    h.dev.values = dict(COLD)
    h.run_until(FIRST + 5 * H)
    assert [r.status_text() for r in h.rt.rules] == ["armed", "armed"]

    drift_start = h.clock.t
    h.dev.values["cpa_helium"] = lambda t: 310.0 + 0.5 * (t - drift_start) / H  # +0.5 K per hour
    events = h.poll_until(lambda e: e.kind is FIRING, limit=8 * H)
    assert kinds(events) == [(FIRING, "Fridge/helium vs 3-5 h ago")]
    assert h.clock.t - drift_start == 2 * H + 20 * 60  # > 1 K off after 2 h, plus `for`
    # the rule that learns from the last 2 h has followed the drift all along
    assert [e for e in h.run_for(8 * H) if e.rule_id == "Fridge/helium vs last 2 h"] == []
    assert h.rule("Fridge/helium vs last 2 h").status_text() == "armed"


# ---------------------------------------------------------------------------
# 2. cooldowns and warm-ups are excluded
# ---------------------------------------------------------------------------


def cooldown(t: float) -> dict:
    """Warm until 10 h, 50 K until 20 h, then cold; cpa_helium busy until 20 h, settling until 26 h."""
    rel = t - T0
    if rel < 10 * H:
        return {"T4K": 290.0, "Tstill": None, "cpa_helium": 330.0}
    if rel < 20 * H:
        return {"T4K": 50.0, "Tstill": None, "cpa_helium": 330.0}
    if rel < 26 * H:
        return {"T4K": 3.2, "Tstill": 0.85, "cpa_helium": 320.0}
    return {"T4K": 3.2, "Tstill": 0.85, "cpa_helium": wobble(310.0, 0.5)(t)}


def test_cooldown_samples_never_enter_the_baseline(tmp_path, clock):
    ungated = dict(HELIUM, steady_only=False, name="helium learning from everything")
    h = Harness(tmp_path, clock, rules=[HELIUM, ungated])
    h.dev.values = {ch: (lambda t, ch=ch: cooldown(t)[ch]) for ch in ("T4K", "Tstill", "cpa_helium")}
    rule = h.rule(HELIUM_ID)

    events = h.run_until(T0 + 5 * H)
    assert h.ctx().steady_detail == "T4K = 290 K (steady needs T4K < 4.5 K)"
    events += h.run_until(T0 + 15 * H)
    assert h.ctx().steady_detail == "T4K = 50 K (steady needs T4K < 4.5 K)"
    events += h.run_until(T0 + 23 * H)
    assert h.ctx().steady_detail == "settling: steady for 3.0 h of 6 h"
    events += h.run_until(T0 + 33 * H + 5 * 60)
    assert h.ctx().steady_ready_at == T0 + 26 * H
    assert rule.status_text() == "disarmed"  # still learning (6 h of settled data + 1 h)
    armed = h.poll()
    info = [e for e in armed if e.rule_id == ARMED_ID]
    assert [e.message.splitlines()[1] for e in info] == ["- cpa_helium unusual (more than 5 K from normal)"]
    events += armed + h.run_until(T0 + 40 * H)
    v = rule.check(h.ctx())
    assert v.active is False
    assert "normally 310 K (±5 K, learned over 13 h)" in v.detail  # only from 26 h on
    events += h.run_until(T0 + 60 * H)

    assert [e for e in events if e.rule_id == HELIUM_ID] == []  # no alert at the end of the cooldown
    assert rule.status_text() == "armed"
    # the same rule learning from the cooldown raises a false alarm when the compressor load drops
    assert (FIRING, "Fridge/helium learning from everything") in kinds(events)


def test_warmup_pauses_and_disarms_a_rule_that_is_not_firing(h):
    h.settle_and_arm()
    rule = h.rule(HELIUM_ID)
    h.dev.values.update(WARMING, cpa_helium=345.0)  # warm-up: the compressor load changes a lot
    assert h.run_for(4 * H) == []
    assert rule.status_text() == "disarmed"
    assert h.ctx().steady_detail == "T4K = 6 K (steady needs T4K < 4.5 K)"

    # cold again: the 6 h wait and the learning start over; the warm-up isn't learned
    h.dev.values = settled()
    cold_again = h.clock.t + DT
    events = h.poll_until(lambda e: e.rule_id == ARMED_ID)
    assert kinds(events) == [(INFO, ARMED_ID)]
    assert h.clock.t == cold_again + 13 * H + 600
    assert h.ctx().steady_ready_at == cold_again + 6 * H
    assert "normally 310 K" in rule.check(h.ctx()).detail
    assert h.run_for(12 * H) == []


def test_firing_alert_survives_a_warmup_and_resolves_once_steady_and_normal(h):
    h.settle_and_arm()
    rule = h.rule(HELIUM_ID)
    h.dev.values["cpa_helium"] = 330.0
    assert kinds(h.run_for(4 * DT)) == [(FIRING, HELIUM_ID)]

    # warm-up; meanwhile the value is back to normal and reminders (2 h) would be due
    h.dev.values.update(WARMING, cpa_helium=wobble(310.0, 0.5))
    assert h.run_for(5 * H) == []  # no RESOLVED, no REMINDER while paused
    assert rule.state.firing and rule.state.delivered and rule.status_text() == "FIRING"
    assert not h.ctx().steady_ready

    # cold again: re-evaluated only once steady (6 h) and learned (+7 h), then it resolves
    h.dev.values.update(T4K=3.2, Tstill=0.85)
    cold_again = h.clock.t + DT
    events = h.poll_until(lambda e: e.rule_id == HELIUM_ID)
    assert kinds(events) == [(RESOLVED, HELIUM_ID)]
    assert h.clock.t == cold_again + 13 * H + 600
    assert "normally 310 K" in events[0].message
    assert not rule.state.firing and rule.state.armed
    assert h.run_for(6 * H) == []


# ---------------------------------------------------------------------------
# 3. restarts
# ---------------------------------------------------------------------------


def restart(tmp_path, clock, old: Harness, down_for: float = 600.0, **kw) -> Harness:
    old.store.close()
    clock.t += down_for
    new = Harness(tmp_path, clock, store=StateStore(tmp_path / "state.sqlite"), **kw)
    new.monitor._restore_state()
    new.dev.values = old.dev.values
    return new


def test_restart_keeps_the_steady_state_and_the_armed_rule(tmp_path, clock):
    h1 = Harness(tmp_path, clock, store=StateStore(tmp_path / "state.sqlite"))
    h1.settle_and_arm()
    ready_at = h1.ctx().steady_ready_at
    assert ready_at == FIRST + 6 * H
    assert h1.run_for(H) == []

    h2 = restart(tmp_path, clock, h1)
    assert len(h2.rt.history.points("T4K")) > 150  # seeded from the stored history
    assert h2.poll() == []  # no "alarms armed" again
    c = h2.ctx()
    assert c.steady_ready and c.steady_ready_at == ready_at  # no new 6 h wait
    rule = h2.rule(HELIUM_ID)
    assert rule.state.armed
    assert rule.check(c).active is False  # the baseline is back too
    h2.dev.values["cpa_helium"] = 330.0
    assert kinds(h2.run_for(4 * DT)) == [(FIRING, HELIUM_ID)]


def test_restart_before_the_gate_opened_does_not_restart_the_wait(tmp_path, clock):
    h1 = Harness(tmp_path, clock, store=StateStore(tmp_path / "state.sqlite"))
    h1.dev.values = settled()
    h1.run_until(FIRST + 4 * H)
    h2 = restart(tmp_path, clock, h1, down_for=1200.0)
    h2.poll()
    assert h2.ctx().steady_detail.startswith("settling: steady for 4.")
    while h2.clock.t + DT < FIRST + 6 * H:
        h2.poll()
        assert not h2.ctx().steady_ready
    h2.poll()
    c = h2.ctx()
    assert c.steady_ready and c.steady_ready_at == FIRST + 6 * H  # not restart + 6 h


def test_restore_seeds_keep_worth_of_history(tmp_path, clock):
    store = StateStore(tmp_path / "state.sqlite")
    now = clock.t
    for j in range(1, 481):  # 40 h of polls, one every 5 min
        ts = now - j * DT
        when = datetime.fromtimestamp(ts, timezone.utc)
        store.save_readings([Reading("Fridge", "T4K", "x", 3.2, when, "K"),
                             Reading("Fridge", "cpa_helium", "x", 310.0, when, "K"),
                             Reading("Other", "T4K", "x", 99.0, when, "K")], ts)
    h = Harness(tmp_path, clock, store=store)
    assert h.rt.history.keep == 32 * H  # 24 h baseline + 1 h excluded + 6 h gate + 1 h margin
    h.monitor._restore_state()
    for ch in ("T4K", "cpa_helium"):
        pts = h.rt.history.points(ch)
        assert len(pts) == 384 and pts[0][0] == now - 384 * DT and pts[-1][0] == now - DT
    assert {v for _, v in h.rt.history.points("T4K")} == {3.2}  # not another device's data
    assert list(h.rt.history.points("Tstill")) == []


# ---------------------------------------------------------------------------
# bugs
# ---------------------------------------------------------------------------


def test_short_baseline_anomaly_rule_learns_and_fires(tmp_path, clock):
    h = Harness(tmp_path, clock, rules=[dict(HELIUM, baseline="4h")])
    h.dev.values = settled()
    h.run_for(24 * H)  # a day of steady operation, plenty for a 4 h baseline
    rule = h.rt.rules[0]
    v = rule.check(h.ctx())
    assert v.active is False, v.detail
    h.dev.values["cpa_helium"] = 330.0
    assert (FIRING, rule.rule_id) in kinds(h.run_for(H))


def test_default_baseline_covers_24_h_after_days_of_steady_operation(h):
    h.dev.values = settled()
    h.run_for(3 * 24 * H)
    detail = h.rule(HELIUM_ID).check(h.ctx()).detail
    assert float(re.search(r"learned over (\d+) h", detail).group(1)) >= 23, detail


def test_unreachable_fires_while_the_gate_is_closed(tmp_path, clock):
    h = Harness(tmp_path, clock)
    h.dev.values = dict(COLD, T4K=50.0)  # cooling down: not steady
    h.run_for(H)
    h.dev.error = DeviceUnreachable("Fridge: cannot connect to fridge-pc:49098 (ConnectError)")
    assert (FIRING, "Fridge/unreachable") in kinds(h.run_for(H))


def test_steady_only_rule_default_does_not_pause_the_unreachable_alert(tmp_path, clock):
    h = Harness(tmp_path, clock, defaults=dict(DEFAULTS, steady_only=True))
    h.dev.values = dict(COLD, T4K=50.0)  # cooling down: not steady
    h.run_for(H)
    h.dev.error = DeviceUnreachable("Fridge: cannot connect to fridge-pc:49098 (ConnectError)")
    assert (FIRING, "Fridge/unreachable") in kinds(h.run_for(H))


def test_alert_delivered_after_a_mute_during_a_warmup_says_what_is_wrong(h):
    h.settle_and_arm()
    h.store.mute("Fridge", h.clock.t + 3 * H, "maintenance")
    h.dev.values["cpa_helium"] = 330.0
    assert h.run_for(H) == []
    assert h.rule(HELIUM_ID).state.firing
    h.dev.values.update(WARMING)  # maintenance warm-up
    (ev,) = [e for e in h.run_for(3 * H) if e.rule_id == HELIUM_ID]
    assert ev.kind is FIRING
    assert ev.message.startswith("cpa_helium = 330 K, normally 310 K"), ev.message


# ---------------------------------------------------------------------------
# labwatch check
# ---------------------------------------------------------------------------

CHECK_CONFIG = """\
state_db: state.sqlite
devices:
  - name: Sim
    type: simulated
    rules:
      - {channel: cpa_helium, deviation: 5, name: helium anomaly}
      - {channel: pulse_tube, not_equals: 1, name: pulse tube off}
"""


def row(out: str, name: str) -> str:
    return next(line for line in out.splitlines() if line.startswith(name))


def test_check_shows_steady_only_rules_as_paused_until_steady(isolated_env, restore_logging, capsys):
    path = isolated_env / "config.yaml"
    path.write_text(CHECK_CONFIG, encoding="utf-8")
    assert main(["-c", str(path), "check"]) == 0
    out = capsys.readouterr().out
    assert "steady state: no - settling: steady for 0.0 h of 6 h" in out
    assert "paused" in row(out, "helium anomaly")
    assert "waits for steady state (settling: steady for 0.0 h of 6 h)" in row(out, "helium anomaly")
    assert "paused" not in row(out, "pulse tube off") and " ok " in row(out, "pulse tube off")

    # `labwatch run` recorded 8 h of a cold fridge: steady now, the anomaly rule is learning
    store = StateStore(isolated_env / "state.sqlite")
    now = time.time()
    for k in range(1, 97):
        ts = now - k * DT
        when = datetime.fromtimestamp(ts, timezone.utc)
        store.save_readings([Reading("Sim", "T4K", "x", 3.2, when, "K"),
                             Reading("Sim", "Tstill", "x", 0.85, when, "K")], ts)
    store.close()
    assert main(["-c", str(path), "check"]) == 0
    out = capsys.readouterr().out
    assert "steady state: yes" in out
    r = row(out, "helium anomaly")
    assert "paused" not in r and "no data" in r and "learning what's normal" in r
