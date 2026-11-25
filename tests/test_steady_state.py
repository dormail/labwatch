"""The steady-state gate: GateCondition, SteadyState, the gate at the top of Rule.step,
History.seed/points, Rule.horizon and how build_runtime configures gate and history."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from labwatch.config import Config, Section
from labwatch.devices.base import Device, parse_channels, register
from labwatch.devices.bluefors import BLUEFORS_STEADY_STATE, DEFAULT_CHANNELS
from labwatch.engine import build_runtime
from labwatch.models import Reading
from labwatch.rules import (
    Context,
    DeviationRule,
    GateCondition,
    History,
    MaxAgeRule,
    RateRule,
    RuleConfigError,
    RuleOptions,
    StateRule,
    SteadyState,
    ThresholdRule,
    Transition,
)

T0 = 1_800_000_000.0
H = 3600.0
DEV = "Fridge"
GATE_CHANNELS = {k: DEFAULT_CHANNELS[k] for k in ("T4K", "Tstill", "pulse_tube", "cpa_helium")}


def reading(channel: str, value: float | None, ts: float | None = None, unit: str | None = None) -> Reading:
    when = None if ts is None else datetime.fromtimestamp(ts, timezone.utc)
    return Reading(DEV, channel, f"src.{channel}", value, when, unit)


def fill(history: History, channel: str, samples) -> History:
    for t, v in samples:
        history.add(reading(channel, v, ts=t), t)
    return history


def every(start: float, stop: float, step: float = 300.0):
    t = start
    while t <= stop:
        yield t
        t += step


def fridge_gate(for_s: float = 6 * H) -> SteadyState:
    return SteadyState([GateCondition("T4K", below=4.5), GateCondition("Tstill", below=1.5)], for_s,
                       {"T4K": "K", "Tstill": "K"})


# ---------------------------------------------------------------------------
# GateCondition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "above, below, value, ok",
    [
        (None, 4.5, 3.2, True),
        (None, 4.5, 4.5, False),  # strict: exactly at the limit is not "below"
        (None, 4.5, 4.6, False),
        (1.0, None, 1.0, False),
        (1.0, None, 1.1, True),
        (1.0, 2.0, 1.5, True),
        (1.0, 2.0, 2.5, False),
        (1.0, 2.0, 0.5, False),
    ],
)
def test_gate_condition_ok(above, below, value, ok):
    assert GateCondition("x", above=above, below=below).ok(value) is ok


def test_gate_condition_describe():
    assert GateCondition("T4K", below=4.5).describe("K") == "T4K < 4.5 K"
    assert GateCondition("flow", above=0.1).describe() == "flow > 0.1"
    assert GateCondition("P", above=1, below=2).describe("mbar") == "P < 2 mbar and > 1 mbar"


# ---------------------------------------------------------------------------
# SteadyState.from_config
# ---------------------------------------------------------------------------


def test_from_config_parses_conditions_units_and_default_for():
    gate = SteadyState.from_config(DEV, BLUEFORS_STEADY_STATE, DEFAULT_CHANNELS)
    assert [(c.channel, c.above, c.below) for c in gate.conditions] == [("T4K", None, 4.5), ("Tstill", None, 1.5)]
    assert gate.for_s == 6 * H
    assert gate.units == {"T4K": "K", "Tstill": "K"}
    gate = SteadyState.from_config(DEV, {"conditions": [{"channel": "T4K", "below": "4"}]}, DEFAULT_CHANNELS)
    assert gate.for_s == 6 * H  # 'for' defaults to 6 h
    assert gate.conditions[0].below == 4.0
    gate = SteadyState.from_config(DEV, {"conditions": [{"channel": "flow", "above": 0.1}], "for": "90m"},
                                   DEFAULT_CHANNELS)
    assert gate.for_s == 5400 and gate.conditions[0].above == pytest.approx(0.1)


@pytest.mark.parametrize("cfg", [None, {}, False])
def test_from_config_empty_means_no_gate(cfg):
    assert SteadyState.from_config(DEV, cfg, DEFAULT_CHANNELS) is None


@pytest.mark.parametrize(
    "cfg, match",
    [
        ({"for": "6h"}, "needs 'conditions"),
        ({"conditions": []}, "needs 'conditions"),
        ({"conditions": {"channel": "T4K", "below": 4.5}}, "needs 'conditions"),
        ("T4K < 4.5", "needs 'conditions"),
        ({"conditions": [{"channel": "T5K", "below": 4.5}]}, "unknown channel"),
        ({"conditions": ["T4K"]}, "unknown channel"),
        ({"conditions": [{"channel": "T4K"}]}, "needs 'above' and/or 'below'"),
        ({"conditions": [{"channel": "T4K", "under": 4.5}]}, "needs 'above' and/or 'below'"),
        ({"conditions": [{"channel": "T4K", "below": 4.5, "for": "1h"}]}, "needs 'above' and/or 'below'"),
    ],
)
def test_from_config_rejects_bad_config(cfg, match):
    with pytest.raises(RuleConfigError, match=match):
        SteadyState.from_config(DEV, cfg, DEFAULT_CHANNELS)


# ---------------------------------------------------------------------------
# SteadyState.evaluate
# ---------------------------------------------------------------------------


def test_missing_or_invalid_gate_channel_is_not_steady():
    gate = fridge_gate()
    hist = History(48 * H)
    now = T0
    assert gate.evaluate({"T4K": reading("T4K", 3.2)}, hist, now) == (False, None, "Tstill unavailable")
    readings = {"T4K": reading("T4K", 3.2), "Tstill": reading("Tstill", None)}
    assert gate.evaluate(readings, hist, now) == (False, None, "Tstill unavailable")


def test_value_outside_condition_is_not_steady_and_says_why():
    gate = fridge_gate()
    readings = {"T4K": reading("T4K", 6.0, unit="K"), "Tstill": reading("Tstill", 0.8, unit="K")}
    assert gate.evaluate(readings, History(), T0) == (False, None, "T4K = 6 K (steady needs T4K < 4.5 K)")


def test_no_history_counts_from_now():
    gate = fridge_gate()
    readings = {"T4K": reading("T4K", 3.2), "Tstill": reading("Tstill", 0.8)}
    assert gate.evaluate(readings, History(), T0) == (False, None, "settling: steady for 0.0 h of 6 h")


def test_settling_until_for_has_passed_then_ready_at_is_exact():
    gate = fridge_gate()
    hist = History(48 * H)
    cold_since = T0
    for t in every(cold_since, cold_since + 8 * H):
        fill(hist, "T4K", [(t, 3.2)])
        fill(hist, "Tstill", [(t, 0.8)])
    readings = {"T4K": reading("T4K", 3.2), "Tstill": reading("Tstill", 0.8)}
    assert gate.evaluate(readings, hist, cold_since + 3 * H) == (False, None, "settling: steady for 3.0 h of 6 h")
    assert gate.evaluate(readings, hist, cold_since + 6 * H - 1)[0] is False
    assert gate.evaluate(readings, hist, cold_since + 6 * H) == (True, cold_since + 6 * H, "")
    # ready_at stays the moment the device became settled, it doesn't move with "now"
    assert gate.evaluate(readings, hist, cold_since + 8 * H) == (True, cold_since + 6 * H, "")


def test_last_bad_sample_in_history_restarts_the_clock():
    gate = fridge_gate()
    hist = History(48 * H)
    samples = [(t, 3.2) for t in every(T0, T0 + 10 * H)]
    samples[60] = (samples[60][0], 5.0)  # one warm sample 5 h in (T0 + 5 h)
    fill(hist, "T4K", samples)
    fill(hist, "Tstill", [(t, 0.8) for t in every(T0, T0 + 10 * H)])
    readings = {"T4K": reading("T4K", 3.2), "Tstill": reading("Tstill", 0.8)}
    ready, ready_at, detail = gate.evaluate(readings, hist, T0 + 10 * H)
    assert not ready and ready_at is None
    assert detail == "settling: steady for 4.9 h of 6 h"  # counted from the first good sample after it
    assert gate.evaluate(readings, hist, T0 + 11 * H + 300) == (True, T0 + 5 * H + 300 + 6 * H, "")


def test_latest_condition_to_settle_decides():
    gate = fridge_gate()
    hist = History(48 * H)
    fill(hist, "T4K", [(t, 3.2) for t in every(T0, T0 + 12 * H)])  # cold for 12 h
    fill(hist, "Tstill", [(t, 2.0 if t < T0 + 4 * H else 0.8) for t in every(T0, T0 + 12 * H)])  # circulating 8 h
    readings = {"T4K": reading("T4K", 3.2), "Tstill": reading("Tstill", 0.8)}
    assert gate.evaluate(readings, hist, T0 + 12 * H) == (True, T0 + 4 * H + 6 * H, "")


# ---------------------------------------------------------------------------
# the gate in Rule.step
# ---------------------------------------------------------------------------


def opts(**kw) -> RuleOptions:
    base = dict(for_s=0.0, clear_for=60.0, auto_arm=True, arm_after=300.0, repeat_interval=600.0, steady_only=True)
    return RuleOptions(**{**base, **kw})


def ctx(now: float, value: float, steady: bool, detail: str = "T4K = 6 K (steady needs T4K < 4.5 K)") -> Context:
    r = reading("T4K", value, ts=now, unit="K")
    return Context(now=now, readings={"T4K": r}, history=History(), steady_ready=steady,
                   steady_ready_at=(now - H) if steady else None, steady_detail="" if steady else detail)


def counting(rule, monkeypatch) -> list[float]:
    calls: list[float] = []
    original = rule.check
    monkeypatch.setattr(rule, "check", lambda c: calls.append(c.now) or original(c))
    return calls


def test_steady_only_rule_is_paused_and_disarmed_while_not_steady(monkeypatch):
    rule = ThresholdRule(DEV, "T4K", None, opts(for_s=600.0), above=5.0, unit="K")
    assert rule.step(ctx(T0, 3.0, True))[1] is None
    assert rule.step(ctx(T0 + 300, 3.0, True))[1] == Transition.ARMED
    assert rule.step(ctx(T0 + 600, 6.0, True))[1] is None  # pending
    assert rule.state.pending_since == T0 + 600
    calls = counting(rule, monkeypatch)

    verdict, tr = rule.step(ctx(T0 + 900, 6.0, False))
    assert tr is None
    assert verdict.active is None and verdict.detail == "paused: T4K = 6 K (steady needs T4K < 4.5 K)"
    assert calls == []  # the condition isn't even looked at
    st = rule.state
    assert (st.armed, st.firing, st.pending_since, st.healthy_since) == (False, False, None, None)
    assert rule.status_text() == "disarmed"
    # back to steady: it has to arm again before it can fire
    assert rule.step(ctx(T0 + 1200, 6.0, True))[1] is None
    assert rule.step(ctx(T0 + 7200, 6.0, True))[1] is None
    assert not rule.state.firing


def test_paused_rule_with_auto_arm_false_stays_armed():
    rule = StateRule(DEV, "T4K", None, opts(auto_arm=False), not_equals=1)
    assert rule.state.armed
    rule.step(ctx(T0, 0.0, False))
    assert rule.state.armed and not rule.state.firing


def test_firing_rule_is_frozen_while_paused(monkeypatch):
    """A firing alert is never resolved, reminded or silenced by the gate."""
    rule = ThresholdRule(DEV, "T4K", None, opts(auto_arm=False, repeat_interval=600.0, clear_for=0.0),
                         above=5.0, unit="K")
    assert rule.step(ctx(T0, 6.0, True))[1] == Transition.FIRING
    rule.state.delivered = True  # what the engine does when it sends the alert
    before = rule.state.to_dict()
    calls = counting(rule, monkeypatch)
    for k in range(1, 50):  # 4 h of polls: the value is fine again, reminders would be due
        verdict, tr = rule.step(ctx(T0 + 300 * k, 3.0, False))
        assert tr is None and verdict.active is None
    assert calls == []
    assert rule.state.to_dict() == before
    assert rule.status_text() == "FIRING"
    # steady again: evaluated normally, and only now it resolves
    _, tr = rule.step(ctx(T0 + 300 * 50, 3.0, True))
    assert tr == Transition.RESOLVED


def test_firing_rule_that_is_still_bad_after_the_pause_reminds_then():
    rule = ThresholdRule(DEV, "T4K", None, opts(auto_arm=False, repeat_interval=600.0), above=5.0, unit="K")
    assert rule.step(ctx(T0, 6.0, True))[1] == Transition.FIRING
    for k in range(1, 10):
        assert rule.step(ctx(T0 + 300 * k, 6.0, False))[1] is None
    assert rule.step(ctx(T0 + 3000, 6.0, True))[1] == Transition.REMINDER


def test_rule_without_steady_only_ignores_the_gate():
    rule = ThresholdRule(DEV, "T4K", None, opts(steady_only=False, auto_arm=False), above=5.0, unit="K")
    verdict, tr = rule.step(ctx(T0, 6.0, False))
    assert tr == Transition.FIRING and verdict.active is True


# ---------------------------------------------------------------------------
# History.seed / points, Rule.horizon
# ---------------------------------------------------------------------------


def test_points_of_unknown_channel_is_empty_and_not_created():
    hist = History()
    assert list(hist.points("nope")) == []
    assert "nope" not in hist._data


def test_seed_appends_in_order_and_skips_duplicates():
    hist = History(10 * H)
    hist.seed("T4K", [(T0, 3.0), (T0, 3.0), (T0 + 30, 3.1), (T0 + 20, 9.9), (T0 + 60, 3.2)])
    assert list(hist.points("T4K")) == [(T0, 3.0), (T0 + 30, 3.1), (T0 + 60, 3.2)]
    # later live samples continue the series; a repeated controller timestamp is skipped
    hist.add(reading("T4K", 3.3, ts=T0 + 60), T0 + 90)
    hist.add(reading("T4K", 3.4, ts=T0 + 90), T0 + 90)
    assert list(hist.points("T4K"))[-1] == (T0 + 90, 3.4)
    assert len(hist.points("T4K")) == 4


def test_seed_after_live_data_skips_older_samples():
    hist = History(10 * H)
    hist.add(reading("T4K", 3.5, ts=T0 + 100), T0 + 100)
    hist.seed("T4K", [(T0, 3.0), (T0 + 50, 3.1)])
    assert list(hist.points("T4K")) == [(T0 + 100, 3.5)]


def test_add_trims_to_keep():
    hist = History(keep_seconds=H)
    for t in every(T0, T0 + 3 * H):
        hist.add(reading("T4K", 3.2, ts=t), t)
    pts = hist.points("T4K")
    assert pts[0][0] == T0 + 2 * H and pts[-1][0] == T0 + 3 * H


def test_rule_horizons():
    o = RuleOptions()
    assert ThresholdRule(DEV, "T4K", None, o, above=5).horizon == 0
    assert StateRule(DEV, "pulse_tube", None, o, not_equals=1).horizon == 0
    assert MaxAgeRule(DEV, "T4K", None, o, max_age=900).horizon == 0
    assert RateRule(DEV, "T4K", None, o, rate_above=0.05, window=600).horizon == 600
    assert DeviationRule(DEV, "cpa_helium", None, o, 5).horizon == 25 * H
    assert DeviationRule(DEV, "cpa_helium", None, o, 5, baseline=4 * H, exclude_recent=1800).horizon == 4.5 * H


# ---------------------------------------------------------------------------
# build_runtime: which gate, how much history
# ---------------------------------------------------------------------------


@register("lwtest_gate_device")
class GateDevice(Device):
    default_steady_state = BLUEFORS_STEADY_STATE

    @classmethod
    def from_config(cls, name, cfg):
        return cls(name, parse_channels(name, cfg, GATE_CHANNELS))

    async def poll(self):
        return []


@register("lwtest_nogate_device")
class NoGateDevice(GateDevice):
    default_steady_state = None


def config(tmp_path) -> Config:
    return Config(path=tmp_path / "config.yaml", state_db=tmp_path / "s.sqlite", history_days=30, rule_defaults={},
                  devices=[], notifiers=[], heartbeat_url=None, heartbeat_interval=60.0, daily_summary=None,
                  notify_startup=False, secrets=set())


def runtime(tmp_path, type_: str = "lwtest_gate_device", **cfg):
    return build_runtime(config(tmp_path), Section({"name": DEV, "type": type_, **cfg}, set()))


def conds(rt) -> list[tuple[str, float | None, float | None]]:
    return [(c.channel, c.above, c.below) for c in rt.steady.conditions]


def test_default_gate_when_device_has_all_channels(tmp_path):
    rt = runtime(tmp_path)
    assert conds(rt) == [("T4K", None, 4.5), ("Tstill", None, 1.5)]
    assert rt.steady.for_s == 6 * H


def test_default_gate_keeps_only_conditions_for_existing_channels(tmp_path):
    rt = runtime(tmp_path, exclude_channels=["Tstill"])
    assert conds(rt) == [("T4K", None, 4.5)]
    rt = runtime(tmp_path, channels={"T4K": None})
    assert conds(rt) == [("Tstill", None, 1.5)]


def test_default_gate_without_any_of_its_channels_is_no_gate(tmp_path):
    rt = runtime(tmp_path, exclude_channels=["T4K", "Tstill"])
    assert rt.steady is None
    c = rt.context(T0)
    assert (c.steady_ready, c.steady_ready_at, c.steady_detail) == (True, None, "")


def test_default_gate_is_not_modified_by_building(tmp_path):
    runtime(tmp_path, exclude_channels=["Tstill"])
    assert [c["channel"] for c in BLUEFORS_STEADY_STATE["conditions"]] == ["T4K", "Tstill"]


@pytest.mark.parametrize("value", [None, False, {}])
def test_explicit_empty_steady_state_disables_the_default_gate(tmp_path, value):
    assert runtime(tmp_path, steady_state=value).steady is None


def test_explicit_steady_state_replaces_the_default(tmp_path):
    rt = runtime(tmp_path, steady_state={"conditions": [{"channel": "pulse_tube", "above": 0.5}], "for": "2h"})
    assert conds(rt) == [("pulse_tube", 0.5, None)]
    assert rt.steady.for_s == 2 * H


def test_explicit_steady_state_is_strict_about_channels(tmp_path):
    with pytest.raises(RuleConfigError, match="unknown channel"):
        runtime(tmp_path, exclude_channels=["Tstill"],
                steady_state={"conditions": [{"channel": "T4K", "below": 4.5}, {"channel": "Tstill", "below": 1.5}]})


def test_device_without_default_gate(tmp_path):
    assert runtime(tmp_path, type_="lwtest_nogate_device").steady is None
    rt = runtime(tmp_path, type_="lwtest_nogate_device", steady_state={"conditions": [{"channel": "T4K", "below": 4}]})
    assert conds(rt) == [("T4K", None, 4.0)]


def test_bluefors_and_simulated_devices_get_the_fridge_gate(tmp_path):
    rt = runtime(tmp_path, type_="bluefors", host="fridge-pc", api_key="k" * 16)
    try:
        assert conds(rt) == [("T4K", None, 4.5), ("Tstill", None, 1.5)]
        assert rt.steady.units == {"T4K": "K", "Tstill": "K"}
    finally:
        asyncio.run(rt.device.aclose())
    assert conds(runtime(tmp_path, type_="simulated")) == [("T4K", None, 4.5), ("Tstill", None, 1.5)]


@pytest.mark.parametrize(
    "rules, steady_state, keep",
    [
        ([], None, 2 * H),  # at least 1 h, plus 1 h margin
        ([{"channel": "T4K", "rate_above": 0.05, "window": "10m"}], None, 2 * H),
        ([{"channel": "T4K", "rate_above": 0.05, "window": "3h"}], None, 4 * H),
        ([], "default", 8 * H),  # 1 h minimum + the 6 h gate + 1 h margin
        ([{"channel": "cpa_helium", "deviation": 5}], None, 26 * H),  # 24 h baseline + 1 h excluded
        ([{"channel": "cpa_helium", "deviation": 5, "baseline": "2d", "exclude_recent": "2h"}], "default", 57 * H),
        ([{"channel": "cpa_helium", "deviation": 5, "baseline": "2h"}], {"conditions": [{"channel": "T4K", "below": 4}],
                                                                         "for": "12h"}, 16 * H),  # 3 h + 12 h gate + 1 h
    ],
)
def test_history_keep_is_derived_from_rule_horizons_and_gate(tmp_path, rules, steady_state, keep):
    cfg = {"rules": rules}
    if steady_state != "default":
        cfg["steady_state"] = steady_state
    assert runtime(tmp_path, **cfg).history.keep == keep


def test_context_without_gate_is_always_ready(tmp_path):
    rt = runtime(tmp_path, type_="lwtest_nogate_device")
    c = rt.context(T0)
    assert (c.now, c.steady_ready, c.steady_ready_at) == (T0, True, None)
    assert c.readings is rt.latest and c.history is rt.history


def test_context_with_gate_evaluates_latest_and_history(tmp_path):
    rt = runtime(tmp_path)
    c = rt.context(T0)
    assert (c.steady_ready, c.steady_ready_at, c.steady_detail) == (False, None, "T4K unavailable")
    for t in every(T0, T0 + 6 * H):
        for ch, v in (("T4K", 3.2), ("Tstill", 0.8)):
            r = reading(ch, v, ts=t, unit="K")
            rt.latest[ch] = r
            rt.history.add(r, t)
    c = rt.context(T0 + 6 * H)
    assert (c.steady_ready, c.steady_ready_at, c.steady_detail) == (True, T0 + 6 * H, "")

