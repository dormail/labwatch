"""E-mail notifier: plain-text mail via SMTP (stdlib ``smtplib`` in a worker thread)."""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Any

from labwatch.models import AlertEvent, Severity, format_quantity
from labwatch.notifiers._common import ConfigReader, DeliveryError, is_urgent, redact
from labwatch.notifiers.base import Notifier, register

log = logging.getLogger(__name__)

SMTP_TIMEOUT = 20.0
DEFAULT_PORTS = {"starttls": 587, "ssl": 465, "none": 25}


def _fmt_time(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _one_line(text: str) -> str:
    return " ".join(text.split())


@register("email")
class EmailNotifier(Notifier):
    """Config: ``smtp_host`` (required), ``smtp_port`` (default 587 / 465 for
    ssl / 25 for none), ``security`` ("starttls" default, "ssl", "none"),
    ``username`` + ``password`` (optional, secret), ``from`` (required),
    ``to`` (required; string, comma-separated string or list)."""

    def __init__(
        self,
        name: str,
        min_severity: Severity,
        *,
        smtp_host: str,
        sender: str,
        to: list[str],
        smtp_port: int = 587,
        security: str = "starttls",
        username: str | None = None,
        password: str | None = None,
        timeout: float = SMTP_TIMEOUT,
    ) -> None:
        super().__init__(name, min_severity)
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.security = security
        self.username = username
        self._password = password
        self.sender = sender
        self.to = to
        self.timeout = timeout

    @classmethod
    def from_config(cls, name: str, min_severity: Severity, cfg: dict[str, Any]) -> EmailNotifier:
        r = ConfigReader("email", name, cfg)
        host = r.require("smtp_host")
        security = (r.string("security") or "starttls").lower()
        if security not in DEFAULT_PORTS:
            raise r.error(f"'security' must be one of {', '.join(DEFAULT_PORTS)}, got {security!r}")
        port = r.integer("smtp_port", DEFAULT_PORTS[security])
        username, password = r.string("username"), r.string("password")
        if bool(username) != bool(password):
            raise r.error("'username' and 'password' must be given together")
        sender = r.require("from")
        to = r.string_list("to")
        for addr in [sender, *to]:
            if "@" not in parseaddr(addr)[1]:
                raise r.error(f"invalid e-mail address {addr!r}")
        r.finish()
        return cls(
            name,
            min_severity,
            smtp_host=host,
            smtp_port=port,
            security=security,
            username=username,
            password=password,
            sender=sender,
            to=to,
        )

    def build_message(self, event: AlertEvent) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = _one_line(event.headline)
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.to)
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=parseaddr(self.sender)[1].rpartition("@")[2] or None)
        if is_urgent(event):
            msg["X-Priority"] = "1"
            msg["Importance"] = "high"

        details = [("Device", event.device), ("Channel", event.channel)]
        if event.value is not None:
            details.append(("Value", format_quantity(event.value, event.unit)))
        if event.since is not None:
            details.append(("Since", _fmt_time(event.since)))
        details.append(("Time", _fmt_time(event.timestamp)))
        details.append(("Event", f"{event.kind.value}, severity {event.severity.label}"))
        footer = "\n".join(f"{k + ':':<9}{v}" for k, v in details if v)
        body = f"{event.message.rstrip()}\n\n" if event.message.strip() else ""
        msg.set_content(f"{body}-- \n{footer}\n\nSent by labwatch\n")
        return msg

    def _send_sync(self, msg: EmailMessage) -> None:
        ctx = ssl.create_default_context()
        if self.security == "ssl":
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(
                self.smtp_host, self.smtp_port, timeout=self.timeout, context=ctx
            )
        else:
            smtp = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.timeout)
        with smtp:
            if self.security == "starttls":
                smtp.starttls(context=ctx)
            if self.username and self._password:
                smtp.login(self.username, self._password)
            refused = smtp.send_message(msg)
        if refused:
            log.warning(
                "email notifier %r: some recipients were refused: %s", self.name, ", ".join(refused)
            )

    async def send(self, event: AlertEvent) -> None:
        msg = self.build_message(event)
        try:
            await asyncio.to_thread(self._send_sync, msg)
        except (smtplib.SMTPException, OSError) as e:
            detail = redact(f"{type(e).__name__}: {e}", [self._password])
            raise DeliveryError(
                f"email notifier {self.name!r}: sending via {self.smtp_host}:{self.smtp_port} failed: {detail}"
            ) from e
