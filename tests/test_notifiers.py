from __future__ import annotations

import asyncio
import json
import logging
import smtplib
from datetime import datetime, timezone
from typing import ClassVar

import httpx
import pytest

from labwatch.models import AlertEvent, EventKind, Severity
from labwatch.notifiers import NotifierConfigError, build_notifier
from labwatch.notifiers._common import DeliveryError
from labwatch.notifiers.discord import BLUE, GREEN, ORANGE, RED, DiscordNotifier
from labwatch.notifiers.ntfy import NtfyNotifier
from labwatch.notifiers.telegram import TelegramNotifier

WEBHOOK_SECRET = "s3cr3t-webhook-token_XYZ"
WEBHOOK = f"https://discord.com/api/webhooks/1234567890/{WEBHOOK_SECRET}"
BOT_TOKEN = "123456789:AAH-s3cr3t-bot-token"
NTFY_TOPIC = "labwatch-k3j4h5g6-secret-topic"


def make_event(
    kind: EventKind = EventKind.FIRING, severity: Severity = Severity.CRITICAL, **kw
) -> AlertEvent:
    fields = {
        "rule_id": "Flash/T4K above 6",
        "device": "Flash",
        "title": "Flash: T4K above 6 K",
        "message": "T4K = 6.2 K (limit 6 K)\nPulse tube probably stopped.",
        "channel": "T4K",
        "value": 6.2,
        "unit": "K",
        "since": datetime(2026, 11, 25, 9, 55, tzinfo=timezone.utc),
        "timestamp": datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc),
    }
    fields.update(kw)
    return AlertEvent(kind=kind, severity=severity, **fields)


class Recorder:
    """httpx.MockTransport handler that records requests and returns a canned response."""

    def __init__(
        self,
        status: int = 200,
        json_body=None,
        text: str | None = None,
        exc: Exception | None = None,
    ):
        self.status, self.json_body, self.text, self.exc = status, json_body, text, exc
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        if self.text is not None:
            return httpx.Response(self.status, text=self.text)
        return httpx.Response(self.status, json=self.json_body)

    @property
    def payload(self) -> dict:
        return json.loads(self.requests[-1].content)


def send(notifier, event: AlertEvent) -> None:
    async def run():
        try:
            await notifier.send(event)
        finally:
            await notifier.aclose()

    asyncio.run(run())


# --- Discord -----------------------------------------------------------------


def discord(rec: Recorder, **cfg) -> DiscordNotifier:
    return DiscordNotifier.from_config(
        "dc", Severity.INFO, {"webhook_url": WEBHOOK, **cfg}, transport=rec.transport
    )


def test_discord_critical_firing_embed_and_mention():
    rec = Recorder(status=204)
    send(discord(rec, mention="<@&42>"), make_event())

    req = rec.requests[0]
    assert req.method == "POST" and str(req.url) == WEBHOOK
    p = rec.payload
    assert p["username"] == "labwatch"
    assert p["content"] == "<@&42> [CRITICAL] Flash: T4K above 6 K"
    assert p["allowed_mentions"] == {"parse": [], "roles": ["42"]}
    (embed,) = p["embeds"]
    assert embed["title"] == "[CRITICAL] Flash: T4K above 6 K"
    assert embed["description"].startswith("T4K = 6.2 K")
    assert embed["color"] == RED
    assert embed["timestamp"] == "2026-11-25T10:00:00+00:00"
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Device"] == "Flash" and fields["Channel"] == "T4K" and fields["Value"] == "6.2 K"
    assert (
        fields["Since"]
        == f"<t:{int(datetime(2026, 11, 25, 9, 55, tzinfo=timezone.utc).timestamp())}:R>"
    )


@pytest.mark.parametrize(
    "kind, severity, color",
    [
        (EventKind.FIRING, Severity.WARNING, ORANGE),
        (EventKind.RESOLVED, Severity.CRITICAL, GREEN),
        (EventKind.INFO, Severity.INFO, BLUE),
        (EventKind.TEST, Severity.CRITICAL, BLUE),
    ],
)
def test_discord_mention_only_for_critical_firing(kind, severity, color):
    rec = Recorder(status=204)
    send(discord(rec, mention="@here", username="Fridge bot"), make_event(kind, severity))
    p = rec.payload
    assert "content" not in p
    assert p["allowed_mentions"] == {"parse": []}
    assert p["username"] == "Fridge bot"
    assert p["embeds"][0]["color"] == color


def test_discord_reminder_here_mention_pings_everyone():
    rec = Recorder(status=204)
    send(discord(rec, mention="@here"), make_event(EventKind.REMINDER))
    assert rec.payload["content"].startswith("@here [STILL CRITICAL]")
    assert rec.payload["allowed_mentions"] == {"parse": ["everyone"]}


