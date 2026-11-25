"""Discord notifier: posts an embed to a channel webhook."""

from __future__ import annotations

import re
from typing import Any

from labwatch import __version__
from labwatch.models import AlertEvent, EventKind, Severity, format_quantity
from labwatch.notifiers._common import ConfigReader, HttpNotifier, is_urgent
from labwatch.notifiers.base import register

RED, ORANGE, GREEN, BLUE = 0xE74C3C, 0xE67E22, 0x2ECC71, 0x3498DB

_MENTION_EVERYONE = re.compile(r"@(everyone|here)\b")
_MENTION_ROLE = re.compile(r"<@&(\d+)>")
_MENTION_USER = re.compile(r"<@!?(\d+)>")


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _color(event: AlertEvent) -> int:
    if event.kind is EventKind.RESOLVED:
        return GREEN
    if event.kind in (EventKind.FIRING, EventKind.REMINDER):
        if event.severity >= Severity.CRITICAL:
            return RED
        if event.severity >= Severity.WARNING:
            return ORANGE
    return BLUE


def allowed_mentions(mention: str | None) -> dict[str, list[str]]:
    """Discord ``allowed_mentions`` that lets exactly the configured mention ping."""
    allowed: dict[str, list[str]] = {"parse": []}
    if not mention:
        return allowed
    if _MENTION_EVERYONE.search(mention):
        allowed["parse"].append("everyone")
    if roles := _MENTION_ROLE.findall(mention):
        allowed["roles"] = roles
    if users := _MENTION_USER.findall(mention):
        allowed["users"] = users
    return allowed


@register("discord")
class DiscordNotifier(HttpNotifier):
    """Config: ``webhook_url`` (required, secret), ``mention`` (optional: "@here",
    "@everyone", "<@&ROLE_ID>" or "<@USER_ID>"; only for critical firing/reminder
    events), ``username`` (default "labwatch")."""

    def __init__(
        self,
        name: str,
        min_severity: Severity,
        webhook_url: str,
        *,
        mention: str | None = None,
        username: str = "labwatch",
        **http: Any,
    ) -> None:
        super().__init__(name, min_severity, **http)
        self._webhook_url = webhook_url
        self.mention = mention
        self.username = username

    @classmethod
    def from_config(
        cls, name: str, min_severity: Severity, cfg: dict[str, Any], **http: Any
    ) -> DiscordNotifier:
        r = ConfigReader("discord", name, cfg)
        webhook_url = r.url("webhook_url", required=True) or ""
        mention = r.string("mention")
        if mention and allowed_mentions(mention) == {"parse": []}:
            raise r.error("'mention' must be '@here', '@everyone', '<@&ROLE_ID>' or '<@USER_ID>'")
        username = r.string("username") or "labwatch"
        r.finish()
        return cls(name, min_severity, webhook_url, mention=mention, username=username, **http)

    def _secrets(self) -> list[str | None]:
        # https://discord.com/api/webhooks/<id>/<token>: the token is the secret.
        return [self._webhook_url, self._webhook_url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]]

    def build_payload(self, event: AlertEvent) -> dict[str, Any]:
        embed: dict[str, Any] = {
            "title": _truncate(event.headline, 256),
            "color": _color(event),
            "timestamp": event.timestamp.isoformat(),
        }
        if event.message:
            embed["description"] = _truncate(event.message, 4000)
        fields = [("Device", event.device), ("Channel", event.channel)]
        if event.value is not None:
            fields.append(("Value", format_quantity(event.value, event.unit)))
        if event.since is not None:
            fields.append(("Since", f"<t:{int(event.since.timestamp())}:R>"))
        embed["fields"] = [
            {"name": k, "value": _truncate(str(v), 1024), "inline": True} for k, v in fields if v
        ]

        payload: dict[str, Any] = {"username": self.username, "embeds": [embed]}
        if self.mention and is_urgent(event):
            payload["content"] = _truncate(f"{self.mention} {event.headline}", 2000)
            payload["allowed_mentions"] = allowed_mentions(self.mention)
        else:
            payload["allowed_mentions"] = {"parse": []}
        return payload

    async def send(self, event: AlertEvent) -> None:
        # Non-2xx (including 429 rate limiting) raises; the dispatcher retries.
        # ?wait=true makes Discord confirm the message was created (200 + message)
        # instead of a fire-and-forget 204. Discord asks for a "DiscordBot (url, version)"
        # User-Agent and may block requests without one.
        await self._post(
            self._webhook_url,
            params={"wait": "true"},
            json=self.build_payload(event),
            headers={"User-Agent": f"DiscordBot (https://github.com/dormail/labwatch, {__version__})"},
        )
