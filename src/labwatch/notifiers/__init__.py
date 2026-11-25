"""Notification channels. Importing this package registers all built-in notifiers."""

from labwatch.notifiers.base import (
    Notifier,
    NotifierConfigError,
    build_notifier,
    register,
    registered_types,
)

# Import built-in notifiers so they register themselves.
from labwatch.notifiers import console, discord, discord_bot, email, ntfy, telegram  # noqa: E402,F401

__all__ = [
    "Notifier",
    "NotifierConfigError",
    "build_notifier",
    "register",
    "registered_types",
]