def test_discord_omits_empty_fields():
    rec = Recorder(status=204)
    send(
        discord(rec),
        make_event(EventKind.INFO, Severity.INFO, channel=None, value=None, since=None, message=""),
    )
    embed = rec.payload["embeds"][0]
    assert "description" not in embed
    assert [f["name"] for f in embed["fields"]] == ["Device"]


def test_discord_invalid_mention_rejected():
    with pytest.raises(NotifierConfigError, match="mention"):
        build_notifier({"type": "discord", "webhook_url": WEBHOOK, "mention": "@physicists"})


@pytest.mark.parametrize("status", [400, 404, 429, 500])
def test_discord_http_error_raises_redacted(status):
    rec = Recorder(status=status, text=f'{{"message": "error for {WEBHOOK}", "retry_after": 1.5}}')
    with pytest.raises(DeliveryError) as exc_info:
        send(discord(rec), make_event())
    msg = str(exc_info.value)
    assert f"HTTP {status}" in msg and "'dc'" in msg
    assert WEBHOOK_SECRET not in msg


def test_discord_transport_error_redacted():
    rec = Recorder(exc=httpx.ConnectError(f"cannot connect to {WEBHOOK}"))
    with pytest.raises(DeliveryError) as exc_info:
        send(discord(rec), make_event())
    assert "ConnectError" in str(exc_info.value)
    assert WEBHOOK_SECRET not in str(exc_info.value)
    assert exc_info.value.__cause__ is None and exc_info.value.__suppress_context__


# --- Telegram ----------------------------------------------------------------

TG_OK = {"ok": True, "result": {"message_id": 1}}


def telegram(rec: Recorder, **cfg) -> TelegramNotifier:
    base = {"bot_token": BOT_TOKEN, "chat_id": -1001, "api_base": "https://tg.test"}
    return TelegramNotifier.from_config(
        "tg", Severity.INFO, {**base, **cfg}, transport=rec.transport
    )


def test_telegram_request_and_html_escaping():
    rec = Recorder(json_body=TG_OK)
    event = make_event(title="Flash: T<4K> & P1", message='a < b & "c" > d')
    send(telegram(rec), event)

    assert str(rec.requests[0].url) == f"https://tg.test/bot{BOT_TOKEN}/sendMessage"
    p = rec.payload
    assert p["parse_mode"] == "HTML"
    assert p["chat_id"] == "-1001"
    assert (
        p["text"]
        == "<b>[CRITICAL] Flash: T&lt;4K&gt; &amp; P1</b>\na &lt; b &amp; &quot;c&quot; &gt; d"
    )


@pytest.mark.parametrize(
    "kind, severity, chat, silent",
    [
        (EventKind.FIRING, Severity.CRITICAL, "999", False),
        (EventKind.REMINDER, Severity.CRITICAL, "999", False),
        (EventKind.FIRING, Severity.WARNING, "-1001", True),
        (EventKind.RESOLVED, Severity.CRITICAL, "-1001", True),
        (EventKind.INFO, Severity.CRITICAL, "-1001", True),
        (EventKind.TEST, Severity.WARNING, "-1001", True),
    ],
)
def test_telegram_routing_and_silence(kind, severity, chat, silent):
    rec = Recorder(json_body=TG_OK)
    send(telegram(rec, critical_chat_id=999), make_event(kind, severity))
    assert rec.payload["chat_id"] == chat
    assert rec.payload["disable_notification"] is silent


def test_telegram_silent_below_warning_rings_for_warnings():
    rec = Recorder(json_body=TG_OK)
    tg = telegram(rec, silent_below="warning")
    send(tg, make_event(EventKind.FIRING, Severity.WARNING))
    assert rec.payload["disable_notification"] is False
    assert tg.is_silent(make_event(EventKind.FIRING, Severity.INFO))
    assert tg.is_silent(make_event(EventKind.RESOLVED, Severity.WARNING))


def test_telegram_critical_without_critical_chat_goes_to_main_chat():
    rec = Recorder(json_body=TG_OK)
    send(telegram(rec), make_event())
    assert rec.payload["chat_id"] == "-1001"
    assert rec.payload["disable_notification"] is False


