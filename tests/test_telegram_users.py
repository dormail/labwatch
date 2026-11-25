"""Several Telegram users: TelegramCommandBot (access requests, approval, personal and global mutes,
levels, admin commands) and the multi-recipient TelegramNotifier. All HTTP goes to a fake Bot API."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time as _time

import httpx
import pytest

from labwatch import state, subscriptions, telegram_bot
from labwatch.config import Config, Section
from labwatch.engine import Monitor, build_runtime
from labwatch.models import AlertEvent, EventKind, Severity
from labwatch.notifiers._common import DeliveryError
from labwatch.notifiers.telegram import Recipient, TelegramNotifier
from labwatch.state import StateStore
from labwatch.telegram_bot import TelegramAPIError, TelegramCommandBot

TOKEN = "123456789:AAH-telegram-s3cr3t-token"
HOST = "api.telegram.org"
ADMIN, CRIT, ANA, BEN, EVE = "100", "900", "200", "300", "999"
ACCESS = ("👋 This is a private lab monitoring bot. I've asked an admin to give you access; "
          "you'll get a message when that happens.")
WAITING = "⏳ Still waiting for an admin to approve you."
WELCOME = "✅ You now receive labwatch alerts."


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any request that isn't served by a MockTransport fails the test."""
    attempts: list[str] = []

    async def refuse(self, request):
        attempts.append(f"{request.method} {request.url.host}")  # host only: the URL holds the token
        raise httpx.ConnectError("network disabled in tests", request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    yield
    assert not attempts, f"a test tried to reach the network: {attempts}"


class FakeTelegram:
    """The Telegram Bot API on a MockTransport: records every call, answers like Telegram."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []
        self.violations: list[str] = []
        self.blocked: set[str] = set()  # chats that answer 403 (blocked the bot)
        self.updates: list[list[dict]] = []  # successive getUpdates results; then long-poll forever
        self.handlers: dict = {}  # method -> fn(payload) -> Response | None
        self.transport = httpx.MockTransport(self)

    def __call__(self, request: httpx.Request):
        prefix = f"/bot{TOKEN}/"
        if request.url.host != HOST or request.url.scheme != "https" or not request.url.path.startswith(prefix):
            self.violations.append(f"{request.method} {request.url.host}")
            return httpx.Response(599)
        method = request.url.path[len(prefix):]
        payload = json.loads(request.content or b"{}")
        self.requests.append((method, payload))
        if method in self.handlers:
            resp = self.handlers[method](payload)
            if resp is not None:
                return resp
        if method == "sendMessage" and str(payload.get("chat_id")) in self.blocked:
            return httpx.Response(403, json={"ok": False, "error_code": 403,
                                             "description": "Forbidden: bot was blocked by the user"})
        if method == "getUpdates":
            if self.updates:
                return httpx.Response(200, json={"ok": True, "result": self.updates.pop(0)})
            if payload.get("timeout"):
                return self._long_poll()
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.requests)}})

    async def _long_poll(self):
        await asyncio.Event().wait()  # until the test cancels

    def messages(self, chat=None) -> list[dict]:
        return [p for m, p in self.requests if m == "sendMessage" and (chat is None or str(p["chat_id"]) == str(chat))]

    def texts(self, chat) -> list[str]:
        return [p["text"] for p in self.messages(chat)]

    def chats(self) -> list[str]:
        return [str(p["chat_id"]) for p in self.messages()]

    def clear(self) -> None:
        self.requests.clear()


class Clock:
    """Replaces the ``time`` module in the bot/subscription/state modules."""

    def __init__(self, t: float) -> None:
        self.t = t

    def time(self) -> float:
        return self.t

    def __getattr__(self, name):
        return getattr(_time, name)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock(_time.time())
    for mod in (telegram_bot, subscriptions, state):
        monkeypatch.setattr(mod, "time", c)
    return c


@pytest.fixture
def api():
    a = FakeTelegram()
    yield a
    assert not a.violations, a.violations


def config(tmp_path) -> Config:
    return Config(path=tmp_path / "config.yaml", state_db=tmp_path / "s.sqlite", history_days=30, rule_defaults={},
                  devices=[], notifiers=[], heartbeat_url=None, heartbeat_interval=60.0, daily_summary=None,
                  notify_startup=False, secrets=set())


@pytest.fixture
def monitor(tmp_path) -> Monitor:
    """Two devices (never polled) so that /status, /flash and mute targets work."""
    cfg = config(tmp_path)
    rts = []
    for name in ("Flash", "Ket"):
        sec = Section({"name": name, "type": "ping", "host": f"{name.lower()}-pc.invalid", "method": "tcp",
                       "port": 22, "rules": [{"channel": "reachable", "not_equals": 1}]}, set())
        cfg.devices.append(sec)
        rts.append(build_runtime(cfg, sec))
    return Monitor(cfg, rts, [], StateStore(":memory:"))


def use_api(bot: TelegramCommandBot, api: FakeTelegram) -> None:
    """from_notifier() builds a real HTTP client; replace it by one on the fake API."""
    old = bot._client
    bot._client = httpx.AsyncClient(timeout=10, transport=api.transport)
    asyncio.run(old.aclose())


def make_bot(monitor, api, *, critical=None, allowed=(), min_severity=Severity.WARNING, lifecycle=True,
             silent_below=Severity.CRITICAL):
    n = TelegramNotifier("telegram", min_severity, TOKEN, ADMIN, critical_chat_id=critical,
                         allowed_chat_ids=list(allowed), silent_below=silent_below, transport=api.transport)
    n.lifecycle = lifecycle
    monitor.notifiers.append(n)
    bot = TelegramCommandBot.from_notifier(monitor, n)
    use_api(bot, api)
    return bot, n


_ids = iter(range(1, 10**6))


def update(chat, text, first="Ana", last=None, username=None, title=None) -> dict:
    uid = next(_ids)
    chat_obj = {"id": int(chat), "type": "group" if title else "private"}
    if title:
        chat_obj["title"] = title
    frm = {"id": int(chat), "first_name": first}
    if last:
        frm["last_name"] = last
    if username:
        frm["username"] = username
    return {"update_id": uid, "message": {"message_id": uid, "chat": chat_obj, "from": frm, "text": text}}


def say(bot, chat, text, first="Ana", **kw) -> None:
    asyncio.run(bot.handle_update(update(chat, text, first, **kw)))


def approve(bot, chat, first) -> None:
    say(bot, chat, "/start", first)
    say(bot, ADMIN, f"/approve {chat}", "Admin")


def ev(kind=EventKind.FIRING, severity=Severity.WARNING, device="Flash", category="pulse_tube",
       rule_id="Flash/pulse_tube is not on") -> AlertEvent:
    return AlertEvent(kind, rule_id, device, f"{device}: pulse_tube is not on", "pulse_tube = off", severity,
                      category=category)


def lifecycle_event(rule_id="labwatch/startup") -> AlertEvent:
    return AlertEvent(EventKind.INFO, rule_id, "labwatch", "labwatch started", "Monitoring: Flash", Severity.INFO,
                      category="labwatch")


def chats(bot, event) -> list[str]:
    return [str(r.chat_id) for r in bot.recipients_for(event)]


# ---------------------------------------------------------------------------
# from_notifier / bootstrap
# ---------------------------------------------------------------------------


def test_from_notifier_wires_recipients_and_creates_config_chats(monitor, api):
    bot, n = make_bot(monitor, api, critical=CRIT, allowed=[ANA, " "], min_severity=Severity.CRITICAL)
    assert n.recipients_fn == bot.recipients_for
    assert n.accepts(ev(severity=Severity.INFO))  # filtering is per person now
    assert bot.admins == {ADMIN, CRIT}
    assert bot.preapproved == {ANA}
    assert bot.api_base == "https://api.telegram.org"
    subs = {s.chat_id: (s.role, s.level) for s in monitor.store.subscribers(("admin", "user", "pending"))}
    assert subs == {ADMIN: ("admin", "critical"), CRIT: ("admin", "critical"), ANA: ("user", "critical")}


def test_bootstrap_keeps_existing_names_and_levels(monitor, api):
    st = monitor.store
    st.upsert_subscriber(ADMIN, "Matthias", "user", "info")  # e.g. demoted by mistake
    st.upsert_subscriber(ANA, "Ana", "pending", "warning")
    st.upsert_subscriber(BEN, "Ben", "user", "critical")  # approved earlier, not in the config
    make_bot(monitor, api, allowed=[ANA, ADMIN])
    adm = st.subscriber(ADMIN)
    assert (adm.name, adm.role, adm.level) == ("Matthias", "admin", "info")
    ana = st.subscriber(ANA)
    assert (ana.name, ana.role, ana.level) == ("Ana", "user", "warning")
    ben = st.subscriber(BEN)
    assert (ben.role, ben.level) == ("user", "critical")


def test_bootstrap_does_not_downgrade_an_admin_listed_in_allowed_chat_ids(monitor, api):
    make_bot(monitor, api, allowed=[ADMIN])
    assert monitor.store.subscriber(ADMIN).role == "admin"


def test_bootstrap_without_notifier_uses_warning_and_allowed_ids_as_admins(monitor):
    bot = TelegramCommandBot(monitor, TOKEN, {ADMIN, ""})
    try:
        assert bot.admins == {ADMIN} and bot.preapproved == set()
        s = monitor.store.subscriber(ADMIN)
        assert (s.role, s.level) == ("admin", "warning")
    finally:
        asyncio.run(bot._client.aclose())


# ---------------------------------------------------------------------------
# recipients_for
# ---------------------------------------------------------------------------


@pytest.fixture
def three(monitor, api):
    """Admin at warning, Ana at critical, Ben at info."""
    bot, n = make_bot(monitor, api)
    monitor.store.upsert_subscriber(ANA, "Ana", "user", "critical")
    monitor.store.upsert_subscriber(BEN, "Ben", "user", "info")
    return bot, n


@pytest.mark.parametrize(
    "event, expected",
    [
        (ev(severity=Severity.CRITICAL), {ADMIN, ANA, BEN}),
        (ev(EventKind.REMINDER, Severity.CRITICAL), {ADMIN, ANA, BEN}),
        (ev(severity=Severity.WARNING), {ADMIN, BEN}),
        (ev(severity=Severity.INFO), {BEN}),
        (ev(EventKind.RESOLVED, Severity.CRITICAL), {ADMIN, ANA, BEN}),
        (ev(EventKind.RESOLVED, Severity.WARNING), {ADMIN, BEN}),
        (lifecycle_event(), {ADMIN, ANA, BEN}),
        (lifecycle_event("labwatch/mute"), {ADMIN, ANA, BEN}),
    ],
)
def test_recipients_follow_each_persons_level(three, event, expected):
    bot, _ = three
    got = chats(bot, event)
    assert sorted(got) == sorted(expected)  # and each exactly once


def test_personal_mutes_only_affect_that_person(three, monitor):
    bot, _ = three
    monitor.store.personal_mute(BEN, "anomaly", None)
    monitor.store.personal_mute(ANA, "Ket", None)
    anomaly = ev(severity=Severity.CRITICAL, category="anomaly", rule_id="Flash/cw unusual")
    assert sorted(chats(bot, anomaly)) == [ADMIN, ANA]
    ket = ev(severity=Severity.CRITICAL, device="Ket", rule_id="Ket/pulse_tube is not on")
    assert sorted(chats(bot, ket)) == [ADMIN, BEN]
    assert sorted(chats(bot, ev(severity=Severity.CRITICAL))) == [ADMIN, ANA, BEN]
    # "muted for everyone" news is never hidden by a device or category mute
    assert sorted(chats(bot, lifecycle_event("labwatch/mute"))) == [ADMIN, ANA, BEN]


def test_lifecycle_false_hides_status_messages_from_everyone(monitor, api):
    bot, _ = make_bot(monitor, api, lifecycle=False)
    monitor.store.upsert_subscriber(ANA, "Ana", "user", "info")
    assert chats(bot, lifecycle_event()) == []
    assert sorted(chats(bot, ev())) == [ADMIN, ANA]


def test_test_events_only_reach_admins(monitor, api):
    bot, _ = make_bot(monitor, api, critical=CRIT)
    monitor.store.upsert_subscriber(ANA, "Ana", "user", "info")
    test = AlertEvent(EventKind.TEST, "labwatch/test", "labwatch", "test message", "", Severity.INFO)
    # the critical-only chat gets nothing but the admin's urgent alerts
    assert sorted(chats(bot, test)) == [ADMIN]


def test_critical_chat_routing_for_the_admin(monitor, api):
    bot, _ = make_bot(monitor, api, critical=CRIT)
    monitor.store.upsert_subscriber(ANA, "Ana", "user", "warning")
    crit = chats(bot, ev(severity=Severity.CRITICAL))
    assert CRIT in crit and ADMIN not in crit  # the admin's urgent alerts go to the critical chat
    assert ANA in crit  # other people keep their own chat
    assert ADMIN in chats(bot, ev(severity=Severity.WARNING))
    assert ADMIN in chats(bot, ev(EventKind.RESOLVED, Severity.CRITICAL))  # resolved is not urgent


def test_critical_chat_gets_each_critical_alert_once(monitor, api):
    bot, n = make_bot(monitor, api, critical=CRIT)
    assert chats(bot, ev(severity=Severity.CRITICAL)).count(CRIT) == 1


def test_discord_channels_and_pending_people_are_not_telegram_recipients(three, monitor):
    bot, _ = three
    monitor.store.upsert_subscriber("discord:111", "#flash", "user", "info")
    monitor.store.upsert_subscriber(EVE, "Eve", "pending", "info")
    got = chats(bot, ev(severity=Severity.CRITICAL)) + chats(bot, lifecycle_event())
    assert "discord:111" not in got and EVE not in got


@pytest.mark.parametrize(
    "event, silent_below, silent",
    [
        (ev(severity=Severity.CRITICAL), Severity.CRITICAL, False),
        (ev(severity=Severity.WARNING), Severity.CRITICAL, True),
        (ev(severity=Severity.WARNING), Severity.WARNING, False),
        (ev(EventKind.RESOLVED, Severity.CRITICAL), Severity.CRITICAL, True),
        (lifecycle_event(), Severity.INFO, True),
    ],
)
def test_recipients_silence(monitor, api, event, silent_below, silent):
    bot, _ = make_bot(monitor, api, silent_below=silent_below, min_severity=Severity.INFO)
    monitor.store.upsert_subscriber(ANA, "Ana", "user", "info")
    rs = bot.recipients_for(event)
    assert len(rs) == 2 and all(r.silent is silent for r in rs)


# ---------------------------------------------------------------------------
# TelegramNotifier with several recipients
# ---------------------------------------------------------------------------


def send(n: TelegramNotifier, event: AlertEvent) -> None:
    asyncio.run(n.send(event))


def test_notifier_without_bot_has_one_recipient(api):
    n = TelegramNotifier("tg", Severity.WARNING, TOKEN, ADMIN, critical_chat_id=CRIT, transport=api.transport)
    assert n.recipients(ev(severity=Severity.CRITICAL)) == [Recipient(CRIT, False)]
    assert n.recipients(ev()) == [Recipient(ADMIN, True)]
    assert not n.accepts(ev(severity=Severity.INFO))


def test_send_delivers_to_each_recipient(three, api):
    bot, n = three
    send(n, ev(severity=Severity.CRITICAL))
    msgs = api.messages()
    assert sorted(str(m["chat_id"]) for m in msgs) == [ADMIN, ANA, BEN]
    for m in msgs:
        assert m["text"].startswith("<b>[CRITICAL] Flash: pulse_tube is not on</b>")
        assert m["parse_mode"] == "HTML" and m["disable_notification"] is False


def test_one_blocked_recipient_does_not_stop_the_others(three, api, caplog):
    bot, n = three
    api.blocked = {ANA}
    with caplog.at_level(logging.WARNING):
        send(n, ev(severity=Severity.CRITICAL))  # no exception: the others got it
    assert sorted(api.chats()) == [ADMIN, ANA, ANA, BEN]  # the failed one is retried once
    assert f"chat {ANA}" in caplog.text and TOKEN not in caplog.text


def test_first_recipient_failing_still_delivers_to_later_ones(three, api):
    bot, n = three
    first = chats(bot, ev(severity=Severity.CRITICAL))[0]
    api.blocked = {first}
    send(n, ev(severity=Severity.CRITICAL))
    assert len(api.messages()) == 4  # everyone once, plus one retry for the failed chat


def test_raises_only_if_every_recipient_fails(three, api):
    bot, n = three
    api.blocked = {ADMIN, ANA, BEN}
    with pytest.raises(DeliveryError) as exc_info:
        send(n, ev(severity=Severity.CRITICAL))
    assert "blocked" in str(exc_info.value) and TOKEN not in str(exc_info.value)
    assert len(api.messages()) == 6  # tried everybody, each retried once


def test_nobody_wants_it_sends_nothing(three, api, monitor):
    bot, n = three
    for chat in (ADMIN, ANA, BEN):
        monitor.store.personal_mute(chat, "*", None)
    send(n, ev(severity=Severity.CRITICAL))
    assert api.requests == []


# ---------------------------------------------------------------------------
# strangers and access requests
# ---------------------------------------------------------------------------


def test_stranger_gets_access_request_and_admins_are_told(monitor, api):
    bot, _ = make_bot(monitor, api, critical=CRIT)
    say(bot, EVE, "/status", "Eve", last="Smith", username="eve")
    s = monitor.store.subscriber(EVE)
    assert (s.role, s.name) == ("pending", "Eve Smith (@eve)")
    assert api.texts(EVE) == [ACCESS]
    for admin in (ADMIN, CRIT):
        (notice,) = api.texts(admin)
        assert "Access request" in notice and "Eve Smith (@eve)" in notice
        assert f"/approve {EVE}" in notice and f"/remove {EVE}" in notice


def test_access_request_escapes_the_strangers_name(monitor, api):
    bot, _ = make_bot(monitor, api)
    say(bot, EVE, "hi", "<b>Eve</b> & co")
    (notice,) = api.texts(ADMIN)
    assert "&lt;b&gt;Eve&lt;/b&gt; &amp; co" in notice and "<b>Eve</b>" not in notice


def test_group_chat_stranger_is_named_by_title(monitor, api):
    bot, _ = make_bot(monitor, api)
    say(bot, "-1005", "/status", "Eve", title="Lab coffee chat")
    assert monitor.store.subscriber("-1005").name == "Lab coffee chat"
    assert api.texts("-1005") == [ACCESS]


def test_pending_user_hears_still_waiting_rate_limited(monitor, api, clock):
    bot, _ = make_bot(monitor, api)
    say(bot, EVE, "/start", "Eve")
    say(bot, EVE, "/status", "Eve")
    for cmd in ("/flash", "/alerts", "/help", "/mutes"):
        clock.t += 60
        say(bot, EVE, cmd, "Eve")
    assert api.texts(EVE) == [ACCESS, WAITING]
    clock.t += 601
    say(bot, EVE, "/status", "Eve")
    assert api.texts(EVE) == [ACCESS, WAITING, WAITING]
    assert len(api.texts(ADMIN)) == 1  # admins are asked once


def test_pending_user_cannot_change_anything(monitor, api):
    bot, _ = make_bot(monitor, api)
    monitor.store.upsert_subscriber(ANA, "Ana", "user", "warning")
    say(bot, EVE, "/start", "Eve")
    for cmd in ("/mute all forever", "/muteall Flash 2d", "/level info", f"/approve {EVE}", f"/remove {ANA}",
                "/unmuteall Flash"):
        say(bot, EVE, cmd, "Eve")
    assert monitor.store.personal_mutes(EVE) == [] and monitor.store.mutes() == []
    s = monitor.store.subscriber(EVE)
    assert (s.role, s.level) == ("pending", "warning")
    assert monitor.store.subscriber(ANA) is not None
    assert set(api.texts(EVE)) <= {ACCESS, WAITING}


def test_approval_sends_a_welcome_and_unlocks_commands(monitor, api):
    bot, _ = make_bot(monitor, api)
    say(bot, ANA, "/start", "Ana")
    api.clear()
    say(bot, ADMIN, f"/approve {ANA}", "Admin")
    assert [str(p["chat_id"]) for p in api.messages()] == [ADMIN, ANA]
    assert api.texts(ADMIN) == ["✅ Ana can now use labwatch."]
    assert api.texts(ANA)[0].startswith(WELCOME)
    assert monitor.store.subscriber(ANA).role == "user"
    say(bot, ANA, "/status", "Ana")
    assert api.texts(ANA)[-1].startswith("<b>labwatch status</b>")


def test_welcome_to_a_blocked_chat_is_logged_not_raised(monitor, api, caplog):
    bot, _ = make_bot(monitor, api)
    say(bot, ANA, "/start", "Ana")
    api.blocked = {ANA}
    with caplog.at_level(logging.WARNING):
        say(bot, ADMIN, f"/approve {ANA}", "Admin")
    assert monitor.store.subscriber(ANA).role == "user"
    assert "could not notify" in caplog.text and TOKEN not in caplog.text


def test_removed_user_is_a_stranger_again(monitor, api):
    bot, _ = make_bot(monitor, api)
    approve(bot, ANA, "Ana")
    say(bot, ADMIN, f"/remove {ANA}", "Admin")
    api.clear()
    say(bot, ANA, "/status", "Ana")
    assert api.texts(ANA) == [ACCESS]
    assert monitor.store.subscriber(ANA).role == "pending"
    assert ANA not in chats(bot, ev(severity=Severity.CRITICAL))


def test_name_changes_are_remembered(monitor, api):
    bot, _ = make_bot(monitor, api)
    approve(bot, ANA, "Ana")
    say(bot, ANA, "/help", "Anabel")
    assert monitor.store.subscriber(ANA).name == "Anabel"


def test_plain_text_from_a_user_gets_help(monitor, api):
    bot, _ = make_bot(monitor, api)
    approve(bot, ANA, "Ana")
    say(bot, ANA, "hello?", "Ana")
    assert api.texts(ANA)[-1].startswith("<b>labwatch commands</b>")


@pytest.mark.parametrize("upd", [
    {"update_id": 1},
    {"update_id": 2, "message": {"message_id": 2, "chat": {"id": 999}, "photo": [{}]}},
    {"update_id": 3, "message": {"message_id": 3, "text": "/status"}},
    {"update_id": 4, "edited_message": {"message_id": 4, "chat": {"id": 999}, "text": "/status"}},
])
def test_updates_without_chat_or_text_are_ignored(monitor, api, upd):
    bot, _ = make_bot(monitor, api)
    asyncio.run(bot.handle_update(upd))
    assert api.requests == []
    assert monitor.store.subscriber(EVE) is None


# ---------------------------------------------------------------------------
# respond(): personal mutes
# ---------------------------------------------------------------------------


@pytest.fixture
def bot(monitor, api, clock):
    b, _ = make_bot(monitor, api)
    monitor.store.upsert_subscriber(ANA, "Ana", "user", "warning")
    monitor.store.upsert_subscriber(BEN, "Ben", "user", "warning")
    return b


def mutes_of(bot, chat):
    return {m.target: m.until for m in bot.store.personal_mutes(chat)}


def test_mute_without_args_shows_usage(bot):
    out = bot.respond("/mute", ANA, "Ana")
    assert out.startswith("<b>/mute</b> what [for how long]")
    assert "Devices: Flash, Ket" in out and "anomaly" in out
    assert mutes_of(bot, ANA) == {}


@pytest.mark.parametrize(
    "cmd, target, seconds",
    [
        ("/mute Flash", "flash", 2 * 3600),
        ("/mute flash 4h", "flash", 4 * 3600),
        ("/mute Ket circulation 1d", "ket/circulation", 86400),
        ("/mute ket/anomaly 30m", "ket/anomaly", 1800),
        ("/mute anomaly forever", "anomaly", None),
        ("/mute all 8h", "*", 8 * 3600),
        ("/mute labwatch", "labwatch", 2 * 3600),
    ],
)
def test_personal_mute(bot, clock, cmd, target, seconds):
    out = bot.respond(cmd, ANA, "Ana")
    assert out.startswith("🔕 Muted ") and "for you" in out and "Others still get these alerts" in out
    assert mutes_of(bot, ANA) == {target: None if seconds is None else clock.t + seconds}
    assert mutes_of(bot, BEN) == {}
    assert bot.store.mutes() == []  # nothing muted for everyone


def test_mute_unknown_target(bot):
    out = bot.respond("/mute fridge 4h", ANA, "Ana")
    assert out.startswith("unknown target &#x27;fridge&#x27;.") and "Devices: Flash, Ket" in out
    assert mutes_of(bot, ANA) == {}


def test_unmute(bot):
    for cmd in ("/mute Flash", "/mute anomaly", "/mute all"):
        bot.respond(cmd, ANA, "Ana")
    bot.respond("/mute Flash", BEN, "Ben")
    assert bot.respond("/unmute FLASH", ANA, "Ana") == "🔔 Unmuted Flash for you."
    assert set(mutes_of(bot, ANA)) == {"anomaly", "*"}
    assert bot.respond("/unmute Flash", ANA, "Ana").startswith("You had no mute for Flash.")
    assert bot.respond("/unmute all", ANA, "Ana") == "🔔 Unmuted all for you."
    assert mutes_of(bot, ANA) == {}
    assert set(mutes_of(bot, BEN)) == {"flash"}
    assert bot.respond("/unmute", ANA, "Ana").startswith("Usage: /unmute")
    assert bot.respond("/unmute bogus", ANA, "Ana").startswith("unknown target &#x27;bogus&#x27;.")


def test_mutes_lists_mine_and_everyones_but_not_other_peoples(bot):
    assert bot.respond("/mutes", ANA) == "Nothing is muted."
    bot.respond("/mute anomaly forever", BEN, "Ben")
    assert bot.respond("/mutes", ANA) == "Nothing is muted."
    bot.respond("/mute Flash 4h", ANA, "Ana")
    bot.respond("/muteall Ket 2d warm-up", BEN, "Ben")
    out = bot.respond("/mutes", ANA)
    assert out.startswith("<b>Muted for you</b>\n  🔕 flash until ")
    assert "<b>Muted for everyone</b>\n  🔕 Ket until " in out and "(warm-up; by Ben)" in out
    assert "anomaly" not in out


# ---------------------------------------------------------------------------
# respond(): mutes for everyone
# ---------------------------------------------------------------------------


def test_muteall_with_reason_and_name(bot, clock):
    out = bot.respond("/muteall Ket 2d warm-up for service", ANA, "Ana")
    assert out.startswith("🔕 Muted Ket for everyone until ") and "Everybody will be told" in out
    (m,) = bot.store.mutes(clock.t)
    assert (m.target, m.until, m.reason) == ("Ket", clock.t + 2 * 86400, "warm-up for service; by Ana")
    assert mutes_of(bot, ANA) == {}  # not a personal mute


def test_muteall_name_falls_back_to_the_stored_name(bot):
    bot.respond("/muteall anomaly 4h", ANA)
    assert bot.store.mutes()[0].reason == "by Ana"


@pytest.mark.parametrize("cmd", ["/muteall Ket forever", "/muteall Ket off", "/muteall Ket 15d",
                                 "/muteall all 14.5d", "/muteall Ket 400h"])
def test_muteall_limits(bot, cmd):
    out = bot.respond(cmd, ANA, "Ana")
    assert out.startswith("Mutes for everyone need a duration of at most 14 days")
    assert bot.store.mutes() == []


def test_muteall_at_most_14_days(bot, clock):
    bot.respond("/muteall all 14d", ANA, "Ana")
    (m,) = bot.store.mutes(clock.t)
    assert (m.target, m.until) == ("*", clock.t + 14 * 86400)


def test_muteall_needs_an_explicit_duration(bot, clock):
    # A mute for everyone is never implicit (same rule as Discord's /muteall).
    assert "need a duration" in bot.respond("/muteall Ket warm-up", ANA, "Ana")
    assert bot.store.mutes(clock.t) == []


def test_muteall_usage_and_unknown_target(bot):
    assert bot.respond("/muteall", ANA).startswith("<b>/muteall</b> what [for how long] [reason]")
    assert bot.respond("/muteall fridge 2d", ANA).startswith("unknown target &#x27;fridge&#x27;.")
    assert bot.store.mutes() == []


def test_unmuteall(bot):
    bot.respond("/muteall Ket 2d", ANA, "Ana")
    assert bot.respond("/unmuteall ket", BEN, "Ben").startswith("🔔 Removed the mute for everyone")
    assert bot.store.mutes() == []
    assert bot.respond("/unmuteall Ket", BEN, "Ben") == "Ket wasn't muted for everyone. /mutes"
    assert bot.respond("/unmuteall", BEN) == "Usage: /unmuteall Flash"
    assert bot.respond("/unmuteall fridge", BEN).startswith("unknown target &#x27;fridge&#x27;.")


# ---------------------------------------------------------------------------
# respond(): /level, admin commands, help
# ---------------------------------------------------------------------------


def test_level(bot):
    assert bot.respond("/level", ANA).startswith("You get <b>warning</b> and above.")
    out = bot.respond("/level Critical", ANA)
    assert out.startswith("OK: you now get only critical alerts")
    assert bot.store.subscriber(ANA).level == "critical"
    assert bot.respond("/level info", ANA).startswith("OK: you now get everything")
    assert bot.respond("/level loud", ANA) == "Use /level critical, /level warning or /level info."
    assert bot.store.subscriber(ANA).level == "info"
    assert bot.store.subscriber(BEN).level == "warning"
    assert bot.respond("/level critical") == "Not available."


def test_level_keeps_the_admin_an_admin(bot):
    bot.respond("/level critical", ADMIN)
    s = bot.store.subscriber(ADMIN)
    assert (s.role, s.level) == ("admin", "critical")


@pytest.mark.parametrize("cmd", ["/users", f"/approve {EVE}", f"/remove {BEN}", "/remove"])
def test_admin_commands_need_an_admin(bot, cmd):
    bot.store.upsert_subscriber(EVE, "Eve", "pending", "warning")
    assert bot.respond(cmd, ANA, "Ana") == "Only admins can do that."
    assert bot.store.subscriber(EVE).role == "pending"
    assert bot.store.subscriber(BEN) is not None


def test_users_lists_everybody(bot):
    bot.store.upsert_subscriber(EVE, "Eve <x>", "pending", "warning")
    out = bot.respond("/users", ADMIN)
    lines = out.splitlines()
    assert lines[0] == f"👑 admin – <code>{ADMIN}</code> – admin, warning"
    assert f"👤 Ana – <code>{ANA}</code> – user, warning" in lines
    assert f"⏳ Eve &lt;x&gt; – <code>{EVE}</code> – pending, warning" in lines


def test_approve_and_remove_usage(bot):
    assert bot.respond("/approve", ADMIN) == "Usage: /approve &lt;chat id&gt; (see /users)"
    assert bot.respond("/approve 12345", ADMIN) == "No user with chat id 12345. See /users"
    assert bot.respond("/remove <b>", ADMIN) == "No user with chat id &lt;b&gt;. See /users"


def test_remove_user_drops_their_mutes(bot):
    bot.respond("/mute Flash forever", BEN, "Ben")
    assert bot.respond(f"/remove {BEN}", ADMIN) == "Removed Ben."
    assert bot.store.subscriber(BEN) is None and bot.store.personal_mutes(BEN) == []
    assert BEN not in chats(bot, ev(severity=Severity.CRITICAL))


def test_remove_pending_request(bot):
    bot.store.upsert_subscriber(EVE, "Eve", "pending", "warning")
    assert bot.respond(f"/remove {EVE}", ADMIN) == "Removed Eve."
    assert bot.store.subscriber(EVE) is None


def test_config_admins_cannot_be_removed(monitor, api):
    bot, _ = make_bot(monitor, api, critical=CRIT)
    for admin in (ADMIN, CRIT):
        assert bot.respond(f"/remove {admin}", ADMIN) == "Admins from the config file can't be removed here."
        assert bot.store.subscriber(admin).role == "admin"


def test_approve_does_not_demote_an_admin(bot):
    bot._to_notify = []
    bot.respond(f"/approve {ADMIN}", ADMIN)
    assert bot.store.subscriber(ADMIN).role == "admin"


def test_help_shows_admin_commands_to_admins_only(bot):
    admin_help = bot.respond("/help", ADMIN)
    user_help = bot.respond("/help", ANA)
    assert "/approve" in admin_help and "/users" in admin_help
    assert "/approve" not in user_help and "/users" not in user_help
    assert "/muteall" in user_help and "/level" in user_help
    assert bot.respond("/start", ANA) == user_help


def test_unknown_command_and_device_commands(bot):
    out = bot.respond("/frobnicate <x>", ANA)
    assert out.startswith("Unknown command /frobnicate.") and "<b>labwatch commands</b>" in out
    assert bot.respond("/ket", ANA).startswith("<b>Ket</b> · data ")
    assert bot.respond("/mute@labwatch_bot Flash", ANA).startswith("🔕 Muted Flash for you")


# ---------------------------------------------------------------------------
# Telegram API: command menus and _call
# ---------------------------------------------------------------------------


def test_register_commands_default_and_admin_scope(monitor, api, caplog):
    bot, _ = make_bot(monitor, api, critical=CRIT)

    def fail_for_crit(payload):
        if payload.get("scope", {}).get("chat_id") == CRIT:
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})
        return None

    api.handlers["setMyCommands"] = fail_for_crit
    with caplog.at_level(logging.INFO):
        asyncio.run(bot.register_commands())
    calls = [p for m, p in api.requests if m == "setMyCommands"]
    assert len(calls) == 3
    default = calls[0]
    assert "scope" not in default
    names = [c["command"] for c in default["commands"]]
    assert names[:3] == ["status", "flash", "ket"] and "users" not in names and "approve" not in names
    scoped = {p["scope"]["chat_id"]: p for p in calls[1:]}
    assert set(scoped) == {ADMIN, CRIT}
    for p in scoped.values():
        assert p["scope"]["type"] == "chat"
        assert {"users", "approve", "remove"} <= {c["command"] for c in p["commands"]}
    assert "chat not found" in caplog.text and TOKEN not in caplog.text


@pytest.mark.parametrize("body", [[1, 2], "ok", 42, True])
def test_call_rejects_non_dict_json(monitor, api, body):
    bot, _ = make_bot(monitor, api)
    api.handlers["getMe"] = lambda p: httpx.Response(200, json=body)
    with pytest.raises(TelegramAPIError, match="getMe: HTTP 200, unexpected response"):
        asyncio.run(bot._call("getMe"))


def test_call_error_redacts_token(monitor, api):
    bot, _ = make_bot(monitor, api)
    api.handlers["getMe"] = lambda p: httpx.Response(401, json={"ok": False,
                                                              "description": f"Unauthorized bot{TOKEN}"})
    with pytest.raises(TelegramAPIError) as exc_info:
        asyncio.run(bot._call("getMe"))
    assert "HTTP 401" in str(exc_info.value) and TOKEN not in str(exc_info.value)


def test_call_transport_error_and_invalid_json_hide_the_url(monitor, api):
    bot, _ = make_bot(monitor, api)

    def boom(payload):
        raise httpx.ConnectError(f"cannot connect to https://{HOST}/bot{TOKEN}/getMe")

    api.handlers["getMe"] = boom
    with pytest.raises(TelegramAPIError) as exc_info:
        asyncio.run(bot._call("getMe"))
    assert str(exc_info.value) == "getMe: ConnectError" and exc_info.value.__cause__ is None
    api.handlers["getMe"] = lambda p: httpx.Response(502, text="<html>bad gateway</html>")
    with pytest.raises(TelegramAPIError, match="getMe: HTTP 502, invalid JSON"):
        asyncio.run(bot._call("getMe"))


def test_call_returns_result(monitor, api):
    bot, _ = make_bot(monitor, api)
    api.handlers["getMe"] = lambda p: httpx.Response(200, json={"ok": True, "result": {"id": 1}})
    assert asyncio.run(bot._call("getMe")) == {"id": 1}


# ---------------------------------------------------------------------------
# the polling loop
# ---------------------------------------------------------------------------


_real_sleep = asyncio.sleep  # some tests patch asyncio.sleep


async def wait_for(pred, n: int = 400) -> None:
    for _ in range(n):
        if pred():
            return
        await _real_sleep(0.005)
    raise AssertionError("condition not reached")


def test_run_skips_the_backlog_and_answers_new_messages(monitor, api):
    bot, _ = make_bot(monitor, api)
    old = update(ADMIN, "/status")
    new = update(ADMIN, "/alerts")
    api.updates = [[old], [new]]

    async def go():
        task = asyncio.create_task(bot.run())
        await wait_for(lambda: len([m for m, _ in api.requests if m == "getUpdates"]) >= 3)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    methods = [m for m, _ in api.requests]
    assert methods[:2] == ["setMyCommands", "setMyCommands"]
    polls = [p for m, p in api.requests if m == "getUpdates"]
    assert polls[0] == {"offset": -1, "timeout": 0}
    assert polls[1] == {"offset": old["update_id"] + 1, "timeout": bot.poll_timeout, "allowed_updates": ["message"]}
    assert polls[2]["offset"] == new["update_id"] + 1
    assert api.texts(ADMIN) == ["🟢 No active alerts."]  # /alerts answered, the stale /status not
    assert bot._client.is_closed


def test_run_backs_off_on_api_errors(monitor, api, monkeypatch):
    bot, _ = make_bot(monitor, api)
    real_sleep = asyncio.sleep
    delays: list[float] = []

    async def fast_sleep(d, *a, **k):
        delays.append(d)
        await real_sleep(0)

    errors = {"n": 0}

    def conflict(payload):
        if payload.get("timeout") and errors["n"] < 3:
            errors["n"] += 1
            return httpx.Response(409, json={"ok": False, "description": "Conflict: terminated by other getUpdates"})
        return None

    api.handlers["getUpdates"] = conflict

    async def go():
        monkeypatch.setattr(asyncio, "sleep", fast_sleep)
        task = asyncio.create_task(bot.run())
        await wait_for(lambda: len([m for m, _ in api.requests if m == "getUpdates"]) >= 5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert delays[:3] == [5.0, 10.0, 20.0]


def test_bot_works_again_after_the_supervisor_restarts_it(monitor, api, monkeypatch):
    bot, _ = make_bot(monitor, api)
    real_skip = bot._skip_backlog
    calls = {"n": 0}

    async def flaky_skip():
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")  # anything but TelegramAPIError
        return await real_skip()

    monkeypatch.setattr(bot, "_skip_backlog", flaky_skip)
    pending = [update(ADMIN, "/alerts")]

    def long_poll(payload):  # the first long poll after the restart gets a message
        if payload.get("timeout") and pending:
            return httpx.Response(200, json={"ok": True, "result": [pending.pop()]})
        return None

    api.handlers["getUpdates"] = long_poll
    real_sleep = asyncio.sleep

    async def fast_sleep(d, *a, **k):
        await real_sleep(0)

    async def go():
        monkeypatch.setattr(asyncio, "sleep", fast_sleep)
        monitor._supervise("commands:TelegramCommandBot", bot.run)
        try:
            await wait_for(lambda: api.messages(ADMIN), n=200)
        finally:
            for t in monitor._tasks:
                t.cancel()
            await asyncio.gather(*monitor._tasks, return_exceptions=True)

    asyncio.run(go())
