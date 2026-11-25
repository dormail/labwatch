"""Telegram notifier: Bot API ``sendMessage``."""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from typing import Any, Callable

from labwatch.models import AlertEvent, EventKind, Severity
from labwatch.notifiers._common import ConfigReader, HttpNotifier, is_urgent
from labwatch.notifiers.base import register

DEFAULT_API_BASE = "https://api.telegram.org"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Recipient:
    chat_id: str | int
    silent: bool


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

    # Set by the Telegram command bot when several people subscribe: returns who gets
    # an event (each with their own level and mutes). None = just chat_id.
    recipients_fn: Callable[[AlertEvent], list[Recipient]] | None = None

    def _secrets(self) -> list[str | None]:
        return [self._bot_token]

    def accepts(self, event: AlertEvent) -> bool:
        if self.recipients_fn is not None:
            return True  # filtered per person in recipients()
        return super().accepts(event)

    def recipients(self, event: AlertEvent) -> list[Recipient]:
        if self.recipients_fn is not None:
            return self.recipients_fn(event)
        return [Recipient(self.target_chat(event), self.is_silent(event))]

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

    def build_payload(self, event: AlertEvent, recipient: Recipient | None = None) -> dict[str, Any]:
        r = recipient or Recipient(self.target_chat(event), self.is_silent(event))
        return {
            "chat_id": r.chat_id,
            "text": self.format_text(event),
            "parse_mode": "HTML",
            "disable_notification": r.silent,
        }

    async def send(self, event: AlertEvent) -> None:
        recipients = self.recipients(event)
        failures: list[Exception] = []
        for r in recipients:
            try:
                await self._send_to(event, r)
            except Exception as e:  # noqa: BLE001 - one blocked/broken chat must not stop the others
                failures.append(e)
                log.warning("telegram: delivery to chat %s failed: %s", r.chat_id, e)
        if failures and len(failures) == len(recipients):
            raise failures[0]

    async def _send_to(self, event: AlertEvent, recipient: Recipient) -> None:
        url = f"{self.api_base}/bot{self._bot_token}/sendMessage"
        resp = await self._post(url, json=self.build_payload(event, recipient))
        try:
            data = resp.json()
        except ValueError:
            raise self._fail(f"invalid JSON response: {self._snippet(resp)}") from None
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise self._fail(f"API error: {self._snippet(resp)}")