def test_telegram_ok_false_raises():
    rec = Recorder(
        json_body={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
    )
    with pytest.raises(DeliveryError, match="chat not found"):
        send(telegram(rec), make_event())


@pytest.mark.parametrize("status", [401, 403, 429, 502])
def test_telegram_http_error_redacts_token(status):
    rec = Recorder(status=status, text=f"nope: /bot{BOT_TOKEN}/sendMessage")
    with pytest.raises(DeliveryError) as exc_info:
        send(telegram(rec), make_event())
    assert f"HTTP {status}" in str(exc_info.value)
    assert BOT_TOKEN not in str(exc_info.value)


def test_telegram_invalid_json_raises():
    rec = Recorder(text="<html>gateway</html>")
    with pytest.raises(DeliveryError, match="invalid JSON"):
        send(telegram(rec), make_event())


@pytest.mark.parametrize("token", ["no-colon-here", "123:abc/def", "123: abc"])
def test_telegram_malformed_token_rejected_without_echo(token):
    with pytest.raises(NotifierConfigError) as exc_info:
        build_notifier({"type": "telegram", "bot_token": token, "chat_id": 1})
    assert token not in str(exc_info.value)


def test_telegram_invalid_silent_below():
    with pytest.raises(NotifierConfigError, match="silent_below"):
        build_notifier(
            {"type": "telegram", "bot_token": BOT_TOKEN, "chat_id": 1, "silent_below": "loud"}
        )


# --- ntfy --------------------------------------------------------------------


def ntfy(rec: Recorder, **cfg) -> NtfyNotifier:
    return NtfyNotifier.from_config(
        "phone", Severity.INFO, {"topic": NTFY_TOPIC, **cfg}, transport=rec.transport
    )


@pytest.mark.parametrize(
    "kind, severity, priority, tag",
    [
        (EventKind.FIRING, Severity.CRITICAL, 5, "rotating_light"),
        (EventKind.REMINDER, Severity.CRITICAL, 5, "rotating_light"),
        (EventKind.FIRING, Severity.WARNING, 4, "warning"),
        (EventKind.REMINDER, Severity.WARNING, 4, "warning"),
        (EventKind.RESOLVED, Severity.CRITICAL, 3, "white_check_mark"),
        (EventKind.INFO, Severity.INFO, 3, "information_source"),
        (EventKind.TEST, Severity.CRITICAL, 3, "test_tube"),
    ],
)
def test_ntfy_priority_and_tags(kind, severity, priority, tag):
    rec = Recorder(json_body={"id": "abc"})
    send(ntfy(rec), make_event(kind, severity))
    assert rec.payload["priority"] == priority
    assert rec.payload["tags"] == [tag]


def test_ntfy_json_publish_unicode_and_auth():
    rec = Recorder(json_body={"id": "abc"})
    event = make_event(title="Flash: T4K ≥ 6 K — Ω", message="Temperatur über Grenzwert")
    send(ntfy(rec, server="https://ntfy.example.org/", token="tk_secret"), event)

    req = rec.requests[0]
    assert str(req.url) == "https://ntfy.example.org/"
    assert req.headers["Authorization"] == "Bearer tk_secret"
    assert rec.payload == {
        "topic": NTFY_TOPIC,
        "title": "[CRITICAL] Flash: T4K ≥ 6 K — Ω",
        "message": "Temperatur über Grenzwert",
        "priority": 5,
        "tags": ["rotating_light"],
    }


def test_ntfy_default_server_no_auth():
    rec = Recorder(json_body={"id": "abc"})
    send(ntfy(rec), make_event(message=""))
    req = rec.requests[0]
    assert str(req.url) == "https://ntfy.sh/"
    assert "Authorization" not in req.headers
    assert rec.payload["message"] == rec.payload["title"]  # ntfy needs a non-empty body


def test_ntfy_error_raises_redacted():
    rec = Recorder(status=403, text=f'{{"code":40301,"error":"forbidden","topic":"{NTFY_TOPIC}"}}')
    with pytest.raises(DeliveryError) as exc_info:
        send(ntfy(rec, token="tk_secret"), make_event())
    assert "HTTP 403" in str(exc_info.value)
    assert NTFY_TOPIC not in str(exc_info.value)


@pytest.mark.parametrize("topic", ["a/b", "has space", "x" * 65])
def test_ntfy_invalid_topic(topic):
    with pytest.raises(NotifierConfigError, match="topic"):
        build_notifier({"type": "ntfy", "topic": topic})


# --- E-mail ------------------------------------------------------------------


class FakeSMTP:
    instances: ClassVar[list[FakeSMTP]] = []
    fail_login = False

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls: list[tuple] = []
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append(("quit",))

    def starttls(self, context=None):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))
        if self.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    def send_message(self, msg):
        self.calls.append(("send_message",))
        self.sent.append(msg)
        return {}


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail_login = False
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    return FakeSMTP


