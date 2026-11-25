"""Alert rules: the ``Rule.step`` state machine, the rule kinds and building rules from config."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from labwatch.devices.bluefors import DEFAULT_CHANNELS
from labwatch.models import Reading, Severity, parse_duration
from labwatch.rules import (
    Context,
    History,
    MaxAgeRule,
    RateRule,
    RuleConfigError,
    RuleOptions,
    RuleState,
    StateRule,
    ThresholdRule,
    Transition,
    UnreachableRule,
    build_rules,
    slope_per_minute,
)

T0 = 1_800_000_000.0  # arbitrary epoch seconds; all times below are offsets from it
DEV = "Fridge"
ARMED, FIRING, REMINDER, RESOLVED = (
    Transition.ARMED, Transition.FIRING, Transition.REMINDER, Transition.RESOLVED,
)
ON_OFF_ERROR = {0: "off", 1: "on", 2: "error"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def reading(channel: str, value: float | None, ts: float | None = None, unit: str | None = None,
            label: str | None = None) -> Reading:
    when = None if ts is None else datetime.fromtimestamp(ts, timezone.utc)
    return Reading(DEV, channel, f"src.{channel}", value, when, unit, label=label)


def options(**kw) -> RuleOptions:
    base = dict(for_s=60.0, clear_for=120.0, auto_arm=True, arm_after=600.0, repeat_interval=3600.0)
    return RuleOptions(**{**base, **kw})


def t4k_above(limit: float = 5.0, hysteresis: float = 0.0, **kw) -> ThresholdRule:
    return ThresholdRule(DEV, "T4K", None, options(**kw), above=limit, hysteresis=hysteresis, unit="K")


class Feed:
    """Drives one rule through (fake) time.

    ``at(t, value)`` delivers a reading of ``value`` at ``T0 + t`` and returns the
    transition; ``run(start, stop, value)`` repeats that every ``every`` seconds and
    returns ``[(t, transition), ...]`` for the polls that produced one.
    """

    def __init__(self, rule, every: float = 30.0) -> None:
        self.rule = rule
        self.every = every
        self.history = History()

    def at(self, t: float, value: float | None, **reading_kw) -> str | None:
        now = T0 + t
        r = reading(self.rule.channel, value, ts=now, **reading_kw)
        self.history.add(r, now)
        _, tr = self.rule.step(Context(now=now, readings={r.channel: r}, history=self.history))
        return tr

    def run(self, start: float, stop: float, value: float | None) -> list[tuple[float, str]]:
        out = []
        t = start
        while t < stop:
            tr = self.at(t, value)
            if tr:
                out.append((t, tr))
            t += self.every
        return out


def fire(feed: Feed, t: float, value: float, delivered: bool = True) -> None:
    """Make an armed rule fire at ``t`` (for_s must be 0) and mark it delivered like the engine does."""
    assert feed.at(t, value) == FIRING
    feed.rule.state.delivered = delivered


# ---------------------------------------------------------------------------
# auto-arm
# ---------------------------------------------------------------------------


def test_disarmed_rule_never_fires_while_condition_is_true():
    f = Feed(t4k_above(5))  # auto_arm, arm_after 10 min
    assert f.run(0, 3 * 3600, 290.0) == []  # a warm fridge for 3 hours
    st = f.rule.state
    assert (st.armed, st.firing, st.pending_since) == (False, False, None)
    assert f.rule.status_text() == "disarmed"


def test_rule_arms_after_arm_after_healthy_seconds_then_fires():
    f = Feed(t4k_above(5))
    assert f.run(0, 3600, 290.0) == []
    # cold from t=3600: arms exactly arm_after (600 s) later, and only once
    assert f.run(3600, 7200, 3.2) == [(4200, ARMED)]
    assert f.rule.status_text() == "armed"
    # now a warm-up is a real alarm: fires after `for` (60 s)
    assert f.run(7200, 7400, 6.0) == [(7260, FIRING)]


def test_arming_needs_an_uninterrupted_healthy_period():
    f = Feed(t4k_above(5))
    assert f.run(0, 570, 3.2) == []  # 540 s healthy ...
    assert f.at(570, 7.0) is None  # ... one bad reading resets the arm timer
    assert f.run(600, 1300, 3.2) == [(1200, ARMED)]


def test_auto_arm_false_is_armed_from_the_start_and_fires_after_for():
    f = Feed(t4k_above(5, auto_arm=False))
    assert f.rule.state.armed
    assert f.run(0, 300, 290.0) == [(60, FIRING)]


# ---------------------------------------------------------------------------
# for / clear_for / reminders
# ---------------------------------------------------------------------------


def test_for_debounce_fires_only_after_condition_held_long_enough():
    f = Feed(t4k_above(5, auto_arm=False, for_s=60))
    assert f.at(0, 6.0) is None
    assert f.rule.status_text() == "pending"
    assert f.at(30, 6.0) is None
    assert f.at(60, 6.0) == FIRING
    assert f.rule.state.fired_at == T0 + 60


def test_for_debounce_ignores_single_bad_readings():
    f = Feed(t4k_above(5, auto_arm=False, for_s=60))
    for i in range(120):  # one hour, every other reading bad
        assert f.at(30 * i, 6.0 if i % 2 == 0 else 3.2) is None
    assert not f.rule.state.firing


def test_for_zero_fires_on_first_bad_reading():
    f = Feed(t4k_above(5, auto_arm=False, for_s=0))
    assert f.at(0, 5.01) == FIRING


def test_clear_for_prevents_flapping_resolve_and_refire():
    f = Feed(t4k_above(5, auto_arm=False, for_s=0, clear_for=120))
    fire(f, 0, 6.0)
    assert f.run(30, 120, 3.2) == []  # healthy for 60 s only (30, 60, 90) - still firing
    assert f.rule.state.firing
    # flapping around the threshold for an hour: neither resolves nor fires again
    for i in range(121):  # ends on a bad reading at t=3720
        assert f.at(120 + 30 * i, 6.0 if i % 2 == 0 else 3.2) not in (RESOLVED, FIRING)
    assert f.rule.state.firing
    # healthy for a full clear_for: resolves once
    assert f.run(3750, 4100, 3.2) == [(3870, RESOLVED)]
    assert not f.rule.state.firing and f.rule.state.fired_at is None


def test_reminders_every_repeat_interval():
    f = Feed(t4k_above(5, auto_arm=False, for_s=0, repeat_interval=3600), every=60)
    trs = f.run(0, 3 * 3600 + 60, 7.0)
    assert trs == [(0, FIRING), (3600, REMINDER), (7200, REMINDER), (10800, REMINDER)]


def test_no_reminders_with_repeat_interval_zero():
    f = Feed(t4k_above(5, auto_arm=False, for_s=0, repeat_interval=0), every=300)
    assert f.run(0, 86400, 7.0) == [(0, FIRING)]


@pytest.mark.parametrize("delivered, expected", [(True, [(120, RESOLVED)]), (False, [])])
def test_resolved_only_if_the_firing_was_delivered(delivered, expected):
    # delivered=False is what the engine leaves behind while the rule is muted
    f = Feed(t4k_above(5, auto_arm=False, for_s=0, clear_for=120))
    fire(f, 0, 6.0, delivered=delivered)
    assert f.run(0, 300, 3.2) == expected
    assert f.rule.state.firing is False
    assert f.rule.state.delivered is False


def test_firing_resets_delivered_flag():
    f = Feed(t4k_above(5, auto_arm=False, for_s=0, clear_for=0))
    fire(f, 0, 6.0, delivered=True)
    assert f.at(30, 3.2) == RESOLVED
    assert f.at(60, 6.0) == FIRING
    assert f.rule.state.delivered is False  # the engine must deliver this new alert


def test_no_data_leaves_state_unchanged():
    f = Feed(t4k_above(5, auto_arm=False, for_s=60, clear_for=120))

    def no_data(t):
        before = f.rule.state.to_dict()
        assert f.at(t, None) is None  # invalid reading (e.g. sensor out of range)
        _, tr = f.rule.step(Context(now=T0 + t, readings={}, history=f.history))  # channel missing
        assert tr is None
        assert f.rule.state.to_dict() == before

    f.at(0, 6.0)  # pending since 0
    no_data(30)
    assert f.at(60, 6.0) == FIRING  # the pending timer was not reset by the gap
    f.rule.state.delivered = True
    no_data(90)
    assert f.rule.state.firing
    f.at(100, 3.2)  # healthy since 100
    no_data(150)
    assert f.at(220, 3.2) == RESOLVED  # the clear timer was not reset by the gap


def test_status_text():
    f = Feed(t4k_above(5, auto_arm=False, for_s=60))
    assert f.rule.status_text() == "armed"
    f.at(0, 6.0)
    assert f.rule.status_text() == "pending"
    f.at(60, 6.0)
    assert f.rule.status_text() == "FIRING"


def test_rule_state_roundtrip_ignores_unknown_keys():
    st = RuleState(armed=True, firing=True, delivered=True, pending_since=1.0, last_detail="x")
    d = st.to_dict()
    assert RuleState.from_dict({**d, "from_a_future_version": 42}) == st


# ---------------------------------------------------------------------------
# ThresholdRule
# ---------------------------------------------------------------------------


def test_threshold_name_detail_and_options():
    rule = t4k_above(5, severity=Severity.CRITICAL)
    assert rule.name == "T4K above 5 K"
    assert rule.rule_id == "Fridge/T4K above 5 K"
    assert rule.title == "Fridge: T4K above 5 K"
    ctx = Context(T0, {"T4K": reading("T4K", 6.2, unit="K")}, History())
    v = rule.check(ctx)
    assert (v.active, v.value, v.detail) == (True, 6.2, "T4K = 6.2 K")

    both = ThresholdRule(DEV, "P1", None, options(), above=1e-4, below=1e-9, unit="mbar")
    assert both.name == "P1 above 1.000e-04 mbar or below 1.000e-09 mbar"

    with pytest.raises(RuleConfigError):
        ThresholdRule(DEV, "T4K", None, options())


def test_threshold_hysteresis_above():
    f = Feed(t4k_above(5, hysteresis=0.3, auto_arm=False, for_s=0, clear_for=0))
    fire(f, 0, 5.5)
    assert f.at(30, 4.8) is None and f.rule.state.firing  # 4.8 > 5 - 0.3: still bad
    assert f.at(60, 4.71) is None and f.rule.state.firing
    assert f.at(90, 4.7) == RESOLVED  # at/below 4.7: healthy
    # hysteresis only applies while firing
    assert f.at(120, 4.8) is None and not f.rule.state.firing
    assert f.at(150, 5.0) is None  # not *above* 5
    assert f.at(180, 5.01) == FIRING


def test_threshold_hysteresis_below():
    rule = ThresholdRule(DEV, "flow", None, options(auto_arm=False, for_s=0, clear_for=0),
                         below=0.1, hysteresis=0.05, unit="mmol/s")
    f = Feed(rule)
    fire(f, 0, 0.05)
    assert f.at(30, 0.12) is None and rule.state.firing  # < 0.1 + 0.05
    assert f.at(60, 0.16) == RESOLVED


# ---------------------------------------------------------------------------
# RateRule and History
# ---------------------------------------------------------------------------


def rate_ctx(points, now=None, channel="T4K", history=None):
    h = history or History()
    for t, v in points:
        h.add(reading(channel, v, ts=T0 + t), fallback_ts=T0 + t)
    last_t, last_v = points[-1]
    now = T0 + (last_t if now is None else now)
    return Context(now, {channel: reading(channel, last_v, ts=T0 + last_t, unit="K")}, h)


def warming(rate_per_min: float, seconds: float, every: float = 60.0, start: float = 3.0):
    return [(t, start + rate_per_min * t / 60.0) for t in range(0, int(seconds) + 1, int(every))]


def t4k_rate(**kw) -> RateRule:
    return RateRule(DEV, "T4K", None, options(), window=600, unit="K", **kw)


def test_rate_rule_detects_warmup_slope():
    rule = t4k_rate(rate_above=0.05)
    v = rule.check(rate_ctx(warming(0.1, 600)))
    assert v.active is True
    assert v.value == pytest.approx(0.1)
    assert v.detail == "T4K changing at +0.1 K/min over 10 min, now 4 K"
    assert rule.name == "T4K rising faster than 0.05 K/min"

    assert rule.check(rate_ctx(warming(0.01, 600))).active is False
    assert rule.check(rate_ctx(warming(0.0, 600))).active is False


def test_rate_rule_needs_three_points_spanning_half_the_window():
    rule = t4k_rate(rate_above=0.05)
    assert rule.check(rate_ctx(warming(1.0, 60))).active is None  # 2 points
    assert rule.check(rate_ctx(warming(1.0, 240))).active is None  # 5 points, but 240 s < 300 s
    assert rule.check(rate_ctx(warming(1.0, 300))).active is True  # exactly half the window


def test_rate_rule_dedupes_identical_timestamps():
    """A controller that hasn't updated returns the same sample on every poll."""
    rule = t4k_rate(rate_above=0.05)
    h = History()
    stale = reading("T4K", 4.0, ts=T0)
    for i in range(20):
        h.add(stale, fallback_ts=T0 + 30 * i)
    assert h.window("T4K", 600, T0 + 570) == [(T0, 4.0)]
    ctx = Context(T0 + 570, {"T4K": stale}, h)
    assert rule.check(ctx).active is None


