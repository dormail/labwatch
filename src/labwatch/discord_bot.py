"""Discord slash commands: anyone in a machine's channel can check and mute it.

Runs inside ``labwatch run`` next to the ``discord_bot`` notifier (same bot).
It keeps an outbound websocket to Discord's gateway (no open ports) and answers
slash commands. A channel is one subscriber: ``/mute`` in #flash mutes alerts in
#flash for everybody in that channel; the bot says publicly who muted what.

  /status [device]          overview, or one machine (temperatures, pumps, ...)
  /alerts                   active alerts
  /mute [what] [duration]   mute this channel (default: all of this machine, 2 h)
  /unmute [what]            unmute (default: everything muted in this channel)
  /mutes                    what's muted here and for everyone
  /level <level>            critical / warning / info for this channel
  /muteall what duration    mute for everyone (Telegram too), announced everywhere
  /unmuteall what
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
import sys
import time
from typing import TYPE_CHECKING, Any, Callable

import httpx

from labwatch import report, subscriptions
from labwatch.models import AlertEvent, Severity
from labwatch.subscriptions import fmt_until, is_duration

if TYPE_CHECKING:
    from labwatch.engine import Monitor
    from labwatch.notifiers.discord_bot import DiscordBotNotifier

log = logging.getLogger(__name__)

EPHEMERAL = 64
FATAL_CLOSE_CODES = {4004, 4010, 4011, 4012, 4013, 4014}  # bad token, bad intents, ...


class DiscordAPIError(Exception):
    pass


class FatalGatewayError(Exception):
    pass


def html_to_markdown(text: str) -> str:
    """Convert the (simple, controlled) HTML of labwatch.report to Discord markdown."""
    text = re.sub(r"<pre>(.*?)</pre>", lambda m: "```\n" + m.group(1) + "\n```", text, flags=re.S)
    text = re.sub(r"</?b>", "**", text)
    text = re.sub(r"</?code>", "`", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def sub_id(channel_id: str | int) -> str:
    return f"discord:{channel_id}"


class DiscordCommandBot:
    def __init__(
        self,
        monitor: Monitor,
        notifier: DiscordBotNotifier,
        transport: httpx.AsyncBaseTransport | None = None,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self.monitor = monitor
        self.store = monitor.store
        self.n = notifier
        self._client = httpx.AsyncClient(base_url=notifier.api_base, headers=notifier.headers,
                                         timeout=20, transport=transport)
        self._connect = connect
        self.guilds: set[str] = set()
        self._seq: int | None = None
        self._acked = True
        self._bootstrap()
        notifier.recipients_fn = self.recipients_for

    def _bootstrap(self) -> None:
        level = self.n.min_severity.label
        names = {cid: f"#{dev}" for dev, cid in self.n.channels.items()}
        if self.n.status_channel:
            names.setdefault(self.n.status_channel, "#status")
        for cid, name in names.items():
            s = self.store.subscriber(sub_id(cid))
            self.store.upsert_subscriber(sub_id(cid), s.name if s else name, "user", s.level if s else level)

    # -- who gets an alert -----------------------------------------------------------

    def recipients_for(self, event: AlertEvent) -> list[str]:
        now = time.time()
        out = []
        for cid in self.n.default_channels(event):
            s = self.store.subscriber(sub_id(cid))
            if s is None or subscriptions.wants(s, event, self.store, self.n.lifecycle, now):
                out.append(cid)
        return out

    # -- REST -------------------------------------------------------------------------

    async def _api(self, method: str, path: str, payload: Any = None) -> Any:
        try:
            resp = await self._client.request(method, path, json=payload)
        except httpx.HTTPError as e:
            raise DiscordAPIError(f"{method} {path}: {type(e).__name__}") from None
        if resp.status_code >= 400:
            raise DiscordAPIError(f"{method} {path}: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json() if resp.content else None

    def commands(self) -> list[dict[str, Any]]:
        devices = [{"name": rt.name, "value": rt.name} for rt in self.monitor.runtimes][:25]
        what = "device, alarm type (anomaly, pulse_tube, ...), both, or all"
        dur = "30m, 4h, 2d or forever (default 2h)"

        def opt(name, desc, required=False, choices=None):
            o = {"type": 3, "name": name, "description": desc[:100], "required": required}
            if choices:
                o["choices"] = choices
            return o

        levels = [{"name": x, "value": x} for x in ("critical", "warning", "info")]
        return [
            {"name": "status", "description": "Status of all machines, or one",
             "options": [opt("device", "which machine", choices=devices)]},
            {"name": "alerts", "description": "Active alerts"},
            {"name": "mute", "description": "Mute alerts in this channel (for everyone here)",
             "options": [opt("what", f"{what} (default: all)"), opt("duration", dur)]},
            {"name": "unmute", "description": "Unmute alerts in this channel",
             "options": [opt("what", "what to unmute (default: everything)")]},
            {"name": "mutes", "description": "What is muted here and for everyone"},
            {"name": "level", "description": "Which alerts this channel gets",
             "options": [opt("level", "critical = only ringing alerts", True, levels)]},
            {"name": "muteall", "description": "Mute for EVERYONE (all channels, Telegram too)",
             "options": [opt("what", what, True), opt("duration", "e.g. 4h or 2d (max 14d)", True),
                         opt("reason", "e.g. warm-up")]},
            {"name": "unmuteall", "description": "Remove a mute for everyone",
             "options": [opt("what", what, True)]},
        ]

    async def register_commands(self) -> None:
        app = await self._api("GET", "/oauth2/applications/@me")
        self.app_id = app["id"]
        self.guilds = set()
        for dev, cid in {**self.n.channels, **({"status": self.n.status_channel} if self.n.status_channel else {})}.items():
            try:
                ch = await self._api("GET", f"/channels/{cid}")
            except DiscordAPIError as e:
                log.warning("Discord: can't see the channel for %s (%s) - is the bot in that server "
                            "and allowed to view the channel? %s", dev, cid, e)
                continue
            if ch.get("guild_id"):
                self.guilds.add(str(ch["guild_id"]))
            s = self.store.subscriber(sub_id(cid))
            if s and ch.get("name"):
                self.store.upsert_subscriber(sub_id(cid), f"#{ch['name']}", s.role, s.level)
        for gid in self.guilds:  # guild commands show up immediately (global ones take up to an hour)
            await self._api("PUT", f"/applications/{self.app_id}/guilds/{gid}/commands", self.commands())

    # -- commands -------------------------------------------------------------------

    def _channel_device(self, channel_id: str) -> str | None:
        for dev, cid in self.n.channels.items():
            if cid == str(channel_id):
                return dev
        return None

    def respond(self, name: str, options: dict[str, str], channel_id: str, user: str) -> tuple[str, bool]:
        """(markdown reply, ephemeral)."""
        now = time.time()
        mutes = self.store.mutes(now)
        sid = sub_id(channel_id)
        sub = self.store.subscriber(sid)
        here_device = self._channel_device(channel_id)
        if name == "status":
            dev = options.get("device") or ""
            rt = subscriptions.find_device(self.monitor, dev) if dev else None
            if dev and rt is None:
                return f"Unknown device {dev!r}.", True
            text = report.device_html(rt, mutes, now) if rt else report.overview_html(self.monitor.runtimes, mutes, now)
            return html_to_markdown(text).replace("\nDetails: ", "\nDetails: `/status device:` "), True
        if name == "alerts":
            return html_to_markdown(report.alerts_html(self.monitor.runtimes, now)), True
        if name == "mutes":
            lines = []
            mine = self.store.personal_mutes(sid, now) if sub else []
            if mine:
                lines.append("**Muted in this channel**")
                lines += [f"🔕 {m.target} {fmt_until(m.until)}" for m in mine]
            if mutes:
                lines.append("**Muted for everyone**")
                lines += [f"🔕 {m.target} {fmt_until(m.until)}" + (f" ({m.reason})" if m.reason else "")
                          for m in mutes]
            return ("\n".join(lines) or "Nothing is muted."), True
        if name in ("mute", "unmute", "level") and sub is None:
            return "This channel doesn't receive labwatch alerts, so there's nothing to mute here.", True
        if name == "mute":
            tokens = (options.get("what") or "all").split()
            target, rest, err = subscriptions.parse_target(self.monitor, tokens)
            if err:
                return f"{err}. Try `all`, a device, or an alarm type: {', '.join(subscriptions.categories(self.monitor))}", True
            dur = options.get("duration") or (rest[0] if rest else "")
            if dur and not is_duration(dur):
                return f"Unknown duration {dur!r}: use 30m, 4h, 2d or forever.", True
            seconds = subscriptions.parse_duration(dur) if dur else subscriptions.DEFAULT_MUTE
            until = None if seconds is None else now + seconds
            self.store.personal_mute(sid, target, until)
            label = f"all {here_device} alerts" if target == "*" and here_device else (
                "all alerts" if target == "*" else f"**{target}** alerts")
            return f"🔕 **{user}** muted {label} in this channel {fmt_until(until)}. `/unmute` to undo.", False
        if name == "unmute":
            what = (options.get("what") or "all").strip()
            if what.lower() != "all":
                target, _, err = subscriptions.parse_target(self.monitor, what.split())
                if err:
                    return f"{err}.", True
            else:
                target = "all"
            n = self.store.personal_unmute(sid, target)
            if not n:
                return f"Nothing like {what!r} was muted in this channel. `/mutes` shows what is.", True
            return f"🔔 **{user}** unmuted {'everything' if target == 'all' else target} in this channel.", False
        if name == "level":
            try:
                level = Severity.parse(options.get("level", ""))
            except ValueError:
                return "Use critical, warning or info.", True
            self.store.upsert_subscriber(sid, sub.name, sub.role, level.label)
            what = {"critical": "only critical alerts", "warning": "warnings and critical alerts",
                    "info": "everything"}[level.label]
            return f"⚙️ **{user}** set this channel to get {what}.", False
        if name == "muteall":
            target, _, err = subscriptions.parse_target(self.monitor, (options.get("what") or "").split())
            if err:
                return f"{err}.", True
            dur = options.get("duration") or ""
            if not is_duration(dur) or subscriptions.parse_duration(dur) is None:
                return "Give a duration like 4h or 2d (mutes for everyone always end).", True
            seconds = subscriptions.parse_duration(dur)
            if seconds > subscriptions.MAX_GLOBAL_MUTE:
                return "At most 14 days for everyone.", True
            reason = "; ".join(x for x in ((options.get("reason") or "").strip(), f"by {user}") if x)
            self.store.mute(target, now + seconds, reason)
            return (f"🔕 **{user}** muted **{target}** for everyone {fmt_until(now + seconds)}. "
                    "All channels and Telegram users will be told."), False
        if name == "unmuteall":
            target, _, err = subscriptions.parse_target(self.monitor, (options.get("what") or "").split())
            if err:
                return f"{err}.", True
            if not self.store.unmute(target):
                return f"{target} wasn't muted for everyone.", True
            return f"🔔 **{user}** removed the mute for everyone on **{target}**.", False
        return "Unknown command.", True

    async def handle_interaction(self, d: dict[str, Any]) -> None:
        if d.get("type") != 2:  # APPLICATION_COMMAND
            return
        if d.get("guild_id") and self.guilds and str(d["guild_id"]) not in self.guilds:
            return
        data = d.get("data") or {}
        options = {o["name"]: str(o.get("value", "")) for o in data.get("options") or []}
        member = d.get("member") or {}
        user = member.get("user") or d.get("user") or {}
        uname = member.get("nick") or user.get("global_name") or user.get("username") or "someone"
        try:
            text, ephemeral = self.respond(data.get("name", ""), options, str(d.get("channel_id")), uname)
        except Exception:  # noqa: BLE001
            log.exception("Discord: command %s failed", data.get("name"))
            text, ephemeral = "Sorry, that failed (see the labwatch log).", True
        if len(text) > 1990:
            text = text[:1985] + "\n…"
        log.info("Discord /%s in channel %s by %s", data.get("name"), d.get("channel_id"), uname)
        await self._api("POST", f"/interactions/{d['id']}/{d['token']}/callback", {
            "type": 4,
            "data": {"content": text, "flags": EPHEMERAL if ephemeral else 0, "allowed_mentions": {"parse": []}},
        })

    # -- gateway --------------------------------------------------------------------

    async def _heartbeat(self, ws, interval: float) -> None:
        await asyncio.sleep(interval * random.random())
        while True:
            if not self._acked:  # no ACK since the last beat: zombie connection
                await ws.close(4000, "heartbeat timeout")
                return
            self._acked = False
            await ws.send(json.dumps({"op": 1, "d": self._seq}))
            await asyncio.sleep(interval)

    async def _session(self, ws) -> None:
        hello = json.loads(await asyncio.wait_for(ws.recv(), 30))
        interval = hello["d"]["heartbeat_interval"] / 1000.0
        self._acked = True
        await ws.send(json.dumps({"op": 2, "d": {
            "token": self.n._bot_token, "intents": 0,  # slash commands need no intents
            "properties": {"os": sys.platform, "browser": "labwatch", "device": "labwatch"},
        }}))
        hb = asyncio.create_task(self._heartbeat(ws, interval))
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("s") is not None:
                    self._seq = msg["s"]
                op = msg.get("op")
                if op == 0 and msg.get("t") == "READY":
                    log.info("Discord commands active in %d server(s)", len(self.guilds))
                elif op == 0 and msg.get("t") == "INTERACTION_CREATE":
                    asyncio.create_task(self._safe(self.handle_interaction(msg["d"])))
                elif op == 1:
                    await ws.send(json.dumps({"op": 1, "d": self._seq}))
                elif op == 11:
                    self._acked = True
                elif op in (7, 9):  # reconnect requested / invalid session
                    await asyncio.sleep(random.uniform(1, 5))
                    return
        finally:
            hb.cancel()
            code = getattr(ws, "close_code", None)
            if code in FATAL_CLOSE_CODES:
                raise FatalGatewayError(f"Discord closed the gateway with code {code} (bad bot token?)")

    async def _safe(self, coro) -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001
            log.exception("Discord: handling an interaction failed")

    async def run(self) -> None:
        if self._connect is None:
            from websockets.asyncio.client import connect

            self._connect = connect
        backoff = 5.0
        registered = False
        try:
            while True:
                try:
                    if not registered:
                        await self.register_commands()
                        registered = True
                    info = await self._api("GET", "/gateway/bot")
                    async with self._connect(f"{info['url']}/?v=10&encoding=json", max_size=2**22) as ws:
                        await self._session(ws)
                    backoff = 5.0
                except asyncio.CancelledError:
                    raise
                except FatalGatewayError as e:
                    log.error("%s - Discord commands disabled (alerts are still posted)", e)
                    return
                except Exception as e:  # noqa: BLE001
                    log.warning("Discord gateway: %s: %s (reconnecting in %.0fs)", type(e).__name__, e, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 300.0)
        finally:
            await self._client.aclose()
