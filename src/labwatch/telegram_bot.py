"""Telegram bot commands: ask labwatch for the current status from your phone.

Runs inside ``labwatch run`` next to the Telegram notifier, using the same bot.
It long-polls ``getUpdates`` (outbound HTTPS only, no open ports) and answers
commands from authorized chats only (the notifier's ``chat_id`` /
``critical_chat_id`` plus ``allowed_chat_ids``). Everyone else is ignored:
anybody can find a bot by its name, but lab data stays private.

Commands (also registered with Telegram so they show up in the "/" menu):

  /status            overview of all devices
  /<device>          details for one device, e.g. /flash, /ket, /plassys
  /alerts            active alerts
  /help              list of commands
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from labwatch import report

if TYPE_CHECKING:
    from labwatch.engine import DeviceRuntime, Monitor
    from labwatch.notifiers.telegram import TelegramNotifier

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
    ) -> None:
        self.monitor = monitor
        self._token = token
        self.allowed = {str(c) for c in allowed_chat_ids if str(c).strip()}
        self.api_base = api_base.rstrip("/")
        self.poll_timeout = poll_timeout
        self._client = httpx.AsyncClient(timeout=poll_timeout + 15, transport=transport)
        self._warned_chats: set[str] = set()

    @classmethod
    def from_notifier(cls, monitor: Monitor, n: TelegramNotifier) -> TelegramCommandBot:
        chats = {str(n.chat_id)} | ({str(n.critical_chat_id)} if n.critical_chat_id else set())
        chats |= {str(c) for c in n.allowed_chat_ids}
        return cls(monitor, n._bot_token, chats, api_base=n.api_base)

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
        if not data.get("ok"):
            desc = str(data.get("description", "")).replace(self._token, "***")
            raise TelegramAPIError(f"{method}: HTTP {resp.status_code} {desc}")
        return data.get("result")

    def commands(self) -> list[dict[str, str]]:
        cmds = [{"command": "status", "description": "Overview of all devices"}]
        for rt in self.monitor.runtimes:
            what = "reachability" if rt.device.type_name == "ping" else "temperatures and state"
            cmds.append({"command": report.command_name(rt.name), "description": f"{rt.name}: {what}"})
        cmds += [
            {"command": "alerts", "description": "Active alerts"},
            {"command": "help", "description": "List commands"},
        ]
        return cmds

    async def register_commands(self) -> None:
        await self._call("setMyCommands", {"commands": self.commands()})

    async def _skip_backlog(self) -> int:
        """Ignore messages sent while labwatch wasn't running (don't answer stale commands)."""
        updates = await self._call("getUpdates", {"offset": -1, "timeout": 0})
        return updates[-1]["update_id"] + 1 if updates else 0

    async def reply(self, chat_id: int | str, text: str) -> None:
        await self._call("sendMessage", {
            "chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })

    # -- command handling --------------------------------------------------------

    def _device(self, name: str) -> DeviceRuntime | None:
        key = report.command_name(name)
        for rt in self.monitor.runtimes:
            if report.command_name(rt.name) == key:
                return rt
        return None

    def help_text(self) -> str:
        lines = ["<b>labwatch commands</b>"]
        lines += [f"/{c['command']} – {c['description']}" for c in self.commands()]
        return "\n".join(lines)

    def respond(self, text: str) -> str:
        parts = text.strip().split()
        cmd = parts[0].lstrip("/").split("@", 1)[0].lower() if parts else ""
        args = parts[1:]
        now = time.time()
        mutes = self.monitor.store.mutes(now)
        if cmd in ("start", "help", ""):
            return self.help_text()
        if cmd == "status":
            if args:
                rt = self._device(args[0])
                return report.device_html(rt, mutes, now) if rt else f"Unknown device {args[0]!r}.\n\n" + self.help_text()
            return report.overview_html(self.monitor.runtimes, mutes, now)
        if cmd == "alerts":
            return report.alerts_html(self.monitor.runtimes, now)
        rt = self._device(cmd)
        if rt is not None:
            return report.device_html(rt, mutes, now)
        return f"Unknown command /{cmd}.\n\n" + self.help_text()

    async def handle_update(self, update: dict[str, Any]) -> None:
        msg = update.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        text = msg.get("text") or ""
        if chat is None or not text:
            return
        if str(chat) not in self.allowed:
            if str(chat) not in self._warned_chats:
                self._warned_chats.add(str(chat))
                log.warning("Telegram: ignoring message from unauthorized chat %s "
                            "(add it to allowed_chat_ids to allow it)", chat)
                await self.reply(chat, "This is a private bot.")
            return
        if not text.startswith("/"):
            text = "/help"
        log.info("Telegram command from chat %s: %s", chat, text.split()[0][:40])
        await self.reply(chat, self.respond(text))

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
                        log.info("Telegram commands active (%d authorized chat(s))", len(self.allowed))
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