def test_rate_rule_only_uses_points_inside_the_window():
    rule = t4k_rate(rate_above=0.05)
    pts = [(0, 3.0), (60, 50.0)] + [(t, 4.0) for t in range(1200, 1801, 60)]  # old spike, then flat
    assert rule.check(rate_ctx(pts)).active is False


def test_rate_below_detects_falling_value():
    rule = RateRule(DEV, "flow", None, options(), rate_below=-0.05, window=600, unit="mmol/s")
    assert rule.name == "flow changing slower than -0.05 mmol/s/min"
    pts = [(t, 0.6 - 0.1 * t / 60) for t in range(0, 601, 60)]
    v = rule.check(rate_ctx(pts, channel="flow"))
    assert v.active is True and v.value == pytest.approx(-0.1)


def test_rate_rule_requires_a_rate():
    with pytest.raises(RuleConfigError):
        RateRule(DEV, "T4K", None, options())


def test_history_ignores_invalid_and_out_of_order_and_prunes():
    h = History(keep_seconds=600)
    h.add(reading("T4K", None, ts=T0), T0)
    h.add(reading("T4K", 3.0, ts=T0 + 10), T0)
    h.add(reading("T4K", 9.9, ts=T0 + 5), T0)  # older than the last sample
    h.add(reading("T4K", 3.1), fallback_ts=T0 + 20)  # no device timestamp: poll time is used
    assert h.window("T4K", 3600, T0 + 20) == [(T0 + 10, 3.0), (T0 + 20, 3.1)]
    h.add(reading("T4K", 3.2, ts=T0 + 700), T0 + 700)
    assert h.window("T4K", 3600, T0 + 700) == [(T0 + 700, 3.2)]  # older than keep_seconds dropped
    assert h.window("nope", 3600, T0) == []


