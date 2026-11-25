"""Telegram bot: status commands, several users, personal and global mutes.

Runs inside ``labwatch run`` next to the Telegram notifier, using the same bot.
It long-polls ``getUpdates`` (outbound HTTPS only, no open ports).

People
------
* **Admins**: the notifier's ``chat_id`` (and ``critical_chat_id``). They approve others.
* **Users**: anyone an admin approved (or listed in ``allowed_chat_ids``).
* Anybody else who messages the bot gets "access requested" and the admins get
  ``/approve <id>``. Until approved, they see no lab data.

Every user and admin receives alerts in their own chat, filtered by their own
``/level`` and personal ``/mute``s. Mutes for everyone (``/muteall`` or
``labwatch mute`` on the server) are announced to everybody by the monitor.

Mute targets: a device (``Flash``), an alarm type (``anomaly``, ``pulse_tube``,
``circulation``, ``cooling_water``, ``temperature``, ``pressure``, ``fse``,
``connection``, ``labwatch``), both (``Ket pumps`` / ``Ket/circulation``), or ``all``.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from typing import TYPE_CHECKING, Any

import httpx

from labwatch import report, subscriptions
from labwatch.models import AlertEvent, Severity
from labwatch.notifiers._common import is_urgent
from labwatch.subscriptions import CATEGORY_HELP, fmt_until, is_duration

if TYPE_CHECKING:
    from labwatch.engine import DeviceRuntime, Monitor
    from labwatch.notifiers.telegram import Recipient, TelegramNotifier

log = logging.getLogger(__name__)

POLL_TIMEOUT = 50  # seconds, Telegram long polling


class TelegramAPIError(Exception):
    pass


class TelegramCommandBot:
    def __init__(
        self,
        monitor: Monitor,
        token: str,
        allowed_chat_ids: set[str],
        api_base: str = "https://api.telegram.org",
        transport: httpx.AsyncBaseTransport | None = None,
        poll_timeout: int = POLL_TIMEOUT,
        admin_chat_ids: set[str] | None = None,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self.monitor = monitor
        self.store = monitor.store
        self._token = token
        self.admins = {str(c) for c in (admin_chat_ids if admin_chat_ids is not None else allowed_chat_ids) if str(c).strip()}
        self.preapproved = {str(c) for c in allowed_chat_ids if str(c).strip()} - self.admins
        self.api_base = api_base.rstrip("/")
        self.poll_timeout = poll_timeout
        self.notifier = notifier
        self._client = httpx.AsyncClient(timeout=poll_timeout + 15, transport=transport)
        self._pending_replied: dict[str, float] = {}
        self._bootstrap()

    @classmethod
    def from_notifier(cls, monitor: Monitor, n: TelegramNotifier) -> TelegramCommandBot:
        admins = {str(n.chat_id)} | ({str(n.critical_chat_id)} if n.critical_chat_id else set())
        bot = cls(monitor, n._bot_token, admins | {str(c) for c in n.allowed_chat_ids},
                  api_base=n.api_base, admin_chat_ids=admins, notifier=n)
        n.recipients_fn = bot.recipients_for
        return bot

    def _bootstrap(self) -> None:
        """Config chats always exist: admins from chat_id, users from allowed_chat_ids."""
        level = self.notifier.min_severity.label if self.notifier else "warning"
        for cid in self.admins:
            s = self.store.subscriber(cid)
            self.store.upsert_subscriber(cid, s.name if s else "admin", "admin", s.level if s else level)
        for cid in self.preapproved:
            s = self.store.subscriber(cid)
            if s is None or s.role == "pending":
                self.store.upsert_subscriber(cid, s.name if s else "", "user", s.level if s else level)

    # -- who gets an alert -----------------------------------------------------------

    def recipients_for(self, event: AlertEvent) -> list[Recipient]:
        from labwatch.notifiers.telegram import Recipient

        n = self.notifier
        now = time.time()
        out = []
        lifecycle = n.lifecycle if n is not None else True
        for s in self.store.subscribers():
            if s.chat_id.startswith("discord:"):
                continue  # Discord channels are served by the Discord bot
            if not subscriptions.wants(s, event, self.store, lifecycle, now):
                continue
            chat: str | int = s.chat_id
            if n is not None and n.critical_chat_id and str(s.chat_id) == str(n.chat_id) and is_urgent(event):
                chat = n.critical_chat_id
            silent = n.is_silent(event) if n is not None else event.severity < Severity.CRITICAL
            out.append(Recipient(chat, silent))
        return out

    # -- Telegram API ------------------------------------------------------------

    async def _call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{self.api_base}/bot{self._token}/{method}"
        try:
            resp = await self._client.post(url, json=payload or {})
        except httpx.HTTPError as e:
            raise TelegramAPIError(f"{method}: {type(e).__name__}") from None  # URL holds the token
        try:
            data = resp.json()
        except ValueError:
            raise TelegramAPIError(f"{method}: HTTP {resp.status_code}, invalid JSON") from None
        if not isinstance(data, dict):
            raise TelegramAPIError(f"{method}: HTTP {resp.status_code}, unexpected response")
        if not data.get("ok"):
            desc = str(data.get("description", "")).replace(self._token, "***")
            raise TelegramAPIError(f"{method}: HTTP {resp.status_code} {desc}")
        return data.get("result")

    def commands(self, admin: bool = False) -> list[dict[str, str]]:
        cmds = [{"command": "status", "description": "Overview of all devices"}]
        for rt in self.monitor.runtimes:
            what = "reachability" if rt.device.type_name == "ping" else "temperatures and state"
            cmds.append({"command": report.command_name(rt.name), "description": f"{rt.name}: {what}"})
        cmds += [
            {"command": "alerts", "description": "Active alerts"},
            {"command": "mute", "description": "Mute for yourself: /mute Flash 4h, /mute anomaly"},
            {"command": "unmute", "description": "Unmute for yourself: /unmute Flash, /unmute all"},
            {"command": "mutes", "description": "Your mutes and mutes for everyone"},
            {"command": "muteall", "description": "Mute for everyone: /muteall Flash 2d warm-up"},
            {"command": "unmuteall", "description": "Remove a mute for everyone"},
            {"command": "level", "description": "What you get: /level critical|warning|info"},
            {"command": "help", "description": "List commands"},
        ]
        if admin:
            cmds += [
                {"command": "users", "description": "Admin: list users and requests"},
                {"command": "approve", "description": "Admin: /approve <chat id>"},
                {"command": "remove", "description": "Admin: /remove <chat id>"},
            ]
        return cmds

    async def register_commands(self) -> None:
        await self._call("setMyCommands", {"commands": self.commands()})
        for cid in self.admins:  # admins additionally see the admin commands
            try:
                await self._call("setMyCommands", {"commands": self.commands(admin=True),
                                                   "scope": {"type": "chat", "chat_id": cid}})
            except TelegramAPIError as e:
                log.info("Telegram: admin command menu for chat %s: %s", cid, e)

    async def _skip_backlog(self) -> int:
        """Ignore messages sent while labwatch wasn't running (don't answer stale commands)."""
        updates = await self._call("getUpdates", {"offset": -1, "timeout": 0})
        return updates[-1]["update_id"] + 1 if updates else 0

    async def reply(self, chat_id: int | str, text: str) -> None:
        await self._call("sendMessage", {
            "chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })

    async def tell_admins(self, text: str) -> None:
        for cid in {s.chat_id for s in self.store.subscribers(("admin",))}:
            try:
                await self.reply(cid, text)
            except TelegramAPIError as e:
                log.warning("Telegram: could not notify admin %s: %s", cid, e)

    # -- targets ---------------------------------------------------------------------

    def _categories(self) -> list[str]:
        return subscriptions.categories(self.monitor)

    def _device(self, name: str) -> DeviceRuntime | None:
        return subscriptions.find_device(self.monitor, name)

    def parse_target(self, tokens: list[str]) -> tuple[str | None, list[str], str]:
        return subscriptions.parse_target(self.monitor, tokens)

    def targets_help(self) -> str:
        devs = ", ".join(rt.name for rt in self.monitor.runtimes)
        cats = "\n".join(f"  • <b>{c}</b> – {html.escape(CATEGORY_HELP.get(c, c))}" for c in self._categories())
        return (f"Devices: {html.escape(devs)}\nAlarm types:\n{cats}\n"
                "Combine: <code>/mute Ket circulation 1d</code> · everything: <code>/mute all 8h</code>\n"
                "Duration: 30m, 4h, 2d, forever (default 2h)")

    # -- command handling --------------------------------------------------------

    def help_text(self, admin: bool = False) -> str:
        lines = ["<b>labwatch commands</b>"]
        lines += [f"/{c['command']} – {html.escape(c['description'])}" for c in self.commands(admin)]
        return "\n".join(lines)

    def respond(self, text: str, chat_id: str | int | None = None, name: str = "") -> str:
        parts = text.strip().split()
        cmd = parts[0].lstrip("/").split("@", 1)[0].lower() if parts else ""
        args = parts[1:]
        now = time.time()
        sub = self.store.subscriber(chat_id) if chat_id is not None else None
        is_admin = sub is not None and sub.role == "admin"
        mutes = self.store.mutes(now)
        if cmd in ("start", "help", ""):
            return self.help_text(is_admin)
        if cmd == "status":
            if args:
                rt = self._device(args[0])
                return report.device_html(rt, mutes, now) if rt else f"Unknown device {html.escape(args[0])!r}."
            return report.overview_html(self.monitor.runtimes, mutes, now)
        if cmd == "alerts":
            return report.alerts_html(self.monitor.runtimes, now)
        if cmd in ("mute", "muteall"):
            return self._mute(cmd == "muteall", args, chat_id, name or (sub.name if sub else ""), now)
        if cmd == "unmute":
            if not args or chat_id is None:
                return "Usage: /unmute Flash · /unmute anomaly · /unmute all"
            target, _, err = self.parse_target(args) if args[0].lower() != "all" else ("all", [], "")
            if err:
                return f"{html.escape(err)}.\n\n{self.targets_help()}"
            n = self.store.personal_unmute(chat_id, target if target != "*" else "*")
            return f"🔔 Unmuted {html.escape(target)} for you." if n else f"You had no mute for {html.escape(target)}. /mutes"
        if cmd == "unmuteall":
            if not args:
                return "Usage: /unmuteall Flash"
            target, _, err = self.parse_target(args)
            if err:
                return f"{html.escape(err)}.\n\n{self.targets_help()}"
            ok = self.store.unmute(target)
            return ("🔔 Removed the mute for everyone; everybody will be told." if ok
                    else f"{html.escape(target)} wasn't muted for everyone. /mutes")
        if cmd == "mutes":
            return self._mutes_text(chat_id, mutes, now)
        if cmd == "level":
            if chat_id is None or sub is None:
                return "Not available."
            if not args:
                return f"You get <b>{sub.level}</b> and above. Change: /level critical · /level warning · /level info"
            try:
                level = Severity.parse(args[0])
            except ValueError:
                return "Use /level critical, /level warning or /level info."
            self.store.upsert_subscriber(chat_id, sub.name, sub.role, level.label)
            what = {"critical": "only critical alerts (the ones that ring)",
                    "warning": "warnings (silent) and critical alerts", "info": "everything"}[level.label]
            return f"OK: you now get {what}. Status messages (started/stopped) come regardless; /mute labwatch hides them."
        if cmd in ("users", "approve", "remove"):
            if not is_admin:
                return "Only admins can do that."
            return self._admin(cmd, args)
        rt = self._device(cmd)
        if rt is not None:
            return report.device_html(rt, mutes, now)
        return f"Unknown command /{html.escape(cmd)}.\n\n" + self.help_text(is_admin)

    def _mute(self, everyone: bool, args: list[str], chat_id, name: str, now: float) -> str:
        cmd = "/muteall" if everyone else "/mute"
        if not args:
            return (f"<b>{cmd}</b> what [for how long]{' [reason]' if everyone else ''}\n\n"
                    f"{self.targets_help()}")
        target, rest, err = self.parse_target(args)
        if err:
            return f"{html.escape(err)}.\n\n{self.targets_help()}"
        duration: float | None = subscriptions.DEFAULT_MUTE
        if rest and is_duration(rest[0]):
            duration = subscriptions.parse_duration(rest.pop(0))
        until = None if duration is None else now + duration
        if everyone:
            if until is None or duration > subscriptions.MAX_GLOBAL_MUTE:
                return "Mutes for everyone need a duration of at most 14 days, e.g. /muteall Flash 2d warm-up."
            reason = " ".join(rest).strip()
            by = f"by {name}" if name else ""
            self.store.mute(target, until, "; ".join(x for x in (reason, by) if x))
            return f"🔕 Muted {html.escape(target)} for everyone {fmt_until(until)}. Everybody will be told."
        if chat_id is None:
            return "Not available."
        self.store.personal_mute(chat_id, target, until)
        return (f"🔕 Muted {html.escape(target)} for you {fmt_until(until)}.\n"
                f"Others still get these alerts. Undo: /unmute {html.escape(target)}")

    def _mutes_text(self, chat_id, global_mutes, now: float) -> str:
        lines = []
        mine = self.store.personal_mutes(chat_id, now) if chat_id is not None else []
        if mine:
            lines.append("<b>Muted for you</b>")
            lines += [f"  🔕 {html.escape(m.target)} {fmt_until(m.until)}" for m in mine]
        if global_mutes:
            lines.append("<b>Muted for everyone</b>")
            lines += [f"  🔕 {html.escape(m.target)} {fmt_until(m.until)}"
                      + (f" ({html.escape(m.reason)})" if m.reason else "") for m in global_mutes]
        return "\n".join(lines) if lines else "Nothing is muted."

    def _admin(self, cmd: str, args: list[str]) -> str:
        if cmd == "users":
            rows = []
            for role in ("admin", "user", "pending"):
                for s in self.store.subscribers((role,)):
                    rows.append(f"{'👑' if role == 'admin' else '⏳' if role == 'pending' else '👤'} "
                                f"{html.escape(s.name or '?')} – <code>{s.chat_id}</code> – {role}, {s.level}")
            return "\n".join(rows) or "No users."
        if not args:
            return f"Usage: /{cmd} &lt;chat id&gt; (see /users)"
        s = self.store.subscriber(args[0])
        if s is None:
            return f"No user with chat id {html.escape(args[0])}. See /users"
        if cmd == "approve":
            self.store.upsert_subscriber(s.chat_id, s.name, "user", s.level)
            self._to_notify.append((s.chat_id, "✅ You now receive labwatch alerts. /help shows what you can do, "
                                               "/level and /mute let you choose what you get."))
            return f"✅ {html.escape(s.name or s.chat_id)} can now use labwatch."
        if s.role == "admin" and s.chat_id in self.admins:
            return "Admins from the config file can't be removed here."
        self.store.remove_subscriber(s.chat_id)
        return f"Removed {html.escape(s.name or s.chat_id)}."

    _to_notify: list[tuple[str, str]] = []

    @staticmethod
    def _chat_name(msg: dict[str, Any]) -> str:
        chat = msg.get("chat") or {}
        if chat.get("title"):
            return str(chat["title"])
        frm = msg.get("from") or chat
        name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
        if frm.get("username"):
            name += f" (@{frm['username']})"
        return name.strip() or str(chat.get("id"))

    async def handle_update(self, update: dict[str, Any]) -> None:
        msg = update.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        text = msg.get("text") or ""
        if chat is None or not text:
            return
        cid = str(chat)
        name = self._chat_name(msg)
        sub = self.store.subscriber(cid)
        if sub is None:
            self.store.upsert_subscriber(cid, name, "pending", "warning")
            log.warning("Telegram: access request from %s (chat %s)", name, cid)
            await self.reply(cid, "👋 This is a private lab monitoring bot. I've asked an admin to give you "
                                  "access; you'll get a message when that happens.")
            await self.tell_admins(f"🔑 <b>Access request</b> from {html.escape(name)} (chat <code>{cid}</code>)\n"
                                   f"Approve: /approve {cid}\nIgnore or reject: /remove {cid}")
            return
        if sub.role == "pending":
            if time.time() - self._pending_replied.get(cid, 0) > 600:
                self._pending_replied[cid] = time.time()
                await self.reply(cid, "⏳ Still waiting for an admin to approve you.")
            return
        if sub.name != name and name:
            self.store.upsert_subscriber(cid, name, sub.role, sub.level)
        if not text.startswith("/"):
            text = "/help"
        log.info("Telegram command from %s: %s", name, text.split()[0][:40])
        self._to_notify = []
        await self.reply(cid, self.respond(text, cid, name))
        for target, note in self._to_notify:
            try:
                await self.reply(target, note)
            except TelegramAPIError as e:
                log.warning("Telegram: could not notify %s: %s", target, e)
        self._to_notify = []

    # -- main loop -----------------------------------------------------------------

    async def run(self) -> None:
        delay = 5.0
        offset: int | None = None
        try:
            while True:
                try:
                    if offset is None:
                        await self.register_commands()
                        offset = await self._skip_backlog()
                        log.info("Telegram commands active (%d admin(s), %d user(s))",
                                 len(self.store.subscribers(("admin",))), len(self.store.subscribers(("user",))))
                    updates = await self._call("getUpdates", {
                        "offset": offset, "timeout": self.poll_timeout, "allowed_updates": ["message"],
                    })
                    delay = 5.0
                except TelegramAPIError as e:
                    # 409 = another labwatch (or bot) is polling the same token.
                    log.warning("Telegram commands: %s (retrying in %.0fs)", e, delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 300.0)
                    continue
                for u in updates or []:
                    offset = u["update_id"] + 1
                    try:
                        await self.handle_update(u)
                    except Exception:  # noqa: BLE001 - one bad message must not stop the bot
                        log.exception("Telegram: handling a command failed")
        finally:
            await self._client.aclose()
