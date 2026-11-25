"""Reachability check for any networked machine (e.g. an evaporator's control PC).

Config::

    - name: Plassys
      type: ping
      host: ${PLASSYS_HOST}
      method: icmp          # icmp (system `ping`) or tcp (connect to `port`)
      port: 3389            # tcp only; e.g. 3389 RDP, 445 SMB on Windows PCs
      timeout: 3
      attempts: 2           # a poll fails only if all attempts fail
      poll_interval: 30

Channels: ``reachable`` (1 = up, 0 = down) and ``rtt_ms``. Alert on
``reachable`` with ``not_equals: 1`` and a ``for:`` duration - two rules with
different durations give a warning first and a critical alert later.

Windows blocks inbound ICMP echo by default; if ping doesn't work, use
``method: tcp`` with a port the PC has open.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import sys
import time
from typing import Any

from labwatch.devices.base import ChannelSpec, Device, DeviceConfigError, register
from labwatch.models import Reading, parse_duration, utcnow

_RTT = re.compile(r"time[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)

CHANNELS = {
    "reachable": ChannelSpec("reachable", "reachable", labels=((0, "down"), (1, "up"))),
    "rtt_ms": ChannelSpec("rtt_ms", "rtt", "ms"),
}


@register("ping")
class PingDevice(Device):
    summary_channels = ("reachable", "rtt_ms")

    def __init__(
        self,
        name: str,
        host: str,
        method: str = "icmp",
        port: int | None = None,
        timeout: float = 3.0,
        attempts: int = 2,
        poll_interval: float = 30.0,
    ) -> None:
        # Unreachability is reported through the `reachable` channel and normal rules,
        # so the built-in "unreachable" rule must never trigger for this device.
        super().__init__(name, dict(CHANNELS), poll_interval, unreachable_after=float("inf"))
        self.host = host
        self.method = method
        self.port = port
        self.timeout = timeout
        self.attempts = max(1, attempts)
        self.last_error = ""

    @classmethod
    def from_config(cls, name: str, cfg: dict[str, Any]) -> PingDevice:
        host = cfg.get("host")
        if not host:
            raise DeviceConfigError(f"device {name!r}: 'host' is required")
        method = str(cfg.get("method", "icmp")).lower()
        if method not in ("icmp", "tcp"):
            raise DeviceConfigError(f"device {name!r}: method must be 'icmp' or 'tcp'")
        port = cfg.get("port")
        if method == "tcp" and not port:
            raise DeviceConfigError(f"device {name!r}: method 'tcp' needs a 'port' (e.g. 3389 or 445)")
        if method == "icmp" and not shutil.which("ping"):
            raise DeviceConfigError(f"device {name!r}: no 'ping' command found; use method: tcp")
        return cls(
            name=name,
            host=str(host),
            method=method,
            port=int(port) if port else None,
            timeout=parse_duration(cfg.get("timeout", 3.0)),
            attempts=int(cfg.get("attempts", 2)),
            poll_interval=parse_duration(cfg.get("poll_interval", 30.0)),
        )

    async def _icmp(self) -> float | None:
        if sys.platform.startswith("win"):
            cmd = ["ping", "-n", "1", "-w", str(int(self.timeout * 1000)), self.host]
        elif sys.platform == "darwin":
            cmd = ["ping", "-c", "1", "-W", str(int(self.timeout * 1000)), self.host]
        else:
            cmd = ["ping", "-c", "1", "-W", str(max(1, round(self.timeout))), self.host]
        t0 = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.timeout + 2)
        except asyncio.TimeoutError:
            proc.kill()
            self.last_error = "ping timed out"
            return None
        text = out.decode(errors="replace")
        # Windows ping returns 0 for "Destination host unreachable" replies; require a time.
        m = _RTT.search(text)
        if proc.returncode != 0 or not m:
            self.last_error = (text.strip().splitlines() or ["no reply"])[-1][:120]
            return None
        try:
            return float(m.group(1))
        except ValueError:
            return (time.perf_counter() - t0) * 1000

    async def _tcp(self) -> float | None:
        t0 = time.perf_counter()
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), self.timeout)
        except (OSError, asyncio.TimeoutError) as e:
            self.last_error = f"tcp {self.port}: {type(e).__name__}"
            return None
        rtt = (time.perf_counter() - t0) * 1000
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return rtt

    async def poll(self) -> list[Reading]:
        rtt = None
        for _ in range(self.attempts):
            rtt = await (self._icmp() if self.method == "icmp" else self._tcp())
            if rtt is not None:
                break
        now = utcnow()
        up = 1.0 if rtt is not None else 0.0
        return [
            Reading(self.name, "reachable", self.host, up, now, raw=str(int(up)),
                    status="OK" if up else self.last_error, label=CHANNELS["reachable"].label_for(up)),
            Reading(self.name, "rtt_ms", self.host, rtt, now if rtt is not None else None, "ms",
                    status="OK" if up else "NO_REPLY"),
        ]

    async def describe(self) -> dict[str, Any]:
        how = "icmp ping" if self.method == "icmp" else f"tcp port {self.port}"
        return {"host": self.host, "check": how}