def test_slope_per_minute():
    assert slope_per_minute([(0, 1.0), (60, 2.0), (120, 3.0)]) == pytest.approx(1.0)
    assert slope_per_minute([(0, 1.0)]) is None
    assert slope_per_minute([(5, 1.0), (5, 2.0)]) is None  # no time spread


# ---------------------------------------------------------------------------
# StateRule
# ---------------------------------------------------------------------------


def pt_ctx(value: float | None, label: str | None = None) -> Context:
    return Context(T0, {"pulse_tube": reading("pulse_tube", value, label=label)}, History())


def test_state_rule_not_equals_with_labels():
    rule = StateRule(DEV, "pulse_tube", None, options(), not_equals=1, labels=ON_OFF_ERROR)
    assert rule.name == "pulse_tube is not on"
    v = rule.check(pt_ctx(0.0, "off"))
    assert (v.active, v.value, v.detail) == (True, 0.0, "pulse_tube = off")
    assert rule.check(pt_ctx(2.0, "error")).active is True
    assert rule.check(pt_ctx(1.0, "on")).active is False
    assert rule.check(pt_ctx(None)).active is None


def test_state_rule_equals_list():
    rule = StateRule(DEV, "pulse_tube", None, options(), equals=[0, 2], labels=ON_OFF_ERROR)
    assert rule.name == "pulse_tube is off/error"
    assert [rule.check(pt_ctx(x)).active for x in (0.0, 1.0, 2.0)] == [True, False, True]


