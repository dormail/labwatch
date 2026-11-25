"""Helpers shared by the built-in notifiers: config validation, redaction, HTTP."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

import httpx

from labwatch import __version__
from labwatch.models import AlertEvent, EventKind, Severity
from labwatch.notifiers.base import Notifier, NotifierConfigError

log = logging.getLogger(__name__)

# httpx logs every request URL at INFO level. Telegram bot tokens and Discord
# webhook secrets are part of the URL, so keep that logger quiet unless the
# application configured it explicitly.
if logging.getLogger("httpx").level == logging.NOTSET:
    logging.getLogger("httpx").setLevel(logging.WARNING)

HTTP_TIMEOUT = 15.0
USER_AGENT = f"labwatch/{__version__}"


class DeliveryError(RuntimeError):
    """A notifier could not deliver an event (message is safe to log)."""


def is_urgent(event: AlertEvent) -> bool:
    """A critical alert that is (still) firing: the case that should wake someone up."""
    return (
        event.kind in (EventKind.FIRING, EventKind.REMINDER) and event.severity >= Severity.CRITICAL
    )


def redact(text: str, secrets: Iterable[str | None]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


class ConfigReader:
    """Validates one notifier's config section with error messages naming the notifier."""

    def __init__(self, type_name: str, name: str, cfg: dict[str, Any]) -> None:
        self.what = f"{type_name} notifier {name!r}"
        self.cfg = cfg
        self.used: set[str] = set()

    def error(self, msg: str) -> NotifierConfigError:
        return NotifierConfigError(f"{self.what}: {msg}")

    def string(self, key: str, default: str | None = None, *, required: bool = False) -> str | None:
        self.used.add(key)
        value = self.cfg.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                raise self.error(f"missing required option '{key}'")
            return default
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise self.error(f"option '{key}' must be a string, got {type(value).__name__}")
        value = str(value).strip()
        if "${" in value:
            raise self.error(f"option '{key}' contains an unexpanded environment variable")
        return value

    def require(self, key: str) -> str:
        value = self.string(key, required=True)
        assert value is not None
        return value

    def string_list(self, key: str) -> list[str]:
        """Required non-empty list of strings; a string may be comma-separated."""
        self.used.add(key)
        value = self.cfg.get(key)
        items = value.split(",") if isinstance(value, str) else value
        if not isinstance(items, list) or not all(isinstance(v, str) for v in items):
            raise self.error(f"option '{key}' must be a string or a list of strings")
        items = [v.strip() for v in items if v.strip()]
        if not items:
            raise self.error(f"missing required option '{key}'")
        return items

    def integer(self, key: str, default: int) -> int:
        self.used.add(key)
        value = self.cfg.get(key, default)
        try:
            if isinstance(value, bool):
                raise ValueError
            return int(value)
        except (TypeError, ValueError):
            raise self.error(f"option '{key}' must be an integer") from None

    def boolean(self, key: str, default: bool) -> bool:
        self.used.add(key)
        value = self.cfg.get(key, default)
        if not isinstance(value, bool):
            raise self.error(f"option '{key}' must be true or false")
        return value

    def severity(self, key: str, default: Severity) -> Severity:
        self.used.add(key)
        value = self.cfg.get(key)
        if value is None:
            return default
        try:
            return Severity.parse(value)
        except ValueError as e:
            raise self.error(f"option '{key}': {e}") from None

    def url(self, key: str, default: str | None = None, *, required: bool = False) -> str | None:
        """An http(s) URL; the value is never echoed (it may contain a secret)."""
        value = self.string(key, default, required=required)
        if value is not None and not value.startswith(("https://", "http://")):
            raise self.error(f"option '{key}' must be an http(s):// URL")
        return value

    def finish(self) -> None:
        unknown = sorted(set(self.cfg) - self.used)
        if unknown:
            log.warning("%s: ignoring unknown option(s): %s", self.what, ", ".join(unknown))


class HttpNotifier(Notifier):
    """Base for notifiers that POST to an HTTP API.

    Pass ``client`` (owned by the caller) or ``transport`` (e.g.
    ``httpx.MockTransport`` in tests); otherwise a client is created lazily.
    Subclasses list values that must never appear in errors in ``_secrets()``.
    """

    def __init__(
        self,
        name: str,
        min_severity: Severity,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = HTTP_TIMEOUT,
    ) -> None:
        super().__init__(name, min_severity)
        self._client = client
        self._owns_client = client is None
        self._transport = transport
        self._timeout = timeout

    def _secrets(self) -> list[str | None]:
        return []

    def _fail(self, msg: str) -> DeliveryError:
        return DeliveryError(
            redact(f"{self.type_name} notifier {self.name!r}: {msg}", self._secrets())
        )

    async def _post(self, url: str, **kwargs: Any) -> httpx.Response:
        """POST and return the response; raise DeliveryError on transport errors or non-2xx."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                headers={"User-Agent": USER_AGENT},
            )
        try:
            resp = await self._client.post(url, **kwargs)
        except httpx.HTTPError as e:
            # `from None`: the original exception can carry the (secret) URL.
            raise self._fail(
                f"request failed: {type(e).__name__}: {str(e) or 'no details'}"
            ) from None
        if not resp.is_success:
            raise self._fail(f"HTTP {resp.status_code}: {self._snippet(resp)}")
        return resp

    @staticmethod
    def _snippet(resp: httpx.Response, limit: int = 200) -> str:
        text = " ".join(resp.text.split())
        return (text[:limit] + "...") if len(text) > limit else (text or "(empty response)")

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
