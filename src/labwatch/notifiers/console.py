"""Console notifier: writes events to the log (and optionally stdout)."""

from __future__ import annotations

import logging
from typing import Any

from labwatch.models import AlertEvent, EventKind, Severity
from labwatch.notifiers._common import ConfigReader
from labwatch.notifiers.base import Notifier, register

log = logging.getLogger(__name__)

_LEVELS = {
    Severity.CRITICAL: logging.ERROR,
    Severity.WARNING: logging.WARNING,
    Severity.INFO: logging.INFO,
}


@register("console")
class ConsoleNotifier(Notifier):
    """Config: ``stdout`` (bool, default false) - also print each event to stdout."""

    def __init__(
        self, name: str, min_severity: Severity = Severity.WARNING, *, stdout: bool = False
    ) -> None:
        super().__init__(name, min_severity)
        self.stdout = stdout

    @classmethod
    def from_config(cls, name: str, min_severity: Severity, cfg: dict[str, Any]) -> ConsoleNotifier:
        r = ConfigReader("console", name, cfg)
        stdout = r.boolean("stdout", False)
        r.finish()
        return cls(name, min_severity, stdout=stdout)

    async def send(self, event: AlertEvent) -> None:
        # Resolutions and info messages are good news; log them at INFO.
        if event.kind in (EventKind.FIRING, EventKind.REMINDER):
            level = _LEVELS.get(event.severity, logging.WARNING)
        else:
            level = logging.INFO
        log.log(level, "%s%s", event.headline, f": {event.message}" if event.message else "")
        if self.stdout:
            ts = event.timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            text = f"{ts}  {event.headline}"
            if event.message:
                text += "\n" + "\n".join(f"    {line}" for line in event.message.splitlines())
            print(text, flush=True)