def test_state_rule_not_equals_list_and_unlabelled_values():
    rule = StateRule(DEV, "cpa_alarm", None, options(), not_equals=[0, 4])
    assert rule.name == "cpa_alarm is not 0/4"
    ctx = Context(T0, {"cpa_alarm": reading("cpa_alarm", 3.0)}, History())
    assert rule.check(ctx).active is True
    with pytest.raises(RuleConfigError):
        StateRule(DEV, "pulse_tube", None, options())


# ---------------------------------------------------------------------------
# MaxAgeRule
# ---------------------------------------------------------------------------


def test_max_age_rule():
    rule = MaxAgeRule(DEV, "T4K", None, options(), max_age=1200)
    assert rule.name == "T4K not updated for 20 min"
    stale = Context(T0 + 1300, {"T4K": reading("T4K", 3.2, ts=T0)}, History())
    v = rule.check(stale)
    assert (v.active, v.value, v.detail) == (True, 1300, "T4K last updated 21.7 min ago")
    fresh = Context(T0 + 100, {"T4K": reading("T4K", 3.2, ts=T0)}, History())
    assert rule.check(fresh).active is False
    # no timestamp / no reading: unknown (the unreachable rule covers a dead device)
    assert rule.check(Context(T0, {"T4K": reading("T4K", 3.2)}, History())).active is None
    assert rule.check(Context(T0, {}, History())).active is None