EMAIL_CFG = {
    "type": "email",
    "smtp_host": "smtp.example.org",
    "from": "labwatch <labwatch@example.org>",
    "to": "alice@example.org, bob@example.org",
}


def test_email_starttls_login_and_message(fake_smtp):
    n = build_notifier({**EMAIL_CFG, "username": "labwatch", "password": "pw-s3cret"})
    send(n, make_event())

    (smtp,) = fake_smtp.instances
    assert type(smtp) is FakeSMTP
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.example.org", 587, 20.0)
    assert smtp.calls == [
        ("starttls",),
        ("login", "labwatch", "pw-s3cret"),
        ("send_message",),
        ("quit",),
    ]
    (msg,) = smtp.sent
    assert msg["Subject"] == "[CRITICAL] Flash: T4K above 6 K"
    assert msg["From"] == "labwatch <labwatch@example.org>"
    assert msg["To"] == "alice@example.org, bob@example.org"
    assert msg["X-Priority"] == "1"
    assert msg["Date"] and msg["Message-ID"].endswith("@example.org>")
    body = msg.get_content()
    assert body.startswith("T4K = 6.2 K (limit 6 K)\nPulse tube probably stopped.\n")
    assert "Device:  Flash" in body and "Channel: T4K" in body and "Value:   6.2 K" in body
    assert "Since:" in body and "Time:" in body and "Event:   firing, severity critical" in body


def test_email_ssl_without_auth_and_list_recipients(fake_smtp):
    n = build_notifier({**EMAIL_CFG, "security": "ssl", "to": ["alice@example.org"]})
    send(n, make_event(EventKind.RESOLVED))

    (smtp,) = fake_smtp.instances
    assert type(smtp) is FakeSMTPSSL and smtp.port == 465 and smtp.context is not None
    assert smtp.calls == [("send_message",), ("quit",)]
    (msg,) = smtp.sent
    assert msg["To"] == "alice@example.org"
    assert msg["Subject"].startswith("[RESOLVED]")
    assert msg["X-Priority"] is None


def test_email_plain_custom_port(fake_smtp):
    n = build_notifier({**EMAIL_CFG, "security": "none", "smtp_port": "2525"})
    send(n, make_event(EventKind.FIRING, Severity.WARNING))
    (smtp,) = fake_smtp.instances
    assert smtp.port == 2525 and ("starttls",) not in smtp.calls
    assert smtp.sent[0]["X-Priority"] is None


def test_email_failure_raises_without_password(fake_smtp):
    fake_smtp.fail_login = True
    n = build_notifier({**EMAIL_CFG, "username": "labwatch", "password": "pw-s3cret"})
    with pytest.raises(DeliveryError) as exc_info:
        send(n, make_event())
    msg = str(exc_info.value)
    assert "SMTPAuthenticationError" in msg and "smtp.example.org:587" in msg
    assert "pw-s3cret" not in msg and "pw-s3cret" not in repr(n)


def test_email_subject_is_single_line(fake_smtp):
    n = build_notifier(EMAIL_CFG)
    send(n, make_event(title="Flash:\nT4K\r\nabove 6 K"))
    assert fake_smtp.instances[0].sent[0]["Subject"] == "[CRITICAL] Flash: T4K above 6 K"


@pytest.mark.parametrize(
    "cfg, match",
    [
        ({"security": "tls"}, "security"),
        ({"username": "u"}, "together"),
        ({"to": []}, "'to'"),
        ({"to": "not-an-address"}, "e-mail address"),
        ({"smtp_port": "abc"}, "smtp_port"),
    ],
)
def test_email_invalid_config(cfg, match):
    with pytest.raises(NotifierConfigError, match=match):
        build_notifier({**EMAIL_CFG, **cfg})


# --- Console -----------------------------------------------------------------


def test_console_logs_by_severity_and_prints(caplog, capsys):
    n = build_notifier({"type": "console", "stdout": True})
    with caplog.at_level(logging.INFO, logger="labwatch.notifiers.console"):
        send(n, make_event())
        send(n, make_event(EventKind.FIRING, Severity.WARNING))
        send(n, make_event(EventKind.RESOLVED))
    assert [r.levelno for r in caplog.records] == [logging.ERROR, logging.WARNING, logging.INFO]
    assert caplog.records[0].getMessage().startswith("[CRITICAL] Flash: T4K above 6 K: T4K = 6.2 K")
    out = capsys.readouterr().out
    assert "[CRITICAL] Flash: T4K above 6 K" in out and "    Pulse tube probably stopped." in out


def test_console_no_stdout_by_default(capsys):
    send(build_notifier({"type": "console"}), make_event())
    assert capsys.readouterr().out == ""


