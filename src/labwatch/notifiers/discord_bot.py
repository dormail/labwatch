"""Discord bot notifier: posts each device's alerts into its own channel.

Config::

    - name: discord
      type: discord_bot
      bot_token: ${DISCORD_BOT_TOKEN}
      channels:                       # device -> channel id (Developer Mode -> Copy Channel ID)
        Flash: ${DISCORD_FLASH_CHANNEL}
        Ket: ${DISCORD_KET_CHANNEL}
      status_channel: ${DISCORD_STATUS_CHANNEL:-}   # optional: "labwatch started/stopped" etc.
      min_severity: warning           # default level per channel (/level changes it)
      mention: "@here"                # pinged on critical alerts only
      commands: true                  # slash commands (/status, /mute ...), see labwatch.discord_bot

Without ``status_channel``, messages about labwatch itself go to every channel.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from labwatch import __version__
from labwatch.models import AlertEvent, Severity
from labwatch.notifiers._common import ConfigReader, HttpNotifier, fan_out
from labwatch.notifiers.base import register
from labwatch.notifiers.discord import _MENTION_EVERYONE, _MENTION_ROLE, _MENTION_USER, build_message

log = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"
USER_AGENT = f"DiscordBot (https://github.com/dormail/labwatch, {__version__})"


@register("discord_bot")
class DiscordBotNotifier(HttpNotifier):
    def __init__(
        self,
        name: str,
        min_severity: Severity,
        bot_token: str,
        channels: dict[str, str],
        *,
        status_channel: str | None = None,
        mention: str | None = None,
        commands: bool = True,
        api_base: str = API_BASE,
        **http: Any,
    ) -> None:
        super().__init__(name, min_severity, **http)
        self._bot_token = bot_token
        self.channels = {dev: str(cid) for dev, cid in channels.items()}
        self.status_channel = str(status_channel) if status_channel else None
        self.mention = mention
        self.commands = commands
        self.api_base = api_base.rstrip("/")

    # Set by the Discord command bot: filters the channels by each channel's /level
    # and /mute. None = every matching channel (e.g. `labwatch test-notify`).
    recipients_fn: Callable[[AlertEvent], list[str]] | None = None

    @classmethod
    def from_config(cls, name: str, min_severity: Severity, cfg: dict[str, Any], **http: Any) -> DiscordBotNotifier:
        r = ConfigReader("discord_bot", name, cfg)
        token = r.require("bot_token")
        r.used.add("channels")
        channels = cfg.get("channels")
        if not isinstance(channels, dict) or not channels:
            raise r.error("'channels' must map device names to Discord channel ids, e.g. {Flash: 1234567890}")
        clean = {}
        for dev, cid in channels.items():
            cid = str(cid or "").strip()
            if not cid.isdigit():
                raise r.error(f"channel id for {dev!r} must be a number (Developer Mode -> Copy Channel ID)")
            clean[str(dev)] = cid
        status = r.string("status_channel")
        if status and not status.isdigit():
            raise r.error("'status_channel' must be a channel id")
        mention = r.string("mention")
        if mention and not (_MENTION_EVERYONE.search(mention) or _MENTION_ROLE.search(mention)
                            or _MENTION_USER.search(mention)):
            raise r.error("'mention' must be '@here', '@everyone', '<@&ROLE_ID>' or '<@USER_ID>'")
        commands = r.boolean("commands", True)
        api_base = r.url("api_base") or API_BASE
        r.finish()
        return cls(name, min_severity, token, clean, status_channel=status, mention=mention,
                   commands=commands, api_base=api_base, **http)

    def _secrets(self) -> list[str | None]:
        return [self._bot_token]

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self._bot_token}", "User-Agent": USER_AGENT}

    def channel_for_device(self, device: str) -> str | None:
        for dev, cid in self.channels.items():
            if dev.lower() == device.lower():
                return cid
        return None

    def default_channels(self, event: AlertEvent) -> list[str]:
        """The channel of the event's device; labwatch's own messages go to the status channel."""
        cid = self.channel_for_device(event.device)
        if cid is not None:
            return [cid]
        if self.status_channel:
            return [self.status_channel]
        return sorted(set(self.channels.values()))

    def accepts(self, event: AlertEvent) -> bool:
        if self.recipients_fn is not None:
            return True  # filtered per channel in recipients()
        return super().accepts(event)

    def recipients(self, event: AlertEvent) -> list[str]:
        if self.recipients_fn is not None:
            return self.recipients_fn(event)
        return self.default_channels(event)

    async def send(self, event: AlertEvent) -> None:
        await self.post_to(self.recipients(event), event)

    async def post_to(self, channels: list[str], event: AlertEvent) -> None:
        targets = list(dict.fromkeys(channels))
        payload = build_message(event, self.mention)
        errors = await fan_out(targets, lambda cid: self._post(
            f"{self.api_base}/channels/{cid}/messages", json=payload, headers=self.headers))
        for cid, e in errors.items():
            log.warning("discord: posting to channel %s failed: %s", cid, e)
        if targets and len(errors) == len(targets):
            raise next(iter(errors.values()))