# ---------------------------------------------------------------------------
# UnreachableRule
# ---------------------------------------------------------------------------


def unreachable(after: float = 300.0) -> UnreachableRule:
    return UnreachableRule(DEV, after, options(auto_arm=False, for_s=0, clear_for=0))


def test_unreachable_rule_without_reference_time_has_no_data():
    rule = unreachable()
    assert rule.check(Context(T0, {}, History())).active is None
    assert rule.rule_id == "Fridge/unreachable"
    assert rule.name == "unreachable"


def test_unreachable_rule_relative_to_start_when_never_successful():
    rule = unreachable(300)
    rule.started = T0
    rule.last_error = "Fridge: cannot connect to fridge-pc:49098 (ConnectError)"
    v = rule.check(Context(T0 + 200, {}, History()))
    assert v.active is False
    assert v.detail == "no successful poll since labwatch started 3.3 min ago"  # no error while ok
    v = rule.check(Context(T0 + 360, {}, History()))
    assert v.active is True
    assert v.detail == (
        "no successful poll since labwatch started 6.0 min ago; "
        "last error: Fridge: cannot connect to fridge-pc:49098 (ConnectError)"
    )


def test_unreachable_rule_relative_to_last_success():
    rule = unreachable(300)
    rule.started = T0
    rule.last_success = T0 + 1000
    v = rule.check(Context(T0 + 1200, {}, History()))
    assert (v.active, v.detail) == (False, "last successful poll 3.3 min ago")
    v = rule.check(Context(T0 + 1301, {}, History()))
    assert v.active is True and v.detail.startswith("last successful poll 5.0 min ago")


def test_unreachable_rule_fires_and_resolves_immediately():
    rule = unreachable(300)
    rule.started = T0
    _, tr = rule.step(Context(T0 + 301, {}, History()))
    assert tr == FIRING
    rule.state.delivered = True
    rule.last_success = T0 + 330
    _, tr = rule.step(Context(T0 + 330, {}, History()))
    assert tr == RESOLVED  # clear_for 0: the first successful poll resolves it


# ---------------------------------------------------------------------------
# build_rules (config validation)
# ---------------------------------------------------------------------------


def build(*cfgs, defaults=None):
    return build_rules(DEV, list(cfgs), defaults or {}, DEFAULT_CHANNELS)


def test_build_rules_kinds_units_and_labels():
    rules = build(
        {"channel": "T4K", "above": 5, "hysteresis": 0.3},
        {"channel": "T4K", "rate_above": 0.05, "window": "10m"},
        {"channel": "pulse_tube", "not_equals": 1},
        {"channel": "T4K", "max_age": "20m"},
        {"channel": "TMXC", "above": 100, "name": "mixing chamber warm"},
    )
    assert [type(r) for r in rules] == [ThresholdRule, RateRule, StateRule, MaxAgeRule, ThresholdRule]
    assert [r.name for r in rules] == [
        "T4K above 5 K",
        "T4K rising faster than 0.05 K/min",
        "pulse_tube is not on",  # labels come from the channel spec
        "T4K not updated for 20 min",
        "mixing chamber warm",
    ]
    assert rules[0].hysteresis == 0.3
    assert rules[1].window == 600
    assert rules[3].max_age == 1200
    assert rules[4].rule_id == "Fridge/mixing chamber warm"


