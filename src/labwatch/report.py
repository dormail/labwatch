"""Human-readable status reports from the running monitor (Telegram commands, ...)."""

from __future__ import annotations

import html
import time
from typing import TYPE_CHECKING

from labwatch.rules import UnreachableRule

if TYPE_CHECKING:
    from labwatch.engine import DeviceRuntime
    from labwatch.state import Mute

LABEL_W = 18


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s ago"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    return f"{seconds / 3600:.1f} h ago"


def _pretty(channel: str) -> str:
    return channel.replace("fse_", "").replace("_", " ")


def data_age(rt: DeviceRuntime, now: float) -> float | None:
    ok = rt.unreachable.last_success
    return None if ok is None else now - ok


def firing_rules(rt: DeviceRuntime) -> list:
    return [r for r in rt.all_rules if r.state.firing]


def device_headline(rt: DeviceRuntime, now: float, muted: bool = False) -> str:
    """'Ket: OK · T4K 3.44 K · TMXC 10.8 mK · pulse_tube on (12 s ago)'."""
    firing = firing_rules(rt)
    reach = rt.latest.get("reachable")
    if rt.last_poll_ok is False:
        state = "UNREACHABLE"
    elif reach is not None and reach.value == 0:
        state = "DOWN"
    elif firing:
        state = f"{len(firing)} ALERT{'S' if len(firing) > 1 else ''}"
    else:
        state = "OK"
    if muted:
        state += " (muted)"
    parts = [f"{rt.name}: {state}"]
    for ch in rt.device.overview_channels:
        r = rt.latest.get(ch)
        if r is not None and (r.value is not None or r.label):
            parts.append(f"{_pretty(ch)} {r.format_value()}")
    return " · ".join(parts) + f" ({_age(data_age(rt, now))})"


def overview_html(runtimes: list[DeviceRuntime], mutes: list[Mute], now: float | None = None) -> str:
    now = time.time() if now is None else now
    lines = ["<b>labwatch status</b>"]
    for rt in runtimes:
        muted = any(m.target in ("*", rt.name) for m in mutes)
        reach = rt.latest.get("reachable")
        bad = firing_rules(rt) or rt.last_poll_ok is False or (reach is not None and reach.value == 0)
        icon = "🔴" if bad else "🟢"
        lines.append(f"{icon} {html.escape(device_headline(rt, now, muted))}")
    lines.append("\nDetails: /" + " /".join(command_name(rt.name) for rt in runtimes))
    return "\n".join(lines)


def device_html(rt: DeviceRuntime, mutes: list[Mute], now: float | None = None) -> str:
    now = time.time() if now is None else now
    head = f"<b>{html.escape(rt.name)}</b> · data {_age(data_age(rt, now))}"
    if rt.last_poll_ok is False:
        head += f"\n⚠️ last poll failed: {html.escape(rt.unreachable.last_error or 'unknown error')}"
    body: list[str] = []
    for title, channels in rt.device.status_sections:
        rows = []
        for ch in channels:
            r = rt.latest.get(ch)
            if r is None:
                continue
            if not title:  # free text line (e.g. Bluefors status message)
                text = r.format_value()
                if text and text != "-":
                    body.append(f"{_pretty(ch)}: {text}")
                continue
            rows.append(f"  {_pretty(ch):<{LABEL_W}} {r.format_value()}")
        if rows:
            body.append(title)
            body.extend(rows)
    text = head
    if body:
        text += "\n<pre>" + html.escape("\n".join(body)) + "</pre>"

    firing = firing_rules(rt)
    if firing:
        text += "\n" + "\n".join(
            f"🔴 {html.escape(r.name)}: {html.escape(r.state.last_detail)}" for r in firing
        )
    rules = [r for r in rt.rules if not isinstance(r, UnreachableRule)]
    if rules:
        armed = sum(1 for r in rules if r.state.armed)
        text += f"\nAlarms: {armed}/{len(rules)} armed"
        if armed < len(rules):
            text += " (the others arm once their value has been normal for a while)"
    muted = [m for m in mutes if m.target in ("*", rt.name) or m.target.startswith(rt.name + "/")]
    for m in muted:
        until = time.strftime("%H:%M %d.%m.", time.localtime(m.until)) if m.until else "unmuted"
        text += f"\n🔕 muted ({html.escape(m.target)}) until {until}"
    return text


def alerts_html(runtimes: list[DeviceRuntime], now: float | None = None) -> str:
    now = time.time() if now is None else now
    lines = []
    for rt in runtimes:
        for r in firing_rules(rt):
            since = r.state.fired_at
            dur = f" for {_age(now - since).removesuffix(' ago')}" if since else ""
            lines.append(f"🔴 <b>{html.escape(rt.name)}</b>: {html.escape(r.name)}{dur}\n   {html.escape(r.state.last_detail)}")
    return "\n".join(lines) if lines else "🟢 No active alerts."


def command_name(device_name: str) -> str:
    """Telegram command for a device: lowercase a-z, 0-9, _ (max 32 chars)."""
    out = "".join(c if c.isalnum() and c.isascii() else "_" for c in device_name.lower()).strip("_")
    return (out or "device")[:32]
