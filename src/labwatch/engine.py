"""The monitor: polls devices, evaluates rules, dispatches notifications."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from labwatch.computed import compute_reading
from labwatch.config import Config, ConfigError
from labwatch.devices import Device, DeviceError, build_device
from labwatch.models import AlertEvent, EventKind, Reading, Severity, utcnow
from labwatch.notifiers import Notifier, build_notifier
from labwatch.rules import (
    Context,
    History,
    Rule,
    RuleConfigError,
    RuleState,
    SteadyState,
    Transition,
    UnreachableRule,
    build_options,
    build_rules,
)
from labwatch.state import Mute, StateStore

log = logging.getLogger(__name__)

RETRY_DELAYS = (5.0, 20.0, 60.0)  # seconds between notification attempts
SEND_TIMEOUT = 30.0


def fmt_time(ts: float | None) -> str:
    if ts is None:
        return "?"
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    if seconds < 86400 * 2:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


@dataclass
class DeviceRuntime:
    device: Device
    rules: list[Rule]
    unreachable: UnreachableRule
    history: History = field(default_factory=History)
    latest: dict[str, Reading] = field(default_factory=dict)
    last_poll_ok: bool | None = None
    steady: SteadyState | None = None

    def context(self, now: float) -> Context:
        ctx = Context(now=now, readings=self.latest, history=self.history)
        if self.steady is not None:
            ctx.steady_ready, ctx.steady_ready_at, ctx.steady_detail = self.steady.evaluate(
                self.latest, self.history, now
            )
        return ctx

    def add_computed(self, now: float) -> list[Reading]:
        """Evaluate computed channels from the latest readings; returns the new readings."""
        out = []
        for spec in self.device.channels.values():
            if spec.computed:
                r = compute_reading(self.name, spec, self.latest)
                self.latest[spec.name] = r
                self.history.add(r, now)
                out.append(r)
        return out

    @property
    def name(self) -> str:
        return self.device.name

    @property
    def all_rules(self) -> list[Rule]:
        return [self.unreachable, *self.rules]

    def snapshot(self) -> str:
        names = self.device.summary_channels or tuple(self.latest)[:8]
        parts = [f"{n} {self.latest[n].format_value()}" for n in names if n in self.latest]
        return " | ".join(parts)


class Monitor:
    def __init__(
        self,
        config: Config,
        runtimes: list[DeviceRuntime],
        notifiers: list[Notifier],
        store: StateStore,
    ) -> None:
        self.config = config
        self.runtimes = runtimes
        self.notifiers = notifiers
        self.store = store
        self._queue: asyncio.Queue[AlertEvent] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._device_tasks: list[asyncio.Task] = []
        self._dispatching = 0

    # -- construction ------------------------------------------------------

    @classmethod
    def from_config(cls, config: Config, store: StateStore | None = None, with_notifiers: bool = True) -> Monitor:
        runtimes = [build_runtime(config, sec) for sec in config.devices]
        if not runtimes:
            raise ConfigError(f"{config.path}: no devices configured")
        notifiers: list[Notifier] = []
        if with_notifiers:
            for sec in config.notifiers:
                sec.require_env("notifier")
                notifiers.append(build_notifier(sec.cfg))
        store = store or StateStore(config.state_db)
        mon = cls(config, runtimes, notifiers, store)
        mon._restore_state()
        return mon

    def _restore_state(self) -> None:
        saved = self.store.load_rule_states()
        devices = self.store.devices()
        since = time.time() - 3600
        for rt in self.runtimes:
            for rule in rt.all_rules:
                if rule.rule_id in saved:
                    rule.state = RuleState.from_dict(saved[rule.rule_id])
                    if not rule.opts.auto_arm:
                        rule.state.armed = True
            # Remember when the device last answered, so a restart during an outage
            # doesn't look like a recovery (false "RESOLVED unreachable").
            dev = devices.get(rt.name)
            if dev is not None and dev["last_success"] is not None:
                rt.unreachable.last_success = dev["last_success"]
            # Seed history so rate/anomaly rules and the steady-state gate work right after
            # a restart (the table has one row per poll; seed() dedupes).
            for ch in rt.device.channels:
                rt.history.seed(ch, self.store.history(rt.name, ch, time.time() - rt.history.keep))

    def _save_state(self) -> None:
        self.store.save_rule_states(
            (r.rule_id, rt.name, r.state.to_dict()) for rt in self.runtimes for r in rt.all_rules
        )

    # -- main loop -----------------------------------------------------------

    async def run(self) -> None:
        start = time.time()
        for rt in self.runtimes:
            rt.unreachable.started = start
        self._tasks.append(asyncio.create_task(self._dispatch_worker(), name="dispatch"))
        for rt in self.runtimes:
            t = asyncio.create_task(self._device_loop(rt), name=f"poll:{rt.name}")
            self._device_tasks.append(t)
        self._tasks.extend(self._device_tasks)
        if self.config.heartbeat_url:
            self._tasks.append(asyncio.create_task(self._heartbeat_loop(), name="heartbeat"))
        if self.config.daily_summary:
            self._tasks.append(asyncio.create_task(self._daily_summary_loop(), name="summary"))
        self._tasks.append(asyncio.create_task(self._maintenance_loop(), name="maintenance"))
        for bot in self.command_bots():
            self._tasks.append(asyncio.create_task(bot.run(), name="telegram-commands"))

        if self.config.notify_startup:
            names = ", ".join(f"{rt.name} ({len(rt.rules)} rules)" for rt in self.runtimes)
            self.emit(AlertEvent(EventKind.INFO, "labwatch/startup", "labwatch",
                                 "labwatch started", f"Monitoring: {names}", Severity.INFO))
        try:
            await asyncio.gather(*self._tasks)
        finally:
            await self.shutdown()

    def command_bots(self) -> list:
        """Telegram command listeners for Telegram notifiers with ``commands: true`` (one per bot)."""
        from labwatch.notifiers.telegram import TelegramNotifier
        from labwatch.telegram_bot import TelegramCommandBot

        bots, seen = [], set()
        for n in self.notifiers:
            if isinstance(n, TelegramNotifier) and n.commands and n._bot_token not in seen:
                seen.add(n._bot_token)
                bots.append(TelegramCommandBot.from_notifier(self, n))
        return bots

    async def shutdown(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        # Best effort: tell humans monitoring stopped, and flush what's queued.
        if self.config.notify_startup and self.notifiers:
            ev = AlertEvent(EventKind.INFO, "labwatch/shutdown", "labwatch", "labwatch stopped",
                            "Monitoring is OFF until labwatch is started again.", Severity.INFO)
            self._queue.put_nowait(ev)
        while not self._queue.empty():
            ev = self._queue.get_nowait()
            try:
                await asyncio.wait_for(self.dispatch(ev, retries=False), timeout=SEND_TIMEOUT)
            except Exception:  # noqa: BLE001
                log.exception("failed to flush %s", ev.title)
        self._save_state()
        self.store.checkpoint()
        for rt in self.runtimes:
            await rt.device.aclose()
        for n in self.notifiers:
            try:
                await n.aclose()
            except Exception:  # noqa: BLE001
                pass

    async def _device_loop(self, rt: DeviceRuntime) -> None:
        while True:
            t0 = time.monotonic()
            try:
                await self.poll_once(rt)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never let one bad poll kill monitoring
                log.exception("%s: unexpected error while polling", rt.name)
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(1.0, rt.device.poll_interval - elapsed))

    async def poll_once(self, rt: DeviceRuntime) -> list[AlertEvent]:
        """Poll one device, evaluate its rules and queue notifications."""
        try:
            readings = await rt.device.poll()
        except (DeviceError, Exception) as e:  # noqa: BLE001 - a crashing driver must still alert
            if not isinstance(e, DeviceError):
                log.exception("%s: driver crashed while polling", rt.name)
                e = DeviceError(f"unexpected error in the {rt.device.type_name} driver: {type(e).__name__}")
            now = time.time()
            if rt.last_poll_ok is not False:
                log.warning("%s: poll failed: %s", rt.name, e)
            rt.last_poll_ok = False
            rt.unreachable.last_error = str(e)
            self.store.device_error(rt.name, now, str(e))
            # Only the connection rule is meaningful without fresh data.
            return self.evaluate(rt, now, rules=[rt.unreachable])
        now = time.time()
        if rt.last_poll_ok is False:
            log.info("%s: polling works again", rt.name)
        rt.last_poll_ok = True
        rt.unreachable.last_success = now
        for r in readings:
            rt.latest[r.channel] = r
            rt.history.add(r, now)
        readings = [*readings, *rt.add_computed(now)]
        self.store.save_readings(readings, now)
        self.store.device_ok(rt.name, now)
        return self.evaluate(rt, now)

    # -- rule evaluation -------------------------------------------------------

    def evaluate(self, rt: DeviceRuntime, now: float, rules: list[Rule] | None = None) -> list[AlertEvent]:
        ctx = rt.context(now)
        mutes = self.store.mutes(now)
        events: list[AlertEvent] = []
        armed: list[Rule] = []
        for rule in rules if rules is not None else rt.all_rules:
            verdict, tr = rule.step(ctx)
            st = rule.state
            muted = _muted_by(mutes, rule)
            if tr == Transition.ARMED:
                log.info("%s: armed", rule.rule_id)
                armed.append(rule)
            elif tr == Transition.FIRING:
                log.warning("%s: FIRING (%s)%s", rule.rule_id, verdict.detail, " [muted]" if muted else "")
            elif tr == Transition.RESOLVED:
                log.info("%s: resolved (%s)", rule.rule_id, verdict.detail)
            if st.firing and not st.delivered:
                if muted:
                    continue
                # New alert, or an alert that started while muted and is still active.
                st.delivered = True
                st.last_notified = now
                events.append(self._make_event(rt, rule, EventKind.FIRING, verdict.detail, now))
            elif tr == Transition.REMINDER and not muted:
                events.append(self._make_event(rt, rule, EventKind.REMINDER, verdict.detail, now))
            elif tr == Transition.RESOLVED and not muted:
                events.append(self._make_event(rt, rule, EventKind.RESOLVED, verdict.detail, now))
        if armed and not _muted_device(mutes, rt.name):
            lines = "\n".join(f"- {r.name}" for r in armed)
            events.append(AlertEvent(
                EventKind.INFO, f"{rt.name}/armed", rt.name, f"{rt.name}: alarms armed",
                f"These alarms are now active:\n{lines}\n\nNow: {rt.snapshot()}", Severity.INFO,
            ))
        self._save_state()
        for ev in events:
            self.emit(ev)
        return events

    def _make_event(self, rt: DeviceRuntime, rule: Rule, kind: EventKind, detail: str, now: float) -> AlertEvent:
        st = rule.state
        lines = [detail] if detail else []
        if kind is EventKind.RESOLVED:
            lines = [f"Back to normal: {detail}"]
        elif rule.opts.message:
            lines.append(rule.opts.message.strip())
        if rule.opts.detail_channel and kind is not EventKind.RESOLVED:
            r = rt.latest.get(rule.opts.detail_channel)
            if r is not None and r.raw and r.raw.strip():
                lines.append(f"{rule.opts.detail_channel}: {r.raw.strip()}")
        since = st.pending_since if kind is not EventKind.RESOLVED else None
        if kind is EventKind.REMINDER and st.fired_at:
            lines.append(f"Firing for {fmt_duration(now - st.fired_at)} (since {fmt_time(st.fired_at)}).")
        elif since:
            lines.append(f"Since {fmt_time(since)}.")
        snap = rt.snapshot()
        if snap:
            if isinstance(rule, UnreachableRule):
                age = now - rt.unreachable.last_success if rt.unreachable.last_success else None
                if age is not None:
                    lines.append(f"\nLast known values ({fmt_duration(age)} old): {snap}")
            else:
                lines.append(f"\nNow: {snap}")
        if kind in (EventKind.FIRING, EventKind.REMINDER):
            lines.append(f"Silence with: labwatch mute \"{rt.name}\" --for 2h")
        spec = rt.device.channels.get(rule.channel)
        return AlertEvent(
            kind=kind,
            rule_id=rule.rule_id,
            device=rt.name,
            title=rule.title,
            message="\n".join(lines),
            severity=rule.opts.severity,
            channel=None if isinstance(rule, UnreachableRule) else rule.channel,
            value=st.last_value,
            unit=spec.unit if spec else None,
            since=datetime.fromtimestamp(since).astimezone() if since else None,
        )

    # -- notification dispatch ---------------------------------------------------

    def emit(self, ev: AlertEvent) -> None:
        self._queue.put_nowait(ev)

    async def _dispatch_worker(self) -> None:
        while True:
            ev = await self._queue.get()
            try:
                await self.dispatch(ev)
            except asyncio.CancelledError:
                self._queue.put_nowait(ev)  # flushed during shutdown
                raise
            except Exception:  # noqa: BLE001
                log.exception("dispatching %s failed", ev.title)

    async def dispatch(self, ev: AlertEvent, retries: bool = True) -> dict[str, str]:
        level = {Severity.CRITICAL: logging.ERROR, Severity.WARNING: logging.WARNING}.get(ev.severity, logging.INFO)
        if ev.kind is EventKind.RESOLVED:
            level = logging.INFO
        log.log(level, "%s", ev.headline)
        targets = [n for n in self.notifiers if n.accepts(ev)]
        results = await asyncio.gather(*(self._send(n, ev, retries) for n in targets))
        delivery = {n.name: res for n, res in zip(targets, results)}
        if targets and not any(r == "ok" for r in results) and ev.severity >= Severity.WARNING:
            log.error("NO notifier could deliver %r: %s", ev.headline, delivery)
        self.store.log_event(ev, delivery)
        return delivery

    async def _send(self, n: Notifier, ev: AlertEvent, retries: bool) -> str:
        delays = RETRY_DELAYS if retries else ()
        err = ""
        for attempt in range(len(delays) + 1):
            try:
                await asyncio.wait_for(n.send(ev), timeout=SEND_TIMEOUT)
                return "ok"
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
                log.warning("notifier %s failed (attempt %d): %s", n.name, attempt + 1, err)
                if attempt < len(delays):
                    await asyncio.sleep(delays[attempt])
        return f"failed: {err}"

    # -- background loops --------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Ping a dead man's switch (e.g. healthchecks.io) while monitoring is healthy.

        If labwatch dies, or the machine running it loses power/network, the pings
        stop and the external service alerts you.
        """
        url = self.config.heartbeat_url
        failing = False
        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                if all(not t.done() for t in self._device_tasks):
                    try:
                        resp = await client.get(url)
                        resp.raise_for_status()
                        if failing:
                            log.info("heartbeat ping works again")
                        failing = False
                    except httpx.HTTPError as e:
                        if not failing:
                            log.warning("heartbeat ping failed: %s", type(e).__name__)
                        failing = True
                await asyncio.sleep(self.config.heartbeat_interval)

    async def _daily_summary_loop(self) -> None:
        hh, mm = (int(x) for x in self.config.daily_summary.split(":"))
        while True:
            now = datetime.now().astimezone()
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            self.emit(self.summary_event())

    def summary_event(self) -> AlertEvent:
        lines = []
        now = time.time()
        for rt in self.runtimes:
            firing = [r for r in rt.all_rules if r.state.firing]
            armed = sum(1 for r in rt.rules if r.state.armed)
            ok = rt.unreachable.last_success
            status = "OK" if not firing else f"{len(firing)} alert(s) firing"
            lines.append(f"{rt.name}: {status}; {armed}/{len(rt.rules)} alarms armed; "
                         f"last data {fmt_duration(now - ok) + ' ago' if ok else 'never'}")
            if rt.latest:
                lines.append(f"  {rt.snapshot()}")
            for r in firing:
                lines.append(f"  FIRING: {r.name} ({r.state.last_detail})")
        mutes = self.store.mutes(now)
        if mutes:
            lines.append("Muted: " + ", ".join(
                f"{m.target}" + (f" until {fmt_time(m.until)}" if m.until else "") for m in mutes))
        return AlertEvent(EventKind.INFO, "labwatch/summary", "labwatch", "labwatch daily summary",
                          "\n".join(lines), Severity.INFO)

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                self.store.prune(self.config.history_days)
                self.store.checkpoint()
            except Exception:  # noqa: BLE001
                log.exception("pruning the state database failed")
            await asyncio.sleep(3600)


