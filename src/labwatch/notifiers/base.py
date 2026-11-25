"""Notifier interface and registry.

A notifier turns an :class:`~labwatch.models.AlertEvent` into a message on some
channel (Discord, e-mail, phone push, ...). Each concrete notifier registers
itself with :func:`register` under a ``type`` name used in the config file.
"""

from __future__ import annotations

import abc
import logging
from typing import Any, Callable

from labwatch.models import AlertEvent, EventKind, Severity

log = logging.getLogger(__name__)


class NotifierConfigError(ValueError):
    pass


class Notifier(abc.ABC):
    """Base class for all notifiers.

    Subclasses implement :meth:`send` (raise on failure, the dispatcher retries)
    and are constructed from their config dict via :meth:`from_config`.
    """

    type_name: str = ""
    # Also deliver monitoring status messages (started/stopped, alarms armed, daily
    # summary) regardless of min_severity. Set per notifier with `lifecycle: false`.
    lifecycle: bool = True

    def __init__(self, name: str, min_severity: Severity = Severity.WARNING) -> None:
        self.name = name
        self.min_severity = min_severity

    def accepts(self, event: AlertEvent) -> bool:
        """Whether this notifier wants to deliver ``event``.

        Test events always go through; everything else is filtered by severity
        (a RESOLVED event carries the severity of the alert it resolves).
        """
        if event.kind is EventKind.TEST:
            return True
        if self.lifecycle and is_lifecycle(event):
            return True
        return event.severity >= self.min_severity

    @abc.abstractmethod
    async def send(self, event: AlertEvent) -> None:
        """Deliver the event. Raise an exception on failure."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Release resources (HTTP clients, ...)."""

    @classmethod
    @abc.abstractmethod
    def from_config(cls, name: str, min_severity: Severity, cfg: dict[str, Any]) -> Notifier:
        """Build the notifier from its (already env-expanded) config section."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r} >= {self.min_severity.label}>"


LIFECYCLE_IDS = {"labwatch/startup", "labwatch/shutdown", "labwatch/summary", "labwatch/mute"}


def is_lifecycle(event: AlertEvent) -> bool:
    """Status messages about labwatch itself, as opposed to equipment alerts."""
    return event.kind is EventKind.INFO and (
        event.rule_id in LIFECYCLE_IDS or event.rule_id.endswith("/armed")
    )


_REGISTRY: dict[str, type[Notifier]] = {}


def register(type_name: str) -> Callable[[type[Notifier]], type[Notifier]]:
    def deco(cls: type[Notifier]) -> type[Notifier]:
        cls.type_name = type_name
        _REGISTRY[type_name] = cls
        return cls

    return deco


def build_notifier(cfg: dict[str, Any]) -> Notifier:
    """Create a notifier from a config dict with at least a ``type`` key."""
    cfg = dict(cfg)
    type_name = cfg.pop("type", None)
    if not type_name:
        raise NotifierConfigError(f"notifier config is missing 'type': {sorted(cfg)}")
    cls = _REGISTRY.get(type_name)
    if cls is None:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise NotifierConfigError(f"unknown notifier type {type_name!r}; known: {known}")
    name = cfg.pop("name", type_name)
    try:
        min_severity = Severity.parse(cfg.pop("min_severity", "warning"))
    except ValueError as e:
        raise NotifierConfigError(f"{type_name} notifier {name!r}: 'min_severity': {e}") from None
    lifecycle = cfg.pop("lifecycle", True)
    if not isinstance(lifecycle, bool):
        raise NotifierConfigError(f"{type_name} notifier {name!r}: 'lifecycle' must be true or false")
    notifier = cls.from_config(name, min_severity, cfg)
    notifier.lifecycle = lifecycle
    return notifier


def registered_types() -> list[str]:
    return sorted(_REGISTRY)
