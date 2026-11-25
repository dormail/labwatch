"""Command line interface: ``labwatch <command>``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from typing import Iterable

from labwatch import __version__
from labwatch.config import Config, ConfigError, load_config
from labwatch.devices import DeviceConfigError, DeviceError, build_device
from labwatch.engine import Monitor, build_runtime, fmt_duration, fmt_time
from labwatch.models import AlertEvent, EventKind, Reading, Severity
from labwatch.notifiers import NotifierConfigError, build_notifier
from labwatch.rules import RuleConfigError, RuleState, Verdict, parse_duration
from labwatch.state import StateStore

log = logging.getLogger("labwatch")


# -- logging with secret redaction ----------------------------------------------


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self.secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            msg = self.redact(record.getMessage())
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = self.redact(record.exc_text)
            record.msg, record.args = msg, None
        return True


def setup_logging(verbose: bool, secrets: Iterable[str] = ()) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    handler.addFilter(RedactingFilter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # httpx logs every request URL at INFO - and the Bluefors API key is in the URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# -- helpers ----------------------------------------------------------------------


def _table(rows: list[list[str]], header: list[str]) -> str:
    rows = [header, *rows]
    widths = [max(len(r[i]) for r in rows) for i in range(len(header))]
    out = []
    for n, r in enumerate(rows):
        out.append("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
        if n == 0:
            out.append("  ".join("-" * w for w in widths))
    return "\n".join(out)


def _age(seconds: float | None) -> str:
    return "-" if seconds is None else fmt_duration(seconds)


# -- commands -----------------------------------------------------------------------


def cmd_run(cfg: Config, args) -> int:
    monitor = Monitor.from_config(cfg)
    if not monitor.notifiers:
        log.warning("no notifiers configured - alerts will only appear in this log!")
    for rt in monitor.runtimes:
        log.info("%s: %d channels, %d rules, polling every %gs", rt.name, len(rt.device.channels),
                 len(rt.rules), rt.device.poll_interval)
    log.info("notifiers: %s", ", ".join(repr(n) for n in monitor.notifiers) or "(none)")
    if cfg.heartbeat_url:
        log.info("heartbeat: pinging every %gs", cfg.heartbeat_interval)
    else:
        log.warning("no heartbeat configured - nobody will notice if labwatch itself stops")

    async def main() -> None:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, task.cancel)
            except (NotImplementedError, RuntimeError):  # Windows
                pass
        try:
            await monitor.run()
        except asyncio.CancelledError:
            log.info("stopping")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_check(cfg: Config, args) -> int:
    """Poll each device once and show readings and what every rule thinks - no notifications."""
    store = StateStore(cfg.state_db)
    saved = store.load_rule_states()
    sections = [cfg.device_section(args.device)] if args.device else cfg.devices
    rc = 0

    async def check_one(sec) -> int:
        rt = build_runtime(cfg, sec)
        dev = rt.device
        try:
            try:
                info = await dev.describe()
                if info:
                    print(f"== {dev.name}: " + ", ".join(f"{k}={v}" for k, v in info.items()))
                else:
                    print(f"== {dev.name}")
                readings = await dev.poll()
            except DeviceError as e:
                print(f"== {dev.name}: ERROR: {e}")
                return 1
        finally:
            await dev.aclose()
        now = time.time()
        for ch in dev.channels:
            rt.history.seed(ch, store.history(dev.name, ch, now - rt.history.keep))
        for r in readings:
            rt.latest[r.channel] = r
            rt.history.add(r, now)
        readings = [*readings, *rt.add_computed(now)]
        print(_table(
            [[r.channel, r.format_value(), r.status or "", _age(r.age()), r.source] for r in readings],
            ["channel", "value", "status", "age", "source"],
        ))
        print()
        ctx = rt.context(now)
        if rt.steady is not None:
            print(f"steady state: {'yes' if ctx.steady_ready else 'no - ' + ctx.steady_detail}\n")
        rows = []
        for rule in rt.rules:
            if rule.rule_id in saved:  # before check(): hysteresis depends on the firing state
                rule.state = RuleState.from_dict(saved[rule.rule_id])
            paused = rule.opts.steady_only and not ctx.steady_ready
            v = Verdict(None, detail=f"waits for steady state ({ctx.steady_detail})") if paused else rule.check(ctx)
            cond = "paused" if paused else {True: "TRIGGERED", False: "ok", None: "no data"}[v.active]
            rows.append([rule.name, rule.opts.severity.label, cond, rule.status_text(), v.detail])
        if rows:
            print(_table(rows, ["rule", "severity", "condition now", "state", "detail"]))
            if any(r[3] == "disarmed" for r in rows):
                print("\n'disarmed' rules activate once their condition has been ok for arm_after "
                      "(e.g. after the fridge is cold). State is what `labwatch run` last saved.")
        print()
        return 0

    async def main() -> int:
        results = [await check_one(sec) for sec in sections]
        return max(results, default=0)

    rc = asyncio.run(main())
    store.close()
    return rc


def cmd_discover(cfg: Config, args) -> int:
    sec = cfg.device_section(args.device)
    sec.require_env("device")
    dev = build_device(sec.cfg)

    async def main() -> int:
        try:
            values = await dev.discover(args.prefix or "")
        finally:
            await dev.aclose()
        if args.grep:
            g = args.grep.lower()
            values = [v for v in values if g in v.source.lower() or g in (v.type or "").lower()]
        rows = [[v.source, (v.type or "").replace("Value.", ""), (v.raw or "")[:40].replace("\n", " "),
                 v.status or "", _age(v.age)] for v in values]
        print(_table(rows, ["path", "type", "value", "status", "age"]))
        print(f"\n{len(rows)} values. Use a path in config.yaml under the device's 'channels:'.")
        return 0

    try:
        return asyncio.run(main())
    except DeviceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def cmd_test_notify(cfg: Config, args) -> int:
    sections = [s for s in cfg.notifiers if not args.notifier or s.name == args.notifier]
    if not sections:
        print("no matching notifiers configured", file=sys.stderr)
        return 1
    severity = Severity.parse(args.severity)
    ev = AlertEvent(
        kind=EventKind.TEST if severity is Severity.INFO else EventKind.FIRING,
        rule_id="labwatch/test", device="labwatch",
        title="Test notification from labwatch",
        message=(args.message or "If you can read this, this notification channel works.")
        + f"\nSeverity: {severity.label}",
        severity=severity,
    )

    async def main() -> int:
        rc = 0
        for sec in sections:
            try:
                sec.require_env("notifier")
                n = build_notifier(sec.cfg)
            except (ConfigError, NotifierConfigError) as e:
                print(f"{sec.name}: CONFIG ERROR: {e}")
                rc = 1
                continue
            try:
                await n.send(ev)
                print(f"{n.name}: sent")
            except Exception as e:  # noqa: BLE001
                print(f"{n.name}: FAILED: {type(e).__name__}: {e}")
                rc = 1
            finally:
                await n.aclose()
        return rc

    return asyncio.run(main())


def cmd_status(cfg: Config, args) -> int:
    store = StateStore(cfg.state_db)
    now = time.time()
    devs = store.devices()
    for sec in cfg.devices:
        name = sec.name
        d = devs.get(name)
        last_ok = d["last_success"] if d else None
        line = f"== {name}: last data {_age(now - last_ok) + ' ago' if last_ok else 'never'}"
        if d and d["last_error_at"] and (not last_ok or d["last_error_at"] > last_ok):
            line += f"  (FAILING: {d['last_error']})"
        print(line)
        latest = store.latest(name)
        if latest:
            rows = []
            for r in latest:
                if args.all or r["value"] is not None:
                    rd = Reading(name, r["channel"], r["source"], r["value"], None, r["unit"], r["raw"], r["status"], label=r["label"])
                    rows.append([r["channel"], rd.format_value(), _age(now - r["ts"]) if r["ts"] else "-"])
            print(_table(rows, ["channel", "value", "age"]))
        states = store.rule_states(name)
        if states:
            rows = []
            for s in states:
                st = json.loads(s["data"])
                state = "FIRING" if st.get("firing") else ("armed" if st.get("armed") else "disarmed")
                rows.append([s["rule_id"].split("/", 1)[1], state, st.get("last_detail", "")])
            print()
            print(_table(rows, ["rule", "state", "last"]))
        print()
    mutes = store.mutes(now)
    if mutes:
        print("Muted:")
        for m in mutes:
            until = f"until {fmt_time(m.until)}" if m.until else "until unmuted"
            print(f"  {m.target}  {until}  {m.reason}")
        print()
    events = store.events(args.events)
    if events:
        print(f"Last {len(events)} events:")
        for e in reversed(events):
            delivery = json.loads(e["delivery"] or "{}")
            dtxt = ", ".join(f"{k}:{v[:30]}" for k, v in delivery.items()) or "logged only"
            print(f"  {fmt_time(e['ts'])}  {e['kind']:8s} {e['title']}  [{dtxt}]")
    store.close()
    return 0


def cmd_mute(cfg: Config, args) -> int:
    store = StateStore(cfg.state_db)
    until = None if args.indefinitely else time.time() + parse_duration(args.duration)
    store.mute(args.target, until, args.reason or "")
    when = "until unmuted" if until is None else f"until {fmt_time(until)}"
    print(f"muted {args.target!r} {when}")
    print("(alerts are still evaluated and logged; firing alerts are sent once the mute ends)")
    store.close()
    return 0


def cmd_backup(cfg: Config, args) -> int:
    """Bundle config, secrets and state into one tar.gz to move labwatch to another machine."""
    import os
    import tarfile
    import tempfile
    from pathlib import Path

    out = Path(args.output or f"labwatch-backup-{time.strftime('%Y%m%d-%H%M')}.tar.gz")
    base = cfg.path.parent
    files: list[tuple[Path, str]] = [(cfg.path, cfg.path.name)]
    env = base / ".env"
    if env.is_file():
        files.append((env, ".env"))
    for sec in cfg.devices:  # pinned TLS certificates referenced as relative paths
        v = sec.cfg.get("verify_tls")
        if isinstance(v, str) and v.lower() not in ("true", "false"):
            p = Path(v) if Path(v).is_absolute() else base / v
            if p.is_file() and not Path(v).is_absolute():
                files.append((p, v))
    with tempfile.TemporaryDirectory() as tmp:
        db_copy = Path(tmp) / "labwatch.sqlite"
        if cfg.state_db.is_file():
            store = StateStore(cfg.state_db)
            store.backup_to(db_copy)
            store.close()
            try:
                arc_db = str(cfg.state_db.relative_to(base))
            except ValueError:
                arc_db = cfg.state_db.name
                print(f"note: state_db is outside the config folder; it is stored as {arc_db}")
            files.append((db_copy, arc_db))
        old_umask = os.umask(0o077)  # the archive contains secrets
        try:
            with tarfile.open(out, "w:gz") as tar:
                for src, name in files:
                    tar.add(src, arcname=name)
        finally:
            os.umask(old_umask)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB):")
    for _, name in files:
        print(f"  {name}")
    print("It contains your API keys and tokens - copy it with scp/USB, not by e-mail or chat.")
    print(f"Restore on the new machine (in the labwatch folder):  tar xzf {out.name}")
    return 0


def cmd_unmute(cfg: Config, args) -> int:
    store = StateStore(cfg.state_db)
    ok = store.unmute(args.target)
    print(f"unmuted {args.target!r}" if ok else f"{args.target!r} was not muted")
    store.close()
    return 0 if ok else 1


# -- entry point ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="labwatch", description="Monitor lab equipment and send alerts.")
    p.add_argument("-c", "--config", help="config file (default: $LABWATCH_CONFIG or ./config.yaml)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"labwatch {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="run the monitor (foreground; use systemd/NSSM to keep it running)")

    s = sub.add_parser("check", help="poll devices once, show readings and rule conditions (no notifications)")
    s.add_argument("device", nargs="?")

    s = sub.add_parser("discover", help="list all values a device offers (to find paths for channels)")
    s.add_argument("device")
    s.add_argument("prefix", nargs="?", help="e.g. mapper.bf or driver.cpa")
    s.add_argument("-g", "--grep", help="only show paths/types containing this text")

    s = sub.add_parser("test-notify", help="send a test message through the notifiers")
    s.add_argument("notifier", nargs="?", help="only this notifier (by name)")
    s.add_argument("-s", "--severity", default="info", help="info (default), warning or critical")
    s.add_argument("-m", "--message")

    s = sub.add_parser("status", help="show the latest state recorded by the running monitor")
    s.add_argument("-a", "--all", action="store_true", help="include invalid channels")
    s.add_argument("-e", "--events", type=int, default=10, help="number of recent events to show")

    s = sub.add_parser("mute", help="silence notifications for a device or rule")
    s.add_argument("target", help='device name, rule id like "Flash/T4K above 6 K", or "*" for everything')
    g = s.add_mutually_exclusive_group()
    g.add_argument("--for", dest="duration", default="2h", help="duration like 30m, 2h, 3d (default 2h)")
    g.add_argument("--indefinitely", action="store_true")
    s.add_argument("-r", "--reason")

    s = sub.add_parser("unmute", help="remove a mute")
    s.add_argument("target")

    s = sub.add_parser("backup", help="bundle config, .env and history into a tar.gz (to move machines)")
    s.add_argument("-o", "--output", help="archive path (default: labwatch-backup-<date>.tar.gz)")
    return p


COMMANDS = {
    "run": cmd_run,
    "check": cmd_check,
    "discover": cmd_discover,
    "test-notify": cmd_test_notify,
    "status": cmd_status,
    "mute": cmd_mute,
    "unmute": cmd_unmute,
    "backup": cmd_backup,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    setup_logging(args.verbose, cfg.secrets)
    try:
        return COMMANDS[args.command](cfg, args)
    except (ConfigError, DeviceConfigError, RuleConfigError, NotifierConfigError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
