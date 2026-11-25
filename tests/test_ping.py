"""Ping device: tcp checks against a local server / closed port, icmp output parsing."""

from __future__ import annotations

import asyncio
import errno
import socket

import pytest

from labwatch import engine
from labwatch.config import Config, Section
from labwatch.devices import DeviceConfigError, build_device
from labwatch.devices import ping as ping_mod
from labwatch.devices.ping import PingDevice
from labwatch.engine import Monitor, build_runtime
from labwatch.models import AlertEvent, EventKind, Severity
from labwatch.notifiers.base import Notifier
from labwatch.state import StateStore


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def by_channel(readings):
    return {r.channel: r for r in readings}


def test_tcp_up_against_local_server():
    async def go():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            dev = PingDevice("PC", "127.0.0.1", method="tcp", port=port, timeout=2, attempts=1)
            return await dev.poll()

    r = by_channel(asyncio.run(go()))
    assert list(r) == ["reachable", "rtt_ms"]
    up, rtt = r["reachable"], r["rtt_ms"]
    assert (up.value, up.label, up.status, up.raw, up.source) == (1.0, "up", "OK", "1", "127.0.0.1")
    assert up.timestamp is not None and up.format_value() == "up"
    assert rtt.value is not None and 0 <= rtt.value < 2000 and rtt.unit == "ms"
    assert rtt.timestamp is not None and rtt.status == "OK"


def test_tcp_down_on_closed_port_never_raises():
    port = closed_port()
    dev = PingDevice("PC", "127.0.0.1", method="tcp", port=port, timeout=2, attempts=2)
    r = by_channel(asyncio.run(dev.poll()))
    down, rtt = r["reachable"], r["rtt_ms"]
    assert (down.value, down.label, down.raw) == (0.0, "down", "0")
    assert down.status == f"tcp {port}: ConnectionRefusedError"
    assert (rtt.value, rtt.timestamp, rtt.status) == (None, None, "NO_REPLY")


def test_tcp_timeout_is_down(monkeypatch):
    async def hang(host, port):
        await asyncio.sleep(3600)

    monkeypatch.setattr(ping_mod.asyncio, "open_connection", hang)
    dev = PingDevice("PC", "192.0.2.1", method="tcp", port=3389, timeout=0.05, attempts=1)
    r = by_channel(asyncio.run(dev.poll()))
    assert r["reachable"].value == 0.0
    assert r["reachable"].status == "tcp 3389: TimeoutError"


def test_poll_is_up_if_any_attempt_succeeds():
    dev = PingDevice("PC", "10.0.0.5", method="tcp", port=445, attempts=3)
    results = iter([None, None, 4.2])
    calls = []

    async def fake_tcp():
        calls.append(1)
        return next(results)

    dev._tcp = fake_tcp
    r = by_channel(asyncio.run(dev.poll()))
    assert len(calls) == 3
    assert (r["reachable"].value, r["rtt_ms"].value) == (1.0, 4.2)


def test_poll_stops_after_first_successful_attempt():
    dev = PingDevice("PC", "10.0.0.5", method="tcp", port=445, attempts=3)
    calls = []

    async def fake_tcp():
        calls.append(1)
        return 1.0

    dev._tcp = fake_tcp
    asyncio.run(dev.poll())
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# icmp (system ping) with a fake subprocess
# ---------------------------------------------------------------------------


class FakeProc:
    def __init__(self, output: str, returncode: int) -> None:
        self.output = output.encode()
        self.returncode = returncode

    async def communicate(self):
        return self.output, b""

    def kill(self) -> None:
        pass


def fake_ping(monkeypatch, output: str, returncode: int) -> list[tuple]:
    calls: list[tuple] = []

    async def create_subprocess_exec(*cmd, **kw):
        calls.append(cmd)
        return FakeProc(output, returncode)

    monkeypatch.setattr(ping_mod.asyncio, "create_subprocess_exec", create_subprocess_exec)
    return calls


LINUX_OK = """PING 10.0.0.5 (10.0.0.5) 56(84) bytes of data.
64 bytes from 10.0.0.5: icmp_seq=1 ttl=64 time=0.412 ms

--- 10.0.0.5 ping statistics ---
1 packets transmitted, 1 received, 0% packet loss, time 0ms
"""
LINUX_LOSS = """PING 10.0.0.5 (10.0.0.5) 56(84) bytes of data.

--- 10.0.0.5 ping statistics ---
1 packets transmitted, 0 received, 100% packet loss, time 0ms
"""
WINDOWS_UNREACHABLE = """Pinging 10.0.0.5 with 32 bytes of data:
Reply from 10.0.0.1: Destination host unreachable.
"""
WINDOWS_OK = "Reply from 10.0.0.5: bytes=32 time<1ms TTL=128\n"


def test_icmp_reply_parses_rtt(monkeypatch):
    calls = fake_ping(monkeypatch, LINUX_OK, 0)
    dev = PingDevice("PC", "10.0.0.5", method="icmp", timeout=3, attempts=2)
    r = by_channel(asyncio.run(dev.poll()))
    assert (r["reachable"].value, r["rtt_ms"].value) == (1.0, 0.412)
    assert len(calls) == 1 and calls[0][0] == "ping" and calls[0][-1] == "10.0.0.5"


