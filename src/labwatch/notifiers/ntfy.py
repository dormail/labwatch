"""ntfy notifier: phone push via https://ntfy.sh or a self-hosted ntfy server.

Uses JSON publishing (POST to the server root), which is unicode-safe, unlike
ntfy's header-based API.
"""

from __future__ import annotations

import re
from typing import Any

from labwatch.models import AlertEvent, EventKind, Severity
from labwatch.notifiers._common import ConfigReader, HttpNotifier, is_urgent
from labwatch.notifiers.base import register

DEFAULT_SERVER = "https://ntfy.sh"
_TOPIC_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

_TAGS = {
    EventKind.RESOLVED: "white_check_mark",
    EventKind.INFO: "information_source",
    EventKind.TEST: "test_tube",
}


def priority(event: AlertEvent) -> int:
    """ntfy priority: 5 = urgent (can bypass Do-Not-Disturb), 4 = high, 3 = default."""
    if is_urgent(event):
        return 5
    if event.kind in (EventKind.FIRING, EventKind.REMINDER) and event.severity >= Severity.WARNING:
        return 4
    return 3


def tags(event: AlertEvent) -> list[str]:
    if event.kind in _TAGS:
        return [_TAGS[event.kind]]
    if event.severity >= Severity.CRITICAL:
        return ["rotating_light"]
    if event.severity >= Severity.WARNING:
        return ["warning"]
    return ["information_source"]


@register("ntfy")
class NtfyNotifier(HttpNotifier):
    """Config: ``topic`` (required; on the public server anyone who knows it can
    read the messages, so treat it like a password), ``server`` (default
    https://ntfy.sh), ``token`` (optional access token, sent as Bearer auth)."""

    def __init__(
        self,
        name: str,
        min_severity: Severity,
        topic: str,
        *,
        server: str = DEFAULT_SERVER,
        token: str | None = None,
        **http: Any,
    ) -> None:
        super().__init__(name, min_severity, **http)
        self._topic = topic
        self.server = server.rstrip("/")
        self._token = token

    @classmethod
    def from_config(
        cls, name: str, min_severity: Severity, cfg: dict[str, Any], **http: Any
    ) -> NtfyNotifier:
        r = ConfigReader("ntfy", name, cfg)
        topic = r.require("topic")
        if not _TOPIC_RE.fullmatch(topic):
            raise r.error("'topic' may only contain letters, digits, '-' and '_' (max. 64)")
        server = r.url("server") or DEFAULT_SERVER
        token = r.string("token")
        r.finish()
        return cls(name, min_severity, topic, server=server, token=token, **http)

    def _secrets(self) -> list[str | None]:
        return [self._topic, self._token]

    def build_payload(self, event: AlertEvent) -> dict[str, Any]:
        return {
            "topic": self._topic,
            "title": event.headline,
            "message": event.message or event.headline,
            "priority": priority(event),
            "tags": tags(event),
        }

    async def send(self, event: AlertEvent) -> None:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        await self._post(self.server + "/", json=self.build_payload(event), headers=headers)
