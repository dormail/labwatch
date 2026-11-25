"""The monitor end to end: a scripted device -> rules -> notifications, with a fake clock."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import pytest

from labwatch import engine
from labwatch.config import Config, ConfigError, Section
from labwatch.devices.base import Device, DeviceUnreachable, register
from labwatch.devices.bluefors import DEFAULT_CHANNELS
from labwatch.engine import Monitor, build_runtime, fmt_duration
from labwatch.models import AlertEvent, EventKind, Reading, Severity
from labwatch.notifiers.base import Notifier
from labwatch.rules import RuleConfigError
from labwatch.state import StateStore

T0 = 1_800_000_000.0

DEFAULTS = {"for": 60, "clear_for": 120, "auto_arm": True, "arm_after": "10m", "repeat_interval": "2h"}
RULES = [
    {"channel": "pulse_tube", "not_equals": 1, "severity": "critical", "message": "Pulse tube is not running!"},
    {"channel": "T4K", "above": 5, "hysteresis": 0.3, "severity": "critical"},
]
WARM = {"T50K": 295.0, "T4K": 290.0, "pulse_tube": 0.0, "compressor": 0.0}
COLD = {"T50K": 45.0, "T4K": 3.2, "pulse_tube": 1.0, "compressor": 1.0}
PT_RULE = "Fridge/pulse_tube is not on"
T4K_RULE = "Fridge/T4K above 5 K"
UNREACHABLE = "Fridge/unreachable"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeClock:
    """Replaces the ``time`` module inside labwatch.engine."""

    def __init__(self, t: float = T0) -> None:
        self.t = t

    def time(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t


@register("scripted")
class ScriptedDevice(Device):
    """Returns the values the test put in ``values``; raises ``error`` if set."""

    summary_channels = ("T4K", "pulse_tube")

    def __init__(self, name: str, clock: FakeClock, unreachable_after: float = 300.0) -> None:
        channels = {k: DEFAULT_CHANNELS[k] for k in ("T50K", "T4K", "pulse_tube", "compressor")}
        super().__init__(name, channels, poll_interval=30.0, unreachable_after=unreachable_after)
        self.clock = clock
        self.values: dict[str, float | None] = {}
        self.error: Exception | None = None
        self.polls = 0

    @classmethod
    def from_config(cls, name, cfg):
        return cls(name, cfg["clock"], cfg.get("unreachable_after", 300.0))

    async def poll(self) -> list[Reading]:
        self.polls += 1
        if self.error is not None:
            raise self.error
        ts = datetime.fromtimestamp(self.clock.t, timezone.utc)
        out = []
        for ch, v in self.values.items():
            spec = self.channels[ch]
            out.append(Reading(self.name, ch, spec.source, v, ts, spec.unit, raw=str(v), status="OK",
                               label=spec.label_for(v)))
        return out


class RecordingNotifier(Notifier):
    def __init__(self, name: str = "inbox", min_severity: Severity = Severity.INFO) -> None:
        super().__init__(name, min_severity)
        self.events: list[AlertEvent] = []

    async def send(self, event: AlertEvent) -> None:
        self.events.append(event)

    @classmethod
    def from_config(cls, name, min_severity, cfg):
        return cls(name, min_severity)


class FailingNotifier(RecordingNotifier):
    def __init__(self, name: str = "broken") -> None:
        super().__init__(name)
        self.attempts = 0

    async def send(self, event: AlertEvent) -> None:
        self.attempts += 1
        raise RuntimeError("webhook down")


class HangingNotifier(RecordingNotifier):
    async def send(self, event: AlertEvent) -> None:
        await asyncio.sleep(3600)


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    c = FakeClock()
    monkeypatch.setattr(engine, "time", c)
    return c


def make_config(tmp_path, rule_defaults=None) -> Config:
    return Config(
        path=tmp_path / "config.yaml", state_db=tmp_path / "state.sqlite", history_days=30,
        rule_defaults=dict(DEFAULTS if rule_defaults is None else rule_defaults), devices=[], notifiers=[],
        heartbeat_url=None, heartbeat_interval=60.0, daily_summary=None, notify_startup=False, secrets=set(),
    )


async def drain(monitor: Monitor) -> None:
    while not monitor._queue.empty():
        await monitor.dispatch(monitor._queue.get_nowait())


class Harness:
    """One scripted fridge in a Monitor, polled every 30 s of fake time."""

    def __init__(self, tmp_path, clock: FakeClock, store: StateStore | None = None, rules=RULES,
                 defaults=None, unreachable_after: float = 300.0) -> None:
        self.clock = clock
        self.config = make_config(tmp_path, defaults)
        sec = Section({"name": "Fridge", "type": "scripted", "clock": clock, "rules": rules,
                       "unreachable_after": unreachable_after}, set())
        self.config.devices.append(sec)
        self.rt = build_runtime(self.config, sec)
        self.dev: ScriptedDevice = self.rt.device
        self.store = store if store is not None else StateStore(":memory:")
        self.inbox = RecordingNotifier()
        self.monitor = Monitor(self.config, [self.rt], [self.inbox], self.store)
        self.rt.unreachable.started = clock.t  # what Monitor.run() does

    def rule(self, rule_id: str):
        return next(r for r in self.rt.all_rules if r.rule_id == rule_id)

    def poll(self, dt: float = 30.0) -> list[AlertEvent]:
        """Advance the clock, poll once, deliver what was queued; return what the inbox received."""
        self.clock.t += dt
        n = len(self.inbox.events)

        async def go():
            await self.monitor.poll_once(self.rt)
            await drain(self.monitor)

        asyncio.run(go())
        return self.inbox.events[n:]

    def run_for(self, seconds: float, dt: float = 30.0) -> list[AlertEvent]:
        events: list[AlertEvent] = []
        for _ in range(round(seconds / dt)):
            events += self.poll(dt)
        return events

    def poll_until(self, pred, limit: float = 6 * 3600) -> list[AlertEvent]:
        """Poll until an event matching ``pred`` arrives; return all events up to and including it."""
        events: list[AlertEvent] = []
        start = self.clock.t
        while self.clock.t - start < limit:
            events += self.poll()
            if any(pred(e) for e in events):
                return events
        raise AssertionError(f"no matching event within {limit} s; got {[e.headline for e in events]}")

    def cool_down_and_arm(self) -> None:
        self.dev.values = dict(COLD)
        events = self.run_for(630)
        assert [e.title for e in events] == ["Fridge: alarms armed"]


def kinds(events):
    return [(e.kind, e.rule_id) for e in events]


@pytest.fixture
def h(tmp_path, clock) -> Harness:
    return Harness(tmp_path, clock)


# ---------------------------------------------------------------------------
# the normal life of a fridge
# ---------------------------------------------------------------------------


def test_warm_fridge_produces_no_alerts(h):
    h.dev.values = dict(WARM)  # pulse tube off, T4K at room temperature
    assert h.run_for(3 * 3600) == []
    assert [r.status_text() for r in h.rt.rules] == ["disarmed", "disarmed"]
    assert h.rt.unreachable.state.armed and not h.rt.unreachable.state.firing
    assert {r["channel"]: r["value"] for r in h.store.latest("Fridge")}["T4K"] == 290.0


def test_cooldown_arms_alarms_once_after_arm_after(h):
    h.dev.values = dict(WARM)
    h.run_for(3600)
    h.dev.values = dict(COLD)
    cold_since = h.clock.t + 30
    assert h.run_for(600) == []  # cold polls at 0 ... 570 s: not yet
    (ev,) = h.poll()
    assert h.clock.t - cold_since == 600
    assert ev.kind is EventKind.INFO and ev.severity is Severity.INFO
    assert ev.rule_id == "Fridge/armed" and ev.title == "Fridge: alarms armed"
    assert "- pulse_tube is not on\n- T4K above 5 K" in ev.message
    assert "Now: T4K 3.2 K | pulse_tube on" in ev.message
    assert [r.status_text() for r in h.rt.rules] == ["armed", "armed"]
    assert h.run_for(3600) == []  # and stays quiet


def test_pulse_tube_stop_fires_once_reminds_and_resolves(h):
    h.cool_down_and_arm()
    h.dev.values["pulse_tube"] = 0.0  # power outage: the pulse tube stops

    assert h.poll() == [] and h.poll() == []  # pending for 0 s, 30 s
    (ev,) = h.poll()  # 60 s = `for`
    assert (ev.kind, ev.severity, ev.rule_id) == (EventKind.FIRING, Severity.CRITICAL, PT_RULE)
    assert ev.title == "Fridge: pulse_tube is not on"
    assert ev.headline == "[CRITICAL] Fridge: pulse_tube is not on"
    assert ev.channel == "pulse_tube" and ev.value == 0.0
    lines = ev.message.splitlines()
    assert lines[:2] == ["pulse_tube = off", "Pulse tube is not running!"]
    assert lines[2].startswith("Since ")
    assert "Now: T4K 3.2 K | pulse_tube off" in ev.message
    assert 'labwatch mute "Fridge" --for 2h' in ev.message
    assert ev.since is not None
    fired_at = h.clock.t

    # still off: nothing until the reminder after repeat_interval (2 h)
    assert h.run_for(7200 - 30) == []
    (rem,) = h.poll()
    assert h.clock.t - fired_at == 7200
    assert (rem.kind, rem.rule_id) == (EventKind.REMINDER, PT_RULE)
    assert rem.headline == "[STILL CRITICAL] Fridge: pulse_tube is not on"
    assert "Firing for 2.0 h" in rem.message

    # power back: resolved after clear_for (120 s)
    h.dev.values["pulse_tube"] = 1.0
    assert h.run_for(120) == []
    (res,) = h.poll()
    assert (res.kind, res.rule_id, res.severity) == (EventKind.RESOLVED, PT_RULE, Severity.CRITICAL)
    assert res.message.startswith("Back to normal: pulse_tube = on")
    assert "mute" not in res.message
    assert h.run_for(3600) == []


def test_warmup_fires_threshold_rule_with_hysteresis(h):
    h.cool_down_and_arm()
    h.dev.values["T4K"] = 5.5
    events = h.run_for(90)
    assert kinds(events) == [(EventKind.FIRING, T4K_RULE)]
    assert events[0].unit == "K" and events[0].value == 5.5
    h.dev.values["T4K"] = 4.9  # below the limit, but within the 0.3 K hysteresis
    assert h.run_for(3600) == []
    h.dev.values["T4K"] = 4.5
    assert kinds(h.run_for(150)) == [(EventKind.RESOLVED, T4K_RULE)]


def test_flapping_value_does_not_resolve_or_refire(h):
    h.cool_down_and_arm()
    h.dev.values["pulse_tube"] = 0.0
    assert kinds(h.run_for(90)) == [(EventKind.FIRING, PT_RULE)]
    events = []
    for i in range(60):  # 30 min of on/off/on/off
        h.dev.values["pulse_tube"] = float(i % 2)
        events += h.poll()
    assert events == []
    assert h.rule(PT_RULE).state.firing


# ---------------------------------------------------------------------------
# unreachable device
# ---------------------------------------------------------------------------


def test_unreachable_device_fires_and_freezes_other_rules(h, monkeypatch):
    h.cool_down_and_arm()
    h.dev.values["pulse_tube"] = 0.0
    assert h.poll() == []  # pulse tube rule now pending...
    pt = h.rule(PT_RULE)
    pending_since = pt.state.pending_since
    checks = []
    original_check = pt.check
    monkeypatch.setattr(pt, "check", lambda ctx: checks.append(ctx.now) or original_check(ctx))

    # ... and then the control PC goes down
    h.dev.error = DeviceUnreachable("Fridge: cannot connect to fridge-pc:49098 (ConnectError)")
    last_ok = h.clock.t
    events = []
    while h.clock.t - last_ok < 300:  # unreachable_after
        events += h.poll()
    assert events == []
    events = h.poll_until(lambda e: e.rule_id == UNREACHABLE)
    down_for = h.clock.t - last_ok
    assert 300 < down_for <= 300 + 30  # unreachable_after + one poll (no extra `for` delay)

    (ev,) = events
    assert (ev.kind, ev.severity, ev.title) == (EventKind.FIRING, Severity.CRITICAL, "Fridge: unreachable")
    assert ev.channel is None
    assert "last error: Fridge: cannot connect to fridge-pc:49098 (ConnectError)" in ev.message
    assert "Can't reach the control software" in ev.message
    assert "Last known values (5 min old): T4K 3.2 K | pulse_tube off" in ev.message
    assert h.store.devices()["Fridge"]["last_error"].startswith("Fridge: cannot connect")

    # the stale "pulse tube off" reading was never re-evaluated during the outage
    assert checks == []
    assert pt.state.pending_since == pending_since and not pt.state.firing

    # the PC is back (and so is the power)
    h.dev.error = None
    h.dev.values = dict(COLD)
    events = h.poll()
    assert kinds(events) == [(EventKind.RESOLVED, UNREACHABLE)]
    assert len(checks) == 1
    assert h.run_for(3600) == []


def test_unreachable_since_start_when_never_successful(h):
    h.dev.error = DeviceUnreachable("Fridge: timeout talking to fridge-pc:49098")
    (ev,) = h.poll_until(lambda e: e.rule_id == UNREACHABLE)
    assert "no successful poll since labwatch started" in ev.message
    assert "Last known values" not in ev.message


def test_restart_while_device_still_unreachable_does_not_resolve(tmp_path, clock):
    db = tmp_path / "state.sqlite"
    h1 = Harness(tmp_path, clock, store=StateStore(db))
    h1.cool_down_and_arm()
    h1.dev.error = DeviceUnreachable("Fridge: cannot connect to fridge-pc:49098 (ConnectError)")
    h1.poll_until(lambda e: e.rule_id == UNREACHABLE)
    h1.store.close()

    # labwatch restarts (e.g. the monitoring PC rebooted after the same power outage)
    clock.t += 120
    h2 = Harness(tmp_path, clock, store=StateStore(db))
    h2.monitor._restore_state()
    assert h2.rt.unreachable.state.firing and h2.rt.unreachable.state.delivered
    h2.dev.error = DeviceUnreachable("Fridge: cannot connect to fridge-pc:49098 (ConnectError)")
    events = h2.run_for(600)
    assert (EventKind.RESOLVED, UNREACHABLE) not in kinds(events)


def test_crashing_driver_still_raises_unreachable_alert(h):
    h.cool_down_and_arm()
    h.dev.error = RuntimeError("driver bug: unexpected payload")
    events = h.run_for(900)
    assert (EventKind.FIRING, UNREACHABLE) in kinds(events)


# ---------------------------------------------------------------------------
# mutes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["Fridge", PT_RULE, "*"])
def test_mute_suppresses_firing_until_it_expires(h, target):
    h.cool_down_and_arm()
    until = h.clock.t + 3 * 3600  # longer than repeat_interval: no reminders either
    h.store.mute(target, until, "warming up for maintenance")
    h.dev.values["pulse_tube"] = 0.0
    events = []
    while h.clock.t + 30 < until:
        events += h.poll()
    assert events == []
    st = h.rule(PT_RULE).state
    assert st.firing and not st.delivered
    assert h.rule(PT_RULE).status_text() == "FIRING"

    (ev,) = h.poll()  # first poll after the mute expired
    assert h.clock.t >= until
    assert (ev.kind, ev.rule_id) == (EventKind.FIRING, PT_RULE)
    assert st.delivered
    # later reminders count from this delivery
    assert h.run_for(7200 - 30) == []
    assert kinds(h.poll()) == [(EventKind.REMINDER, PT_RULE)]


def test_alert_that_resolves_while_muted_is_never_sent(h):
    h.cool_down_and_arm()
    until = h.clock.t + 3600
    h.store.mute("Fridge", until)
    h.dev.values["pulse_tube"] = 0.0
    assert h.run_for(600) == []
    assert h.rule(PT_RULE).state.firing
    h.dev.values["pulse_tube"] = 1.0
    assert h.run_for(600) == []
    assert not h.rule(PT_RULE).state.firing
    assert h.run_for(3600) == []  # no FIRING and no RESOLVED, also after the mute ended


def test_muting_other_rule_or_device_does_not_suppress(h):
    h.cool_down_and_arm()
    h.store.mute(T4K_RULE, None)
    h.store.mute("OtherFridge", None)
    h.dev.values["pulse_tube"] = 0.0
    assert kinds(h.run_for(90)) == [(EventKind.FIRING, PT_RULE)]


def test_muted_device_gets_no_armed_info(h):
    h.store.mute("Fridge", None)
    h.dev.values = dict(COLD)
    assert h.run_for(3600) == []
    assert [r.status_text() for r in h.rt.rules] == ["armed", "armed"]


def test_unmute_while_firing_sends_alert(h):
    h.cool_down_and_arm()
    h.store.mute("*", None)  # indefinitely
    h.dev.values["pulse_tube"] = 0.0
    assert h.run_for(4 * 3600) == []
    h.store.unmute("*")
    assert kinds(h.poll()) == [(EventKind.FIRING, PT_RULE)]


# ---------------------------------------------------------------------------
# persistence across restarts
# ---------------------------------------------------------------------------


def restart(tmp_path, clock, old: Harness, **kw) -> Harness:
    old.store.close()
    new = Harness(tmp_path, clock, store=StateStore(tmp_path / "state.sqlite"), **kw)
    new.monitor._restore_state()
    return new


def test_firing_state_survives_restart(tmp_path, clock):
    h1 = Harness(tmp_path, clock, store=StateStore(tmp_path / "state.sqlite"))
    h1.cool_down_and_arm()
    h1.dev.values["pulse_tube"] = 0.0
    assert kinds(h1.run_for(90)) == [(EventKind.FIRING, PT_RULE)]

    clock.t += 60
    h2 = restart(tmp_path, clock, h1)
    st = h2.rule(PT_RULE).state
    assert (st.armed, st.firing, st.delivered) == (True, True, True)
    h2.dev.values = dict(COLD, pulse_tube=0.0)
    assert h2.run_for(1800) == []  # no duplicate FIRING after the restart
    h2.dev.values["pulse_tube"] = 1.0
    assert kinds(h2.run_for(150)) == [(EventKind.RESOLVED, PT_RULE)]


def test_armed_state_survives_restart_no_new_arm_wait(tmp_path, clock):
    h1 = Harness(tmp_path, clock, store=StateStore(tmp_path / "state.sqlite"))
    h1.cool_down_and_arm()
    clock.t += 3600
    h2 = restart(tmp_path, clock, h1)
    assert [r.status_text() for r in h2.rt.rules] == ["armed", "armed"]
    h2.dev.values = dict(COLD, pulse_tube=0.0)
    assert kinds(h2.run_for(90)) == [(EventKind.FIRING, PT_RULE)]


def test_restore_arms_rules_that_are_now_auto_arm_false(tmp_path, clock):
    h1 = Harness(tmp_path, clock, store=StateStore(tmp_path / "state.sqlite"))
    h1.dev.values = dict(WARM)
    h1.run_for(300)
    assert not h1.rule(PT_RULE).state.armed  # saved as disarmed
    rules = [dict(RULES[0], auto_arm=False), RULES[1]]
    h2 = restart(tmp_path, clock, h1, rules=rules)
    assert h2.rule(PT_RULE).state.armed
    assert not h2.rule(T4K_RULE).state.armed


RATE_RULES = [{"channel": "T4K", "rate_above": 0.05, "window": "10m", "auto_arm": False}]


def test_restore_seeds_history_for_rate_rules(tmp_path, clock):
    h1 = Harness(tmp_path, clock, store=StateStore(tmp_path / "state.sqlite"), rules=RATE_RULES)
    for i in range(20):  # warming at 0.1 K/min
        h1.dev.values = {"T4K": 3.0 + 0.05 * i}
        h1.poll()
    clock.t += 30
    h2 = restart(tmp_path, clock, h1, rules=RATE_RULES)
    assert len(h2.rt.history.window("T4K", 600, clock.t)) >= 10
    rate = h2.rt.rules[0]
    h2.dev.values = {"T4K": 4.05}
    h2.poll()
    assert rate.state.last_value == pytest.approx(0.1, rel=0.05)  # verdict right after the restart


def test_restored_history_has_no_duplicate_timestamps(tmp_path, clock):
    store = StateStore(tmp_path / "state.sqlite")
    for ts, value in ((clock.t - 600, 3.0), (clock.t - 120, 5.0)):
        r = Reading("Fridge", "T4K", "src", value, datetime.fromtimestamp(ts, timezone.utc), "K")
        for k in range(3):  # the controller hadn't updated: same sample on three polls
            store.save_readings([r], polled_at=ts + 30 * k)
    h = Harness(tmp_path, clock, store=store, rules=RATE_RULES)
    h.monitor._restore_state()
    pts = h.rt.history.window("T4K", 600, clock.t)
    assert len(pts) == len({t for t, _ in pts})


def test_user_rule_named_unreachable_is_fine_on_other_devices(tmp_path, clock):
    rules = [{"channel": "pulse_tube", "not_equals": 1, "name": "pulse tube stopped"}]
    h = Harness(tmp_path, clock, rules=rules)
    assert [r.rule_id for r in h.rt.all_rules] == [UNREACHABLE, "Fridge/pulse tube stopped"]


def test_user_rule_cannot_shadow_builtin_unreachable_rule(tmp_path, clock):
    rules = [{"channel": "pulse_tube", "not_equals": 1, "name": "unreachable"}]
    with pytest.raises((RuleConfigError, ConfigError)):
        Harness(tmp_path, clock, rules=rules)


# ---------------------------------------------------------------------------
# notification dispatch
# ---------------------------------------------------------------------------


def info_event(severity: Severity = Severity.CRITICAL) -> AlertEvent:
    return AlertEvent(EventKind.FIRING, PT_RULE, "Fridge", "Fridge: pulse_tube is not on", "pulse_tube = off",
                      severity)


def test_failing_notifier_does_not_block_others(h, monkeypatch):
    monkeypatch.setattr(engine, "RETRY_DELAYS", (0,))
    bad, good = FailingNotifier("broken"), RecordingNotifier("good")
    h.monitor.notifiers = [bad, good]
    delivery = asyncio.run(h.monitor.dispatch(info_event()))
    assert delivery == {"broken": "failed: RuntimeError: webhook down", "good": "ok"}
    assert bad.attempts == 2  # first try + one retry
    assert [e.title for e in good.events] == ["Fridge: pulse_tube is not on"]
    (row,) = h.store.events(1)
    assert (row["kind"], row["rule_id"], row["severity"]) == ("firing", PT_RULE, "critical")
    assert json.loads(row["delivery"]) == delivery


def test_hanging_notifier_times_out_others_still_deliver(h, monkeypatch):
    monkeypatch.setattr(engine, "SEND_TIMEOUT", 0.05)
    monkeypatch.setattr(engine, "RETRY_DELAYS", ())
    good = RecordingNotifier("good")
    h.monitor.notifiers = [HangingNotifier("slow"), good]
    delivery = asyncio.run(h.monitor.dispatch(info_event()))
    assert delivery["slow"].startswith("failed: TimeoutError")
    assert delivery["good"] == "ok" and len(good.events) == 1


def test_notifiers_filter_by_severity(h):
    phone = RecordingNotifier("phone", Severity.CRITICAL)  # lifecycle on (default)
    pager = RecordingNotifier("pager", Severity.CRITICAL)
    pager.lifecycle = False
    h.monitor.notifiers = [phone, pager, h.inbox]
    h.cool_down_and_arm()  # INFO "alarms armed": a lifecycle message
    h.dev.values["pulse_tube"] = 0.0
    h.run_for(90)
    assert [e.kind for e in h.inbox.events] == [EventKind.INFO, EventKind.FIRING]
    assert [e.title for e in phone.events] == ["Fridge: alarms armed", "Fridge: pulse_tube is not on"]
    assert [e.kind for e in pager.events] == [EventKind.FIRING]
    info_row = h.store.events(10)[-1]
    assert info_row["kind"] == "info" and json.loads(info_row["delivery"]) == {"phone": "ok", "inbox": "ok"}


def test_warning_rules_skip_critical_only_notifier(tmp_path, clock):
    rules = [{"channel": "T4K", "above": 5, "severity": "warning"}]
    h = Harness(tmp_path, clock, rules=rules)
    phone = RecordingNotifier("phone", Severity.CRITICAL)
    h.monitor.notifiers = [phone, h.inbox]
    h.cool_down_and_arm()
    h.dev.values["T4K"] = 6.0
    h.run_for(90)
    h.dev.values["T4K"] = 3.2
    h.run_for(180)
    assert [e.kind for e in phone.events] == [EventKind.INFO]  # only "alarms armed"


def test_run_sends_startup_and_shutdown_to_lifecycle_notifiers(tmp_path):
    """Real clock: Monitor.run() until cancelled, like `labwatch run` + Ctrl-C."""
    h = Harness(tmp_path, FakeClock(time.time()))  # engine.time is *not* patched here
    h.config.notify_startup = True
    telegram = RecordingNotifier("telegram", Severity.WARNING)
    quiet = RecordingNotifier("quiet", Severity.WARNING)
    quiet.lifecycle = False
    h.monitor.notifiers = [telegram, quiet]
    h.dev.values = dict(COLD)

    async def go():
        task = asyncio.create_task(h.monitor.run())
        for _ in range(200):
            if telegram.events and h.dev.polls:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert [(e.rule_id, e.title) for e in telegram.events] == [
        ("labwatch/startup", "labwatch started"), ("labwatch/shutdown", "labwatch stopped")]
    assert "Fridge (2 rules)" in telegram.events[0].message
    assert quiet.events == []
    assert h.store.load_rule_states()  # state saved on shutdown


def test_events_without_any_notifier_are_still_logged(h):
    h.monitor.notifiers = []
    assert asyncio.run(h.monitor.dispatch(info_event())) == {}
    assert h.store.events(1)[0]["delivery"] == "{}"


def test_dispatch_worker_delivers_queue_and_survives_errors(h, monkeypatch):
    calls = {"n": 0}
    real_accepts = h.inbox.accepts

    def flaky_accepts(ev):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("bug in a notifier")
        return real_accepts(ev)

    monkeypatch.setattr(h.inbox, "accepts", flaky_accepts)

    async def go():
        worker = asyncio.create_task(h.monitor._dispatch_worker())
        for title in ("first", "second", "third"):
            h.monitor.emit(AlertEvent(EventKind.FIRING, "r", "Fridge", title, "", Severity.WARNING))
        for _ in range(100):
            if len(h.inbox.events) == 2:
                break
            await asyncio.sleep(0.01)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(go())
    assert [e.title for e in h.inbox.events] == ["second", "third"]


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------


def test_summary_event(h):
    h.cool_down_and_arm()
    h.dev.values["pulse_tube"] = 0.0
    h.run_for(90)
    h.store.mute("Fridge", None)
    ev = h.monitor.summary_event()
    assert ev.kind is EventKind.INFO and ev.severity is Severity.INFO
    assert "Fridge: 1 alert(s) firing; 2/2 alarms armed; last data 0 s ago" in ev.message
    assert "FIRING: pulse_tube is not on (pulse_tube = off)" in ev.message
    assert "Muted: Fridge" in ev.message


@pytest.mark.parametrize(
    "seconds, text",
    [(0, "0 s"), (89, "89 s"), (90, "1 min"), (5399, "89 min"), (5400, "1.5 h"), (2 * 86400, "2.0 days"),
     (-5, "0 s")],
)
def test_fmt_duration(seconds, text):
    assert fmt_duration(seconds) == text