def test_icmp_windows_style_time_below_1ms(monkeypatch):
    fake_ping(monkeypatch, WINDOWS_OK, 0)
    r = by_channel(asyncio.run(PingDevice("PC", "10.0.0.5", attempts=1).poll()))
    assert (r["reachable"].value, r["rtt_ms"].value) == (1.0, 1.0)


@pytest.mark.parametrize(
    "output, rc, status",
    [
        (LINUX_LOSS, 1, "1 packets transmitted, 0 received, 100% packet loss, time 0ms"),
        # Windows returns 0 for "Destination host unreachable": a time is required
        (WINDOWS_UNREACHABLE, 0, "Reply from 10.0.0.1: Destination host unreachable."),
        ("", 2, "no reply"),
    ],
)
def test_icmp_no_reply_is_down(monkeypatch, output, rc, status):
    calls = fake_ping(monkeypatch, output, rc)
    r = by_channel(asyncio.run(PingDevice("PC", "10.0.0.5", attempts=2).poll()))
    assert len(calls) == 2
    assert r["reachable"].value == 0.0 and r["reachable"].status == status
    assert r["rtt_ms"].value is None


def test_icmp_poll_never_raises_when_ping_cannot_start(monkeypatch):
    async def create_subprocess_exec(*cmd, **kw):
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(ping_mod.asyncio, "create_subprocess_exec", create_subprocess_exec)
    r = by_channel(asyncio.run(PingDevice("PC", "10.0.0.5", attempts=1).poll()))
    assert r["reachable"].value == 0.0


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_from_config():
    dev = build_device({"name": "Plassys", "type": "ping", "host": "10.0.0.5", "method": "TCP", "port": "3389",
                        "timeout": "2s", "attempts": 3, "poll_interval": "1m"})
    assert isinstance(dev, PingDevice)
    assert (dev.method, dev.port, dev.timeout, dev.attempts, dev.poll_interval) == ("tcp", 3389, 2, 3, 60)
    assert dev.unreachable_after == float("inf")  # reachability goes through the `reachable` channel
    assert set(dev.channels) == {"reachable", "rtt_ms"}


@pytest.mark.parametrize(
    "cfg, match",
    [
        ({"method": "tcp"}, "needs a 'port'"),
        ({"method": "udp"}, "method must be 'icmp' or 'tcp'"),
        ({"host": None}, "'host' is required"),
    ],
)
def test_from_config_errors(cfg, match):
    with pytest.raises(DeviceConfigError, match=match):
        build_device({"name": "PC", "type": "ping", "host": "10.0.0.5", **cfg})


def test_icmp_without_ping_binary_is_a_config_error(monkeypatch):
    monkeypatch.setattr(ping_mod.shutil, "which", lambda name: None)
    with pytest.raises(DeviceConfigError, match="use method: tcp"):
        build_device({"name": "PC", "type": "ping", "host": "10.0.0.5"})


# ---------------------------------------------------------------------------
# through the engine: warning first, critical later
# ---------------------------------------------------------------------------


class _Clock:
    t = 1_800_000_000.0

    def time(self):
        return self.t

    def monotonic(self):
        return self.t


class _Inbox(Notifier):
    def __init__(self):
        super().__init__("inbox", Severity.INFO)
        self.events: list[AlertEvent] = []

    async def send(self, event):
        self.events.append(event)

    @classmethod
    def from_config(cls, name, min_severity, cfg):
        return cls()


def test_ping_rules_warning_then_critical(tmp_path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(engine, "time", clock)
    rules = [
        {"channel": "reachable", "not_equals": 1, "for": "2m", "severity": "warning", "name": "PC down"},
        {"channel": "reachable", "not_equals": 1, "for": "10m", "severity": "critical", "name": "PC down 10 min"},
    ]
    sec = Section({"name": "PC", "type": "ping", "host": "10.0.0.5", "method": "tcp", "port": 3389,
                   "rules": rules}, set())
    config = Config(path=tmp_path / "c.yaml", state_db=tmp_path / "s.sqlite", history_days=30,
                    rule_defaults={"auto_arm": False}, devices=[sec], notifiers=[], heartbeat_url=None,
                    heartbeat_interval=60, daily_summary=None, notify_startup=False, secrets=set())
    rt = build_runtime(config, sec)
    up = {"rtt": 1.5}

    async def fake_tcp():
        return up["rtt"]

    rt.device._tcp = fake_tcp
    inbox = _Inbox()
    mon = Monitor(config, [rt], [inbox], StateStore(":memory:"))
    rt.unreachable.started = clock.t

    async def poll_for(seconds):
        n = len(inbox.events)
        for _ in range(int(seconds // 30)):
            clock.t += 30
            await mon.poll_once(rt)
            while not mon._queue.empty():
                await mon.dispatch(mon._queue.get_nowait())
        return [(e.kind, e.title, e.severity) for e in inbox.events[n:]]

    async def scenario():
        assert await poll_for(600) == []
        up["rtt"] = None  # the PC goes down
        assert await poll_for(150) == [(EventKind.FIRING, "PC: PC down", Severity.WARNING)]
        assert await poll_for(480) == [(EventKind.FIRING, "PC: PC down 10 min", Severity.CRITICAL)]
        assert await poll_for(2 * 3600) != []  # reminders - but never "PC: unreachable"
        assert all(e.title != "PC: unreachable" for e in inbox.events)

    asyncio.run(scenario())