def test_build_rules_options_and_defaults():
    defaults = {"for": 60, "clear_for": "2m", "arm_after": "10m", "repeat_interval": "2h", "severity": "warning"}
    a, b = build(
        {"channel": "T4K", "above": 5},
        {"channel": "pulse_tube", "not_equals": 1, "severity": "critical", "for": "5m", "auto_arm": False,
         "repeat_interval": 0, "message": "  Check the compressor.  ", "detail_channel": "cpa_alarm_text"},
        defaults=defaults,
    )
    assert (a.opts.for_s, a.opts.clear_for, a.opts.arm_after, a.opts.repeat_interval) == (60, 120, 600, 7200)
    assert a.opts.severity is Severity.WARNING and a.opts.auto_arm is True
    assert not a.state.armed
    assert b.opts.severity is Severity.CRITICAL
    assert (b.opts.for_s, b.opts.repeat_interval) == (300, 0)
    assert b.opts.auto_arm is False and b.state.armed  # auto_arm: false -> armed immediately
    assert b.opts.message == "  Check the compressor.  "
    assert b.opts.detail_channel == "cpa_alarm_text"


def test_build_rules_builtin_option_defaults():
    (r,) = build({"channel": "T4K", "above": 5})
    assert (r.opts.for_s, r.opts.clear_for, r.opts.arm_after, r.opts.repeat_interval) == (0, 60, 300, 3600)
    assert r.opts.severity is Severity.WARNING


@pytest.mark.parametrize(
    "cfg, match",
    [
        ({"above": 5}, "missing 'channel'"),
        ({"channel": "T5K", "above": 5}, r"unknown channel 'T5K' \(known: .*T4K"),
        ({"channel": "T4K", "above": 5, "hysterisis": 0.3}, r"unknown keys \['hysterisis'\]"),
        ({"channel": "T4K", "above": 5, "window": "10m"}, r"unknown keys \['window'\]"),
        ({"channel": "T4K", "above": 5, "max_age": "20m"}, "need exactly one condition type"),
        ({"channel": "T4K", "above": 5, "rate_above": 0.1}, "need exactly one condition type"),
        ({"channel": "T4K", "hysteresis": 0.3}, "need exactly one condition type.*found none"),
        ({"channel": "T4K", "above": "five"}, "rule 'T4K'"),
        ({"channel": "T4K", "max_age": "20 minutes"}, "rule 'T4K'"),
    ],
)
def test_build_rules_rejects_bad_config(cfg, match):
    with pytest.raises(RuleConfigError, match=match):
        build(cfg)


def test_build_rules_error_names_device_and_rule():
    with pytest.raises(RuleConfigError, match="device 'Fridge', rule 'my rule'"):
        build({"channel": "T5K", "above": 5, "name": "my rule"})


def test_build_rules_rejects_duplicate_rule_ids():
    with pytest.raises(RuleConfigError, match="duplicate rule 'Fridge/T4K above 5 K'"):
        build({"channel": "T4K", "above": 5}, {"channel": "T4K", "above": 5, "severity": "critical"})
    # a distinct name fixes it
    rules = build({"channel": "T4K", "above": 5}, {"channel": "T4K", "above": 5, "name": "T4K high (2)"})
    assert len(rules) == 2


def test_build_rules_empty():
    assert build_rules(DEV, None, {}, DEFAULT_CHANNELS) == []


@pytest.mark.parametrize(
    "cfg",
    [
        {"channel": "T4K", "above": 5, "severity": "critcal"},
        {"channel": "T4K", "above": 5, "for": "5 min"},
    ],
)
def test_build_rules_invalid_option_values_are_rule_config_errors(cfg):
    with pytest.raises(RuleConfigError, match="rule 'T4K'"):
        build(cfg)


# ---------------------------------------------------------------------------
# durations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, seconds",
    [(45, 45), (2.5, 2.5), ("45", 45), ("90s", 90), ("5m", 300), ("2h", 7200), ("1d", 86400),
     (" 10M ", 600), ("1.5h", 5400), ("0", 0)],
)
def test_parse_duration(value, seconds):
    assert parse_duration(value) == seconds


@pytest.mark.parametrize("value", ["5 min", "", "m", "abc", "1w"])
def test_parse_duration_invalid(value):
    with pytest.raises(ValueError):
        parse_duration(value)