# --- build_notifier / config validation --------------------------------------


@pytest.mark.parametrize(
    "cfg, missing",
    [
        ({"type": "discord"}, "webhook_url"),
        ({"type": "discord", "webhook_url": "  "}, "webhook_url"),
        ({"type": "telegram", "chat_id": 1}, "bot_token"),
        ({"type": "telegram", "bot_token": BOT_TOKEN}, "chat_id"),
        ({"type": "ntfy"}, "topic"),
        ({"type": "email", "from": "a@example.org", "to": "b@example.org"}, "smtp_host"),
        ({"type": "email", "smtp_host": "h", "to": "b@example.org"}, "from"),
        ({"type": "email", "smtp_host": "h", "from": "a@example.org"}, "to"),
    ],
)
def test_missing_required_keys(cfg, missing):
    with pytest.raises(NotifierConfigError) as exc_info:
        build_notifier({**cfg, "name": "mine"})
    msg = str(exc_info.value)
    assert f"'{missing}'" in msg and cfg["type"] in msg and "'mine'" in msg
    assert BOT_TOKEN not in msg


def test_secrets_not_in_repr():
    notifiers = [
        build_notifier({"type": "discord", "webhook_url": WEBHOOK}),
        build_notifier({"type": "telegram", "bot_token": BOT_TOKEN, "chat_id": 1}),
        build_notifier({"type": "ntfy", "topic": NTFY_TOPIC, "token": "tk_secret"}),
    ]
    for n in notifiers:
        for secret in (WEBHOOK_SECRET, BOT_TOKEN, NTFY_TOPIC, "tk_secret"):
            assert secret not in repr(n) and secret not in str(n)


def test_webhook_url_must_be_http_and_is_not_echoed():
    with pytest.raises(NotifierConfigError) as exc_info:
        build_notifier(
            {"type": "discord", "webhook_url": f"discord.com/api/webhooks/1/{WEBHOOK_SECRET}"}
        )
    assert "http" in str(exc_info.value) and WEBHOOK_SECRET not in str(exc_info.value)


def test_unexpanded_env_var_rejected():
    with pytest.raises(NotifierConfigError, match="environment variable"):
        build_notifier({"type": "discord", "webhook_url": "${DISCORD_WEBHOOK_URL}"})


def test_unknown_type_and_bad_min_severity():
    with pytest.raises(NotifierConfigError, match="unknown notifier type"):
        build_notifier({"type": "pager"})
    with pytest.raises(NotifierConfigError, match="min_severity"):
        build_notifier({"type": "console", "min_severity": "urgent"})


def test_unknown_option_warns(caplog):
    with caplog.at_level(logging.WARNING):
        build_notifier(
            {"type": "telegram", "bot_token": BOT_TOKEN, "chat_id": 1, "critical_chatid": 2}
        )
    assert "critical_chatid" in caplog.text
    assert BOT_TOKEN not in caplog.text


# --- accepts() / client lifecycle --------------------------------------------


def test_accepts_filters_by_severity_but_always_passes_tests():
    n = build_notifier({"type": "console", "min_severity": "warning"})
    assert not n.accepts(make_event(EventKind.FIRING, Severity.INFO))
    assert n.accepts(make_event(EventKind.FIRING, Severity.WARNING))
    assert n.accepts(make_event(EventKind.FIRING, Severity.CRITICAL))
    assert n.accepts(make_event(EventKind.RESOLVED, Severity.WARNING))
    assert not n.accepts(make_event(EventKind.RESOLVED, Severity.INFO))
    assert n.accepts(make_event(EventKind.TEST, Severity.INFO))

    crit = build_notifier({"type": "ntfy", "topic": NTFY_TOPIC, "min_severity": "critical"})
    assert not crit.accepts(make_event(EventKind.FIRING, Severity.WARNING))
    assert crit.accepts(make_event(EventKind.REMINDER, Severity.CRITICAL))


def test_injected_client_is_not_closed_but_owned_client_is():
    rec = Recorder(json_body={"id": "abc"})

    async def run():
        async with httpx.AsyncClient(transport=rec.transport) as client:
            n = NtfyNotifier("phone", Severity.INFO, NTFY_TOPIC, client=client)
            await n.send(make_event())
            await n.aclose()
            assert not client.is_closed

        owned = NtfyNotifier("phone", Severity.INFO, NTFY_TOPIC, transport=rec.transport)
        await owned.send(make_event())
        inner = owned._client
        await owned.aclose()
        assert inner is not None and inner.is_closed and owned._client is None
        assert rec.requests[-1].headers["User-Agent"].startswith("labwatch/")

    asyncio.run(run())