def build_runtime(config: Config, sec) -> DeviceRuntime:
    sec.require_env("device")
    device = build_device(sec.cfg)
    rules = build_rules(device.name, sec.cfg.get("rules") or [], config.rule_defaults, device.channels)
    unreach_cfg = {
        "severity": sec.cfg.get("unreachable_severity", "critical"),
        "auto_arm": False,
        "for": 0,  # unreachable_after already is the debounce
        "clear_for": 0,
        "message": sec.cfg.get(
            "unreachable_message",
            "Can't reach the control software. Possible causes: power outage, control PC "
            "crashed/rebooted, network down, API key revoked.",
        ),
    }
    unreachable = UnreachableRule(device.name, device.unreachable_after, build_options(unreach_cfg, config.rule_defaults))
    if "steady_state" in sec.cfg:
        steady_cfg = sec.cfg["steady_state"]  # explicit (null/false disables the gate)
    elif device.default_steady_state:
        # Built-in default: only the conditions whose channels this device actually has.
        d = device.default_steady_state
        conds = [c for c in d["conditions"] if c["channel"] in device.channels]
        steady_cfg = {**d, "conditions": conds} if conds else None
    else:
        steady_cfg = None
    steady = SteadyState.from_config(device.name, steady_cfg, device.channels) if steady_cfg else None
    # Keep enough in-memory history for the longest rule window and the steady-state gate.
    keep = max([3600.0, *(r.horizon for r in rules), steady.for_s if steady else 0.0]) + 3600.0
    for r in rules:
        if r.rule_id == unreachable.rule_id:
            raise RuleConfigError(
                f"device {device.name!r}: rule name 'unreachable' is reserved for the built-in "
                "connection rule; pick another name"
            )
    return DeviceRuntime(device=device, rules=rules, unreachable=unreachable, history=History(keep), steady=steady)


def _muted_by(mutes: list[Mute], rule: Rule) -> bool:
    return any(m.matches(rule.device, rule.rule_id) for m in mutes)


def _muted_device(mutes: list[Mute], device: str) -> bool:
    return any(m.target in ("*", device) for m in mutes)
