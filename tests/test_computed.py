"""Computed channels: the safe expression evaluator, config validation, and cooling_water_dT end to end."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

import pytest

from labwatch import engine
from labwatch.computed import ExpressionError, compute_reading, evaluate, expr_names, parse_expr
from labwatch.config import Config, Section
from labwatch.devices.base import ChannelSpec, Device, DeviceConfigError, parse_channels, register
from labwatch.devices.bluefors import DEFAULT_CHANNELS
from labwatch.engine import Monitor, build_runtime
from labwatch.models import AlertEvent, EventKind, Reading, Severity
from labwatch.notifiers.base import Notifier
from labwatch.state import StateStore

T0 = 1_800_000_000.0
DT = 30.0
DT_SPEC = DEFAULT_CHANNELS["cooling_water_dT"]
FIRING, RESOLVED, INFO = EventKind.FIRING, EventKind.RESOLVED, EventKind.INFO

log = logging.getLogger(__name__)


def reading(channel: str, value: float | None, ts: float | None = T0, status: str = "OK") -> Reading:
    when = None if ts is None else datetime.fromtimestamp(ts, timezone.utc)
    return Reading("Fridge", channel, f"src.{channel}", value, when, "K", status=status)


# ---------------------------------------------------------------------------
# the expression evaluator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr, expected",
    [
        ("out - inp", 7.0),
        ("(out + inp) / 2", 293.5),
        ("-(out - inp) * 2", -14.0),
        ("+inp", 290.0),
        ("abs(inp - out)", 7.0),
        ("max(inp, out, 0)", 297.0),
        ("min(inp, 300.5)", 290.0),
        ("out / 1e3", 0.297),
        ("1 - 3", -2.0),
        ("max(abs(inp - out), 2) * 1.5", 10.5),
    ],
)
def test_evaluate(expr, expected):
    result = evaluate(expr, {"out": 297.0, "inp": 290.0})
    assert result == pytest.approx(expected)
    assert isinstance(result, float)


@pytest.mark.parametrize(
    "expr",
    [
        "inp.real",  # attribute access
        "inp.__class__",
        "__import__('os').system('echo pwned')",
        "open('/etc/passwd')",
        "pow(inp, 2)",  # calls to functions that are not whitelisted
        "round(inp)",
        "eval('1')",
        "'abc'",  # strings
        "inp + 'K'",
        "f'{inp}'",
        "b'x'",
        "lambda: 1",  # lambdas
        "(lambda x: x)(inp)",
        "max(inp, key=abs)",  # keywords
        "max(*inp)",
        "inp ** 2",
        "inp // 2",
        "inp % 2",
        "inp | 1",
        "inp > out",
        "inp and out",
        "not inp",
        "~inp",
        "inp[0]",
        "[inp, out]",
        "inp if out else 0",
        "(x := 1)",
        "1j",
        "None",
        "...",
    ],
)
def test_parse_expr_rejects_everything_but_arithmetic(expr):
    with pytest.raises(ExpressionError, match="unsupported syntax"):
        parse_expr(expr)
    with pytest.raises(ExpressionError):
        evaluate(expr, {"inp": 1.0, "out": 2.0})


@pytest.mark.parametrize("expr", ["inp -", "", "(inp", "inp out", "import os"])
def test_parse_expr_syntax_errors(expr):
    with pytest.raises(ExpressionError, match=re.escape(f"invalid expression {expr!r}")):
        parse_expr(expr)


def test_unsupported_syntax_message_names_the_construct():
    with pytest.raises(ExpressionError) as ei:
        parse_expr("inp.real")
    assert str(ei.value) == "unsupported syntax in expression 'inp.real': Attribute"
    with pytest.raises(ExpressionError, match=": Lambda$"):
        parse_expr("lambda: 1")
    with pytest.raises(ExpressionError, match=r": Constant$"):
        parse_expr("'x'")


def test_expr_names():
    assert expr_names("cooling_water_out - cooling_water_in") == {"cooling_water_out", "cooling_water_in"}
    assert expr_names("max(P3, abs(P4 - P3)) / 2") == {"P3", "P4"}  # function names aren't channels
    assert expr_names("1 + 2") == set()
    with pytest.raises(ExpressionError):
        expr_names("P3.real")


def test_evaluate_division_by_zero_raises():
    with pytest.raises(ZeroDivisionError):
        evaluate("a / b", {"a": 1.0, "b": 0.0})


# ---------------------------------------------------------------------------
# compute_reading
# ---------------------------------------------------------------------------


def test_compute_reading_value_unit_and_status():
    readings = {"cooling_water_in": reading("cooling_water_in", 290.0), "cooling_water_out": reading("cooling_water_out", 297.5)}
    r = compute_reading("Flash", DT_SPEC, readings)
    assert (r.device, r.channel, r.unit, r.status) == ("Flash", "cooling_water_dT", "K", "COMPUTED")
    assert r.value == pytest.approx(7.5) and r.raw == "7.5"
    assert r.source == "= cooling_water_out - cooling_water_in"
    assert r.format_value() == "7.5 K"


def test_compute_reading_timestamp_is_the_oldest_inputs():
    readings = {"cooling_water_in": reading("cooling_water_in", 290.0, ts=T0 - 600),
                "cooling_water_out": reading("cooling_water_out", 297.0, ts=T0)}
    r = compute_reading("Flash", DT_SPEC, readings)
    assert r.timestamp == datetime.fromtimestamp(T0 - 600, timezone.utc)
    readings["cooling_water_in"] = reading("cooling_water_in", 290.0, ts=None)  # unknown age
    assert compute_reading("Flash", DT_SPEC, readings).timestamp == datetime.fromtimestamp(T0, timezone.utc)
    readings["cooling_water_out"] = reading("cooling_water_out", 297.0, ts=None)
    assert compute_reading("Flash", DT_SPEC, readings).timestamp is None


@pytest.mark.parametrize(
    "readings, status",
    [
        ({"cooling_water_out": reading("cooling_water_out", 297.0)}, "INVALID (no cooling_water_in)"),
        ({"cooling_water_out": reading("cooling_water_out", 297.0),
          "cooling_water_in": reading("cooling_water_in", None, status="DISCONNECTED")},
         "INVALID (no cooling_water_in)"),
        # none of the inputs exist on this system: the computed channel is "not found" too
        ({"cooling_water_out": reading("cooling_water_out", None, status="NOT_FOUND"),
          "cooling_water_in": reading("cooling_water_in", None, status="NOT_FOUND")},
         "NOT_FOUND"),
        ({}, "INVALID (no cooling_water_in, cooling_water_out)"),
    ],
)
def test_compute_reading_is_invalid_if_an_input_is(readings, status):
    r = compute_reading("Flash", DT_SPEC, readings)
    assert (r.value, r.timestamp, r.status, r.label) == (None, None, status, None)
    assert not r.valid


def test_compute_reading_division_by_zero_is_invalid():
    spec = ChannelSpec.parse("p_ratio", {"expr": "P3 / P4"})
    r = compute_reading("Flash", spec, {"P3": reading("P3", 0.25), "P4": reading("P4", 0.0)})
    assert (r.value, r.status) == (None, "INVALID")
    assert r.timestamp == datetime.fromtimestamp(T0, timezone.utc)


def test_compute_reading_scale_and_labels():
    spec = ChannelSpec.parse("both_on", {"expr": "min(a, b)", "labels": {0: "no", 1: "yes"}})
    r = compute_reading("Flash", spec, {"a": reading("a", 1.0), "b": reading("b", 0.0)})
    assert (r.value, r.label, r.format_value()) == (0.0, "no", "no")
    spec = ChannelSpec.parse("dT_mK", {"expr": "a - b", "unit": "mK", "scale": 1000})
    r = compute_reading("Flash", spec, {"a": reading("a", 0.0125), "b": reading("b", 0.0100)})
    assert r.value == pytest.approx(2.5) and r.unit == "mK"


# ---------------------------------------------------------------------------
# ChannelSpec / parse_channels
# ---------------------------------------------------------------------------


def test_channel_spec_parse_expr():
    spec = ChannelSpec.parse("cooling_water_dT", {"expr": "cooling_water_out - cooling_water_in", "unit": "K"})
    assert spec.computed and spec.expr == "cooling_water_out - cooling_water_in"
    assert (spec.source, spec.unit) == ("= cooling_water_out - cooling_water_in", "K")
    assert spec == DT_SPEC
    plain = ChannelSpec.parse("T4K", "mapper.bf.temperatures.t4k")
    assert not plain.computed and plain.expr is None
    assert not ChannelSpec.parse("x", {"path": "a.b", "unit": "K"}).computed
    with pytest.raises(DeviceConfigError, match=r"needs 'path' \(or 'expr'"):
        ChannelSpec.parse("x", {"unit": "K"})
    with pytest.raises(DeviceConfigError, match=r"needs 'path' \(or 'expr'"):
        ChannelSpec.parse("x", {"expr": ""})


def test_bluefors_default_cooling_water_dt():
    assert DT_SPEC.computed and DT_SPEC.unit == "K"
    assert expr_names(DT_SPEC.expr) == {"cooling_water_out", "cooling_water_in"}
    assert {"cooling_water_out", "cooling_water_in"} <= set(DEFAULT_CHANNELS)


def test_parse_channels_rejects_unknown_names_in_expressions():
    with pytest.raises(DeviceConfigError) as ei:
        parse_channels("Flash", {"channels": {"p_ratio": {"expr": "P3 / P7"}}}, DEFAULT_CHANNELS)
    assert str(ei.value) == "device 'Flash', channel 'p_ratio': expression uses unknown channel(s) ['P7']"


def test_parse_channels_rejects_unsafe_expressions_at_config_time():
    with pytest.raises(DeviceConfigError, match="device 'Flash', channel 'x': unsupported syntax"):
        parse_channels("Flash", {"channels": {"x": {"expr": "__import__('os').getcwd()"}}}, DEFAULT_CHANNELS)
    with pytest.raises(DeviceConfigError, match="device 'Flash', channel 'x': invalid expression"):
        parse_channels("Flash", {"channels": {"x": {"expr": "P3 -"}}}, DEFAULT_CHANNELS)


def test_user_computed_channels_and_overrides():
    chans = parse_channels("Flash", {"channels": {
        "p_ratio": {"expr": "P3 / P4"},
        "p_ratio_pct": {"expr": "p_ratio * 100", "unit": "%"},  # may use another computed channel
        "cooling_water_dT": {"expr": "abs(cooling_water_out - cooling_water_in)", "unit": "K"},
    }}, DEFAULT_CHANNELS)
    assert chans["p_ratio"].computed and chans["p_ratio_pct"].unit == "%"
    assert chans["cooling_water_dT"].expr == "abs(cooling_water_out - cooling_water_in)"


@pytest.mark.parametrize(
    "cfg",
    [
        {"exclude_channels": ["cooling_water_in"]},
        {"channels": {"cooling_water_out": None}},
        {"exclude_channels": ["cooling_water_in", "cooling_water_out"]},
    ],
)
def test_default_computed_channel_is_dropped_silently_when_inputs_are_excluded(cfg):
    chans = parse_channels("Flash", cfg, DEFAULT_CHANNELS)
    assert "cooling_water_dT" not in chans
    assert "T4K" in chans


def test_configured_computed_channel_with_excluded_inputs_is_an_error():
    cfg = {"exclude_channels": ["cooling_water_in"],
           "channels": {"cooling_water_dT": {"expr": "cooling_water_out - cooling_water_in"}}}
    with pytest.raises(DeviceConfigError, match=r"unknown channel\(s\) \['cooling_water_in'\]"):
        parse_channels("Flash", cfg, DEFAULT_CHANNELS)
    cfg = {"default_channels": False, "channels": {"T4K": "mapper.bf.temperatures.t4k", "x": {"expr": "T4K - T50K"}}}
    with pytest.raises(DeviceConfigError, match="T50K"):
        parse_channels("Flash", cfg, DEFAULT_CHANNELS)


def test_without_default_channels_no_default_computed_channel():
    chans = parse_channels("Flash", {"default_channels": False, "channels": {"T4K": "mapper.bf.temperatures.t4k"}},
                           DEFAULT_CHANNELS)
    assert list(chans) == ["T4K"]


# ---------------------------------------------------------------------------
# end to end: device -> computed channel -> rules -> store
# ---------------------------------------------------------------------------

CHANNELS = {k: DEFAULT_CHANNELS[k] for k in ("T4K", "pulse_tube", "cooling_water_in", "cooling_water_out",
                                             "cooling_water_dT")}
NORMAL = {"T4K": 3.2, "pulse_tube": 1.0, "cooling_water_in": 290.0, "cooling_water_out": 297.0}
DEFAULTS = {"for": 60, "clear_for": 120, "arm_after": "10m", "repeat_interval": "2h"}
DT_RULE = {"channel": "cooling_water_dT", "above": 15, "severity": "warning",
           "message": "Cooling water flow through the compressor may be too low."}
DT_RULE_ID = "Fridge/cooling_water_dT above 15 K"
PT_RULE = {"channel": "pulse_tube", "not_equals": 1, "severity": "critical"}
PT_RULE_ID = "Fridge/pulse_tube is not on"


class FakeClock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def time(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t


@register("lwtest_computed_fridge")
class Fridge(Device):
    summary_channels = ("T4K", "pulse_tube", "cooling_water_dT")

    def __init__(self, name, channels, clock) -> None:
        super().__init__(name, channels, poll_interval=DT, unreachable_after=300.0)
        self.clock = clock
        self.values: dict[str, float | None] = {}
        self.ages: dict[str, float] = {}  # channel -> how old the controller's value is (s)

    @classmethod
    def from_config(cls, name, cfg):
        return cls(name, parse_channels(name, cfg, CHANNELS), cfg["clock"])

    async def poll(self) -> list[Reading]:
        out = []
        for ch, v in self.values.items():
            spec = self.channels[ch]
            assert not spec.computed  # the engine computes those
            ts = datetime.fromtimestamp(self.clock.t - self.ages.get(ch, 0.0), timezone.utc)
            out.append(Reading(self.name, ch, spec.source, v, ts, spec.unit, raw=None if v is None else str(v),
                               status="OK" if v is not None else "DISCONNECTED", label=spec.label_for(v)))
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
    def __init__(self, tmp_path, clock, rules=(DT_RULE,), store=None, **device_cfg) -> None:
        self.clock = clock
        self.config = Config(
            path=tmp_path / "config.yaml", state_db=tmp_path / "state.sqlite", history_days=30,
            rule_defaults=dict(DEFAULTS), devices=[], notifiers=[], heartbeat_url=None, heartbeat_interval=60.0,
            daily_summary=None, notify_startup=False, secrets=set(),
        )
        sec = Section({"name": "Fridge", "type": "lwtest_computed_fridge", "clock": clock, "rules": list(rules),
                       **device_cfg}, set())
        self.config.devices.append(sec)
        self.rt = build_runtime(self.config, sec)
        self.dev: Fridge = self.rt.device
        self.store = store if store is not None else StateStore(":memory:")
        self.inbox = Inbox()
        self.monitor = Monitor(self.config, [self.rt], [self.inbox], self.store)
        self.rt.unreachable.started = clock.t

    def poll(self, dt: float = DT) -> list[AlertEvent]:
        """Like Monitor._device_loop: an exception in a poll is logged, monitoring goes on."""
        self.clock.t += dt
        n = len(self.inbox.events)

        async def go():
            try:
                await self.monitor.poll_once(self.rt)
            except Exception:  # noqa: BLE001
                log.exception("unexpected error while polling")
            while not self.monitor._queue.empty():
                await self.monitor.dispatch(self.monitor._queue.get_nowait())

        asyncio.run(go())
        return self.inbox.events[n:]

    def run_for(self, seconds: float) -> list[AlertEvent]:
        events: list[AlertEvent] = []
        for _ in range(round(seconds / DT)):
            events += self.poll()
        return events


def kinds(events):
    return [(e.kind, e.rule_id) for e in events]


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    c = FakeClock()
    monkeypatch.setattr(engine, "time", c)
    return c


@pytest.fixture
def h(tmp_path, clock) -> Harness:
    return Harness(tmp_path, clock)


def test_cooling_water_dt_is_computed_and_stored(h):
    h.dev.values = dict(NORMAL)
    h.poll()
    r = h.rt.latest["cooling_water_dT"]
    assert (r.value, r.status, r.unit) == (7.0, "COMPUTED", "K")
    assert r.timestamp == datetime.fromtimestamp(h.clock.t, timezone.utc)
    row = {x["channel"]: x for x in h.store.latest("Fridge")}["cooling_water_dT"]
    assert (row["value"], row["status"], row["source"]) == (7.0, "COMPUTED", "= cooling_water_out - cooling_water_in")
    h.dev.values["cooling_water_out"] = 298.5
    h.poll()
    assert h.store.history("Fridge", "cooling_water_dT", 0) == [(T0 + DT, 7.0), (T0 + 2 * DT, 8.5)]
    assert list(h.rt.history.points("cooling_water_dT")) == [(T0 + DT, 7.0), (T0 + 2 * DT, 8.5)]


def test_threshold_rule_on_cooling_water_dt_fires_and_resolves(h):
    h.dev.values = dict(NORMAL)
    assert kinds(h.run_for(630)) == [(INFO, "Fridge/armed")]
    h.dev.values["cooling_water_out"] = 306.0  # too little water flows: dT 16 K
    events = h.run_for(90)
    assert kinds(events) == [(FIRING, DT_RULE_ID)]
    ev = events[0]
    assert ev.severity is Severity.WARNING and ev.unit == "K" and ev.value == pytest.approx(16.0)
    assert ev.message.startswith("cooling_water_dT = 16 K\nCooling water flow through the compressor")
    assert "Now: T4K 3.2 K | pulse_tube on | cooling_water_dT 16 K" in ev.message
    h.dev.values["cooling_water_out"] = 297.0
    assert kinds(h.run_for(150)) == [(RESOLVED, DT_RULE_ID)]


def test_invalid_input_makes_dt_invalid_and_freezes_the_rule(h):
    h.dev.values = dict(NORMAL)
    h.run_for(630)
    h.dev.values["cooling_water_out"] = 306.0
    h.poll()
    pending = h.rt.rules[0].state.pending_since
    assert pending is not None
    h.dev.values["cooling_water_in"] = None  # compressor driver lost the sensor
    assert h.run_for(600) == []
    r = h.rt.latest["cooling_water_dT"]
    assert (r.value, r.status) == (None, "INVALID (no cooling_water_in)")
    assert r.format_value() == "invalid (INVALID (no cooling_water_in))"
    assert h.rt.rules[0].state.pending_since == pending  # no data: state unchanged
    row = {x["channel"]: x for x in h.store.latest("Fridge")}["cooling_water_dT"]
    assert row["value"] is None and row["status"] == "INVALID (no cooling_water_in)"


def test_invalid_dt_alarms_with_on_invalid_alarm(tmp_path, clock):
    h = Harness(tmp_path, clock, rules=[dict(DT_RULE, on_invalid="alarm")])
    h.dev.values = dict(NORMAL)
    h.run_for(630)
    h.dev.values["cooling_water_in"] = None
    (ev,) = h.run_for(90)
    assert (ev.kind, ev.rule_id) == (FIRING, DT_RULE_ID)
    assert ev.message.startswith("cooling_water_dT unreadable (INVALID (no cooling_water_in))")


def test_stale_input_makes_dt_stale(tmp_path, clock):
    h = Harness(tmp_path, clock, rules=[{"channel": "cooling_water_dT", "max_age": "5m", "auto_arm": False}])
    h.dev.values = dict(NORMAL)
    assert h.run_for(300) == []
    h.dev.ages["cooling_water_in"] = 600.0  # the inlet sensor stopped updating 10 min ago
    events = h.run_for(90)  # rule_defaults `for`: 60 s
    assert kinds(events) == [(FIRING, "Fridge/cooling_water_dT not updated for 5 min")]
    assert "cooling_water_dT last updated 10.0 min ago" in events[0].message


def test_dt_history_follows_the_oldest_input_timestamp(h):
    h.dev.values = dict(NORMAL)
    h.dev.ages["cooling_water_in"] = 600.0
    h.poll()
    h.dev.ages["cooling_water_in"] = 600.0 + DT  # inlet value not updated: same timestamp as before
    h.dev.values["cooling_water_out"] = 299.0
    h.poll()
    assert list(h.rt.history.points("cooling_water_dT")) == [(T0 + DT - 600, 7.0)]


def test_restart_seeds_computed_channel_history(tmp_path, clock):
    db = tmp_path / "state.sqlite"
    h1 = Harness(tmp_path, clock, store=StateStore(db))
    h1.dev.values = dict(NORMAL)
    h1.run_for(300)
    h1.store.close()
    h2 = Harness(tmp_path, clock, store=StateStore(db))
    h2.monitor._restore_state()
    assert len(h2.rt.history.points("cooling_water_dT")) == 10


def test_default_dt_dropped_when_inputs_excluded_and_engine_still_works(tmp_path, clock):
    h = Harness(tmp_path, clock, rules=[PT_RULE], exclude_channels=["cooling_water_in"])
    assert "cooling_water_dT" not in h.dev.channels
    h.dev.values = {"T4K": 3.2, "pulse_tube": 1.0, "cooling_water_out": 297.0}
    h.run_for(630)
    assert "cooling_water_dT" not in h.rt.latest
    h.dev.values["pulse_tube"] = 0.0
    assert kinds(h.run_for(90)) == [(FIRING, PT_RULE_ID)]


def test_rule_on_unknown_computed_channel_is_a_config_error(tmp_path, clock):
    from labwatch.rules import RuleConfigError

    with pytest.raises(RuleConfigError, match="unknown channel 'cooling_water_dT'"):
        Harness(tmp_path, clock, rules=[DT_RULE], exclude_channels=["cooling_water_in"])


@pytest.mark.parametrize("expr", ["max(cooling_water_out)", "abs(cooling_water_out, cooling_water_in)", "min()"])
def test_bad_computed_expression_does_not_silence_other_alerts(tmp_path, clock, expr):
    try:
        h = Harness(tmp_path, clock, rules=[PT_RULE], channels={"bad": {"expr": expr, "unit": "K"}})
    except DeviceConfigError:
        return  # rejected when the config is loaded: fine too
    h.dev.values = dict(NORMAL)
    h.run_for(630)
    h.dev.values["pulse_tube"] = 0.0  # power outage: the pulse tube stops
    assert (FIRING, PT_RULE_ID) in kinds(h.run_for(600))
