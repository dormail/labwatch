"""Telegram notifier: Bot API ``sendMessage``."""

from __future__ import annotations

import html
from typing import Any

from labwatch.models import AlertEvent, EventKind, Severity
from labwatch.notifiers._common import ConfigReader, HttpNotifier, is_urgent
from labwatch.notifiers.base import register

DEFAULT_API_BASE = "https://api.telegram.org"


@register("telegram")
class TelegramNotifier(HttpNotifier):
    """Config: ``bot_token`` (required, secret), ``chat_id`` (required),
    ``critical_chat_id`` (optional: critical firing/reminder events go there
    instead), ``silent_below`` (severity, default "critical": less severe events
    are delivered without sound; resolved/info always silent), ``api_base``,
    ``commands`` (default true: answer /status etc., see labwatch.telegram_bot),
    ``allowed_chat_ids`` (extra chats allowed to use commands)."""

    def __init__(
        self,
        name: str,
        min_severity: Severity,
        bot_token: str,
        chat_id: str | int,
        *,
        critical_chat_id: str | int | None = None,
        silent_below: Severity = Severity.CRITICAL,
        api_base: str = DEFAULT_API_BASE,
        commands: bool = True,
        allowed_chat_ids: list[str] | None = None,
        **http: Any,
    ) -> None:
        super().__init__(name, min_severity, **http)
        self._bot_token = bot_token
        self.chat_id = chat_id
        self.critical_chat_id = critical_chat_id
        self.silent_below = silent_below
        self.api_base = api_base.rstrip("/")
        self.commands = commands
        self.allowed_chat_ids = list(allowed_chat_ids or [])

    @classmethod
    def from_config(
        cls, name: str, min_severity: Severity, cfg: dict[str, Any], **http: Any
    ) -> TelegramNotifier:
        r = ConfigReader("telegram", name, cfg)
        token = r.require("bot_token")
        if ":" not in token or "/" in token or any(c.isspace() for c in token):
            raise r.error(
                "'bot_token' looks malformed (expected '<bot id>:<secret>' from @BotFather)"
            )
        chat_id = r.require("chat_id")
        critical_chat_id = r.string("critical_chat_id")
        silent_below = r.severity("silent_below", Severity.CRITICAL)
        api_base = r.url("api_base") or DEFAULT_API_BASE
        commands = r.boolean("commands", True)
        r.used.add("allowed_chat_ids")
        allowed = cfg.get("allowed_chat_ids") or []
        if isinstance(allowed, (str, int)):
            allowed = [allowed]
        if not isinstance(allowed, list):
            raise r.error("'allowed_chat_ids' must be a chat id or a list of chat ids")
        r.finish()
        return cls(
            name,
            min_severity,
            token,
            chat_id,
            critical_chat_id=critical_chat_id,
            silent_below=silent_below,
            api_base=api_base,
            commands=commands,
            allowed_chat_ids=[str(a) for a in allowed],
            **http,
        )

    def _secrets(self) -> list[str | None]:
        return [self._bot_token]

    def target_chat(self, event: AlertEvent) -> str | int:
        if self.critical_chat_id is not None and is_urgent(event):
            return self.critical_chat_id
        return self.chat_id

    def is_silent(self, event: AlertEvent) -> bool:
        if event.kind in (EventKind.RESOLVED, EventKind.INFO):
            return True
        return event.severity < self.silent_below

    @staticmethod
    def format_text(event: AlertEvent) -> str:
        # Telegram limits messages to 4096 characters (after entity parsing).
        message = event.message if len(event.message) <= 3500 else event.message[:3500] + "…"
        text = f"<b>{html.escape(event.headline)}</b>"
        if message:
            text += "\n" + html.escape(message)
        return text

    def build_payload(self, event: AlertEvent) -> dict[str, Any]:
        return {
            "chat_id": self.target_chat(event),
            "text": self.format_text(event),
            "parse_mode": "HTML",
            "disable_notification": self.is_silent(event),
        }

    async def send(self, event: AlertEvent) -> None:
        url = f"{self.api_base}/bot{self._bot_token}/sendMessage"
        resp = await self._post(url, json=self.build_payload(event))
        try:
            data = resp.json()
        except ValueError:
            raise self._fail(f"invalid JSON response: {self._snippet(resp)}") from None
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise self._fail(f"API error: {self._snippet(resp)}")
