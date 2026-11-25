"""Discord: the discord_bot notifier (one channel per machine) and DiscordCommandBot (slash commands over
the gateway). HTTP goes to a fake Discord API on a MockTransport, the gateway is a fake websocket."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from types import SimpleNamespace

import httpx
import pytest

from labwatch import discord_bot, report
from labwatch.config import Config, Section
from labwatch.discord_bot import (
    EPHEMERAL,
    FATAL_CLOSE_CODES,
    DiscordAPIError,
    DiscordCommandBot,
    FatalGatewayError,
    html_to_markdown,
    sub_id,
)
from labwatch.engine import Monitor, build_runtime
from labwatch.models import AlertEvent, EventKind, Severity
from labwatch.notifiers import NotifierConfigError, build_notifier
from labwatch.notifiers._common import DeliveryError
from labwatch.notifiers.discord_bot import DiscordBotNotifier
from labwatch.state import StateStore

TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4OQ.Gh1jK2.discord-s3cr3t-bot-token"
HOST = "discord.com"
GUILD, OTHER_GUILD, APP = "5550001", "5550002", "7770001"
FLASH, KET, STATUS, ELSEWHERE = "111", "222", "333", "444"
HELLO = {"op": 10, "d": {"heartbeat_interval": 41250}}
_real_sleep = asyncio.sleep


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts: list[str] = []

    async def refuse(self, request):
        attempts.append(f"{request.method} {request.url.host}")
        raise httpx.ConnectError("network disabled in tests", request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    yield
    assert not attempts, f"a test tried to reach the network: {attempts}"


class FakeDiscord:
    """Discord's REST API (v10) on a MockTransport."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object, httpx.Headers]] = []
        self.violations: list[str] = []
        self.hidden: set[str] = set()  # channels the bot can't see
        self.failing: set[str] = set()  # channels where posting fails
        self.guild_of: dict[str, str] = {}
        self.names = {FLASH: "flash-fridge", KET: "ket-fridge", STATUS: "labwatch"}
        self.handlers: dict = {}
        self.transport = httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != HOST or request.url.scheme != "https" or not request.url.path.startswith("/api/v10/"):
            self.violations.append(f"{request.method} {request.url.host}{request.url.path}")
            return httpx.Response(599)
        path = request.url.path[len("/api/v10"):]
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body, request.headers))
        key = (request.method, path)
        if key in self.handlers:
            resp = self.handlers[key](request)
            if resp is not None:
                return resp
        if request.method == "POST" and (m := re.fullmatch(r"/channels/(\d+)/messages", path)):
            if m.group(1) in self.failing:
                return httpx.Response(403, json={"message": "Missing Permissions", "code": 50013})
            return httpx.Response(200, json={"id": "9", "channel_id": m.group(1)})
        if request.method == "GET" and (m := re.fullmatch(r"/channels/(\d+)", path)):
            cid = m.group(1)
            if cid in self.hidden:
                return httpx.Response(403, json={"message": "Missing Access", "code": 50001})
            return httpx.Response(200, json={"id": cid, "type": 0, "guild_id": self.guild_of.get(cid, GUILD),
                                             "name": self.names.get(cid, f"chan-{cid}")})
        if key == ("GET", "/oauth2/applications/@me"):
            return httpx.Response(200, json={"id": APP, "name": "labwatch"})
        if request.method == "PUT" and re.fullmatch(rf"/applications/{APP}/guilds/\d+/commands", path):
            return httpx.Response(200, json=body)
        if request.method == "POST" and re.fullmatch(r"/interactions/\d+/[\w-]+/callback", path):
            return httpx.Response(204)
        if key == ("GET", "/gateway/bot"):
            return httpx.Response(200, json={"url": "wss://gateway.discord.gg", "shards": 1})
        self.violations.append(f"unexpected {request.method} {path}")
        return httpx.Response(404, json={"message": "404: Not Found", "code": 0})

    def posts(self, cid: str | None = None) -> list[dict]:
        return [b for m, p, b, _ in self.requests
                if m == "POST" and re.fullmatch(r"/channels/\d+/messages", p)
                and (cid is None or p == f"/channels/{cid}/messages")]

    def posted_channels(self) -> list[str]:
        return [p.split("/")[2] for m, p, _, _ in self.requests if m == "POST" and p.endswith("/messages")]

    def callbacks(self) -> list[tuple[str, dict]]:
        return [(p, b) for m, p, b, _ in self.requests if p.startswith("/interactions/")]

    def calls(self, method: str, path: str) -> int:
        return sum(1 for m, p, _, _ in self.requests if (m, p) == (method, path))


class FakeWS:
    def __init__(self, msgs, close_code=None, on_message=None):
        self.msgs = [m if isinstance(m, str) else json.dumps(m) for m in msgs]
        self.sent: list[dict] = []
        self.close_code = close_code
        self.closed: list[tuple] = []
        self.on_message = on_message

    async def recv(self):
        return self.msgs.pop(0)

    async def send(self, m):
        self.sent.append(json.loads(m))

    async def close(self, *a):
        self.closed.append(a)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.msgs:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        m = self.msgs.pop(0)
        if self.on_message:
            self.on_message(json.loads(m))
        return m


class _Conn:
    def __init__(self, item) -> None:
        self.item = item

    async def __aenter__(self):
        if isinstance(self.item, BaseException):
            raise self.item
        return self.item

    async def __aexit__(self, *exc):
        return False


class FakeConnect:
    """Stands in for websockets.connect: each call takes the next websocket (or exception) from the script."""

    def __init__(self, script) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        return _Conn(self.script.pop(0) if self.script else FatalGatewayError("test script exhausted"))


@pytest.fixture
def api():
    a = FakeDiscord()
    yield a
    assert not a.violations, a.violations


def config(tmp_path) -> Config:
    return Config(path=tmp_path / "config.yaml", state_db=tmp_path / "s.sqlite", history_days=30, rule_defaults={},
                  devices=[], notifiers=[], heartbeat_url=None, heartbeat_interval=60.0, daily_summary=None,
                  notify_startup=False, secrets=set())


@pytest.fixture
def monitor(tmp_path) -> Monitor:
    cfg = config(tmp_path)
    rts = []
    for name in ("Flash", "Ket", "Plassys"):  # Plassys has no channel of its own
        sec = Section({"name": name, "type": "ping", "host": f"{name.lower()}-pc.invalid", "method": "tcp",
                       "port": 22, "rules": [{"channel": "reachable", "not_equals": 1}]}, set())
        cfg.devices.append(sec)
        rts.append(build_runtime(cfg, sec))
    return Monitor(cfg, rts, [], StateStore(":memory:"))


def notifier(api, channels=None, status=None, mention="@here", min_severity=Severity.WARNING) -> DiscordBotNotifier:
    return DiscordBotNotifier("discord", min_severity, TOKEN, channels or {"Flash": FLASH, "Ket": KET},
                              status_channel=status, mention=mention, transport=api.transport)


def make_bot(monitor, api, connect=None, **kw):
    n = notifier(api, **kw)
    monitor.notifiers.append(n)
    b = DiscordCommandBot(monitor, n, transport=api.transport, connect=connect)
    b.guilds = {GUILD}  # as after register_commands(): only our server's interactions count
    return b, n


def ev(kind=EventKind.FIRING, severity=Severity.CRITICAL, device="Flash", category="pulse_tube", rule_id=None):
    return AlertEvent(kind, rule_id or f"{device}/pulse_tube is not on", device, f"{device}: pulse_tube is not on",
                      "pulse_tube = off", severity, category=category)


def lifecycle_event(rule_id="labwatch/mute"):
    return AlertEvent(EventKind.INFO, rule_id, "labwatch", "🔕 Ket muted for everyone", "", Severity.INFO,
                      category="labwatch")


# ---------------------------------------------------------------------------
# notifier config
# ---------------------------------------------------------------------------


def build(**cfg) -> DiscordBotNotifier:
    return build_notifier({"type": "discord_bot", "name": "dc", "bot_token": TOKEN,
                           "channels": {"Flash": 1234567890, "Ket": " 222 "}, **cfg})


def test_config_valid():
    n = build(status_channel=333, mention="<@&987654321>", min_severity="critical")
    assert isinstance(n, DiscordBotNotifier)
    assert n.channels == {"Flash": "1234567890", "Ket": "222"}
    assert n.status_channel == "333" and n.mention == "<@&987654321>" and n.commands is True
    assert n.api_base == "https://discord.com/api/v10" and n.min_severity is Severity.CRITICAL


@pytest.mark.parametrize("mention", ["@here", "@everyone", "<@&123>", "<@123>", "<@!123>"])
def test_config_mentions_accepted(mention):
    assert build(mention=mention).mention == mention


@pytest.mark.parametrize("mention", ["here", "@someone", "<#123>", "<@&abc>", "everyone"])
def test_config_bad_mention(mention):
    with pytest.raises(NotifierConfigError, match="'mention' must be"):
        build(mention=mention)


def test_config_empty_status_channel_means_none():
    assert build(status_channel="").status_channel is None  # ${DISCORD_STATUS_CHANNEL:-}


@pytest.mark.parametrize("channels", [None, [], {}, ["111"], "111"])
def test_config_channels_must_be_a_mapping(channels):
    with pytest.raises(NotifierConfigError, match="'channels' must map device names"):
        build_notifier({"type": "discord_bot", "bot_token": TOKEN, "channels": channels})


@pytest.mark.parametrize("cid", ["general", "#flash", "", None, "-111", "11 1", "0x1f"])
def test_config_channel_ids_must_be_numeric(cid):
    with pytest.raises(NotifierConfigError, match="channel id for 'Flash' must be a number"):
        build_notifier({"type": "discord_bot", "bot_token": TOKEN, "channels": {"Flash": cid}})


def test_config_status_channel_must_be_numeric():
    with pytest.raises(NotifierConfigError, match="'status_channel' must be a channel id"):
        build(status_channel="#labwatch")


def test_config_errors_never_echo_the_token():
    for cfg in ({"channels": {}}, {"channels": {"Flash": "x"}}, {"mention": "x"}, {"status_channel": "x"}):
        with pytest.raises(NotifierConfigError) as exc_info:
            build_notifier({"type": "discord_bot", "bot_token": TOKEN, "channels": {"Flash": 1}, **cfg})
        assert TOKEN not in str(exc_info.value)
    with pytest.raises(NotifierConfigError, match="missing required option 'bot_token'"):
        build_notifier({"type": "discord_bot", "channels": {"Flash": 1}})
    with pytest.raises(NotifierConfigError, match="unexpanded environment variable"):
        build_notifier({"type": "discord_bot", "bot_token": "${DISCORD_BOT_TOKEN}", "channels": {"Flash": 1}})


def test_token_not_in_repr():
    n = build()
    assert TOKEN not in repr(n) and TOKEN not in str(n)


# ---------------------------------------------------------------------------
# notifier routing and delivery
# ---------------------------------------------------------------------------


def test_default_channels(api):
    n = notifier(api, channels={"Flash": FLASH, "ket": KET, "Ket2": KET})
    assert n.default_channels(ev(device="Flash")) == [FLASH]
    assert n.default_channels(ev(device="FLASH")) == [FLASH]
    assert n.default_channels(ev(device="Ket")) == [KET]
    assert n.default_channels(lifecycle_event()) == [FLASH, KET]  # every channel, once
    assert n.default_channels(ev(device="Plassys")) == [FLASH, KET]
    with_status = notifier(api, status=STATUS)
    assert with_status.default_channels(lifecycle_event()) == [STATUS]
    assert with_status.default_channels(ev(device="Plassys")) == [STATUS]
    assert with_status.default_channels(ev(device="Ket")) == [KET]


def test_without_bot_accepts_by_min_severity_and_uses_default_channels(api):
    n = notifier(api)
    assert n.recipients(ev(device="Ket")) == [KET]
    assert not n.accepts(ev(severity=Severity.INFO))
    assert n.accepts(lifecycle_event())


def send(n, event):
    async def go():
        try:
            await n.send(event)
        finally:
            await n.aclose()

    asyncio.run(go())


def test_send_posts_to_the_device_channel_with_bot_auth(api):
    send(notifier(api), ev(device="Ket"))
    ((method, path, body, headers),) = api.requests
    assert (method, path) == ("POST", f"/channels/{KET}/messages")
    assert headers["authorization"] == f"Bot {TOKEN}"
    assert headers["user-agent"].startswith("DiscordBot (")
    assert body["embeds"][0]["title"] == "[CRITICAL] Ket: pulse_tube is not on"
    assert body["content"] == "@here [CRITICAL] Ket: pulse_tube is not on"
    assert body["allowed_mentions"] == {"parse": ["everyone"]}


@pytest.mark.parametrize("event", [ev(severity=Severity.WARNING), ev(EventKind.RESOLVED), lifecycle_event()])
def test_no_mention_unless_critical_firing(api, event):
    send(notifier(api), event)
    for body in api.posts():
        assert "content" not in body and body["allowed_mentions"] == {"parse": []}


def test_role_mention_pings_only_that_role(api):
    send(notifier(api, mention="<@&42>"), ev(EventKind.REMINDER))
    (body,) = api.posts()
    assert body["content"].startswith("<@&42> [STILL CRITICAL]")
    assert body["allowed_mentions"] == {"parse": [], "roles": ["42"]}


def test_lifecycle_message_goes_to_every_channel_and_one_failure_is_tolerated(api, caplog):
    api.failing = {FLASH}
    with caplog.at_level(logging.WARNING):
        send(notifier(api), lifecycle_event())  # no exception
    assert sorted(api.posted_channels()) == [FLASH, FLASH, KET]  # the failing channel is retried once
    assert f"channel {FLASH}" in caplog.text and TOKEN not in caplog.text


def test_raises_if_every_channel_fails_without_the_token(api):
    def unauthorized(request):
        return httpx.Response(401, json={"message": f"401: Unauthorized ({request.headers['authorization']})"})

    for cid in (FLASH, KET):
        api.handlers[("POST", f"/channels/{cid}/messages")] = unauthorized
    with pytest.raises(DeliveryError) as exc_info:
        send(notifier(api), lifecycle_event())
    assert "HTTP 401" in str(exc_info.value) and TOKEN not in str(exc_info.value)
    assert len(api.posts()) == 4  # each channel tried twice


def test_transport_error_is_redacted(api):
    def boom(request):
        raise httpx.ConnectError(f"cannot connect, Authorization: Bot {TOKEN}")

    api.handlers[("POST", f"/channels/{FLASH}/messages")] = boom
    with pytest.raises(DeliveryError) as exc_info:
        send(notifier(api), ev())
    assert "ConnectError" in str(exc_info.value) and TOKEN not in str(exc_info.value)


# ---------------------------------------------------------------------------
# html_to_markdown
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "html_text, md",
    [
        ("<b>labwatch status</b>\n🟢 Flash: OK", "**labwatch status**\n🟢 Flash: OK"),
        ("<pre>Temperatures\n  T4K   3.3 K</pre>", "```\nTemperatures\n  T4K   3.3 K\n```"),
        ("try <code>/mute Ket 2h</code>", "try `/mute Ket 2h`"),
        ("Oil temp &gt; 50 C &amp; rising &lt;normal&gt;", "Oil temp > 50 C & rising <normal>"),
        ("<pre>a &lt;b&gt; c</pre>", "```\na <b> c\n```"),  # escaped text is not mistaken for tags
        ("<i>x</i> <a href='y'>z</a>", "x z"),
        ("&#x27;Flash&#x27;", "'Flash'"),
    ],
)
def test_html_to_markdown(html_text, md):
    assert html_to_markdown(html_text) == md


def test_html_to_markdown_of_real_reports(monitor):
    text = html_to_markdown(report.overview_html(monitor.runtimes, [], 0))
    assert text.startswith("**labwatch status**\n") and "<" not in text
    text = html_to_markdown(report.device_html(monitor.runtimes[0], [], 0))
    assert text.startswith("**Flash** · data never") and "<pre>" not in text


# ---------------------------------------------------------------------------
# bootstrap and recipients
# ---------------------------------------------------------------------------


def test_bootstrap_creates_one_subscriber_per_channel(monitor, api):
    bot, n = make_bot(monitor, api, status=STATUS, min_severity=Severity.CRITICAL)
    assert n.recipients_fn == bot.recipients_for
    subs = {s.chat_id: (s.name, s.role, s.level) for s in monitor.store.subscribers()}
    assert subs == {sub_id(FLASH): ("#Flash", "user", "critical"), sub_id(KET): ("#Ket", "user", "critical"),
                    sub_id(STATUS): ("#status", "user", "critical")}
    assert sub_id(FLASH) == "discord:111"


def test_bootstrap_keeps_existing_levels_and_names(monitor, api):
    monitor.store.upsert_subscriber(sub_id(KET), "#ket-fridge", "user", "info")
    monitor.store.personal_mute(sub_id(KET), "anomaly", None)
    make_bot(monitor, api)
    s = monitor.store.subscriber(sub_id(KET))
    assert (s.name, s.level) == ("#ket-fridge", "info")
    assert [m.target for m in monitor.store.personal_mutes(sub_id(KET))] == ["anomaly"]


def test_recipients_per_channel_level_and_mutes(monitor, api):
    bot, _ = make_bot(monitor, api)
    st = monitor.store
    assert bot.recipients_for(ev(device="Flash")) == [FLASH]
    assert bot.recipients_for(ev(device="ket", severity=Severity.WARNING)) == [KET]
    st.upsert_subscriber(sub_id(KET), "#Ket", "user", "critical")
    assert bot.recipients_for(ev(device="Ket", severity=Severity.WARNING)) == []
    assert bot.recipients_for(ev(device="Ket")) == [KET]
    st.personal_mute(sub_id(FLASH), "anomaly", None)
    anomaly = ev(device="Flash", category="anomaly", rule_id="Flash/cw unusual")
    assert bot.recipients_for(anomaly) == []
    assert bot.recipients_for(ev(device="Flash")) == [FLASH]
    assert bot.recipients_for(ev(device="Ket", category="anomaly", rule_id="Ket/cw unusual")) == [KET]
    assert bot.recipients_for(lifecycle_event()) == [FLASH, KET]  # announcements ignore levels


def test_recipients_lifecycle_setting(monitor, api):
    bot, n = make_bot(monitor, api)
    n.lifecycle = False
    assert bot.recipients_for(lifecycle_event()) == []
    assert bot.recipients_for(ev()) == [FLASH]


def test_recipients_fail_open_for_a_channel_without_subscriber(monitor, api):
    bot, _ = make_bot(monitor, api)
    monitor.store.remove_subscriber(sub_id(FLASH))
    assert bot.recipients_for(ev(severity=Severity.INFO)) == [FLASH]


def test_notifier_with_bot_posts_only_where_wanted(monitor, api):
    bot, n = make_bot(monitor, api)
    monitor.store.personal_mute(sub_id(FLASH), "*", None)
    assert n.accepts(ev(severity=Severity.INFO))
    send(n, lifecycle_event())
    assert api.posted_channels() == [KET]


def test_mute_without_what_only_mutes_this_machine(monitor, api):
    bot, _ = make_bot(monitor, api)
    text, _ = bot.respond("mute", {}, FLASH, "ana")
    assert "all **Flash** alerts" in text
    assert bot.recipients_for(ev(device="Flash")) == []
    assert bot.recipients_for(ev(device="Plassys", category="connection")) == [FLASH, KET]
    assert bot.recipients_for(lifecycle_event()) == [FLASH, KET]


# ---------------------------------------------------------------------------
# register_commands
# ---------------------------------------------------------------------------


def test_register_commands(monitor, api):
    bot, _ = make_bot(monitor, api, status=STATUS)
    monitor.store.upsert_subscriber(sub_id(KET), "#Ket", "user", "critical")
    asyncio.run(bot.register_commands())
    paths = [(m, p) for m, p, _, _ in api.requests]
    assert paths[0] == ("GET", "/oauth2/applications/@me")
    assert set(paths[1:4]) == {("GET", f"/channels/{c}") for c in (FLASH, KET, STATUS)}
    assert paths[4:] == [("PUT", f"/applications/{APP}/guilds/{GUILD}/commands")]  # once per server
    assert bot.app_id == APP and bot.guilds == {GUILD}
    for _, _, _, headers in api.requests:
        assert headers["authorization"] == f"Bot {TOKEN}"
    cmds = api.requests[-1][2]
    assert [c["name"] for c in cmds] == ["status", "alerts", "mute", "unmute", "mutes", "level", "muteall",
                                         "unmuteall"]
    by_name = {c["name"]: c for c in cmds}
    assert by_name["status"]["options"][0]["choices"] == [{"name": n, "value": n} for n in ("Flash", "Ket", "Plassys")]
    assert [o["required"] for o in by_name["muteall"]["options"]] == [True, True, False]
    assert by_name["level"]["options"][0]["required"] is True
    assert all(len(c["description"]) <= 100 for c in cmds)
    # channel names from Discord, levels kept
    s = monitor.store.subscriber(sub_id(KET))
    assert (s.name, s.level) == ("#ket-fridge", "critical")
    assert monitor.store.subscriber(sub_id(STATUS)).name == "#labwatch"


def test_register_commands_skips_channels_the_bot_cannot_see(monitor, api, caplog):
    bot, _ = make_bot(monitor, api)
    api.hidden = {KET}
    with caplog.at_level(logging.WARNING):
        asyncio.run(bot.register_commands())
    assert "can't see the channel for Ket (222)" in caplog.text and TOKEN not in caplog.text
    assert bot.guilds == {GUILD}
    assert api.calls("PUT", f"/applications/{APP}/guilds/{GUILD}/commands") == 1
    assert monitor.store.subscriber(sub_id(KET)).name == "#Ket"  # unchanged


def test_register_commands_in_every_server(monitor, api):
    bot, _ = make_bot(monitor, api)
    api.guild_of = {KET: OTHER_GUILD}
    asyncio.run(bot.register_commands())
    assert bot.guilds == {GUILD, OTHER_GUILD}
    for g in (GUILD, OTHER_GUILD):
        assert api.calls("PUT", f"/applications/{APP}/guilds/{g}/commands") == 1


def test_api_errors_name_the_request_but_not_the_token(monitor, api):
    bot, _ = make_bot(monitor, api)

    def boom(request):
        raise httpx.ReadTimeout(f"timed out, token {TOKEN}")

    api.handlers[("GET", "/gateway/bot")] = boom
    with pytest.raises(DiscordAPIError) as exc_info:
        asyncio.run(bot._api("GET", "/gateway/bot"))
    assert str(exc_info.value) == "GET /gateway/bot: ReadTimeout"
    api.handlers[("GET", "/gateway/bot")] = lambda r: httpx.Response(500, text="oops")
    with pytest.raises(DiscordAPIError, match="GET /gateway/bot: HTTP 500 oops"):
        asyncio.run(bot._api("GET", "/gateway/bot"))


# ---------------------------------------------------------------------------
# respond(): every command
# ---------------------------------------------------------------------------


@pytest.fixture
def bot(monitor, api):
    b, _ = make_bot(monitor, api, status=STATUS)
    return b


def pm(bot, cid):
    return {m.target: m.until for m in bot.store.personal_mutes(sub_id(cid))}


def test_status_and_alerts_are_private(bot):
    text, eph = bot.respond("status", {}, FLASH, "ana")
    assert eph is True and text.startswith("**labwatch status**\n")
    assert "Details: `/status device:` /flash /ket /plassys" in text
    text, eph = bot.respond("status", {"device": "ket"}, FLASH, "ana")
    assert eph is True and text.startswith("**Ket** · data never")
    assert bot.respond("status", {"device": "fridge"}, FLASH, "ana") == ("Unknown device 'fridge'.", True)
    assert bot.respond("alerts", {}, FLASH, "ana") == ("🟢 No active alerts.", True)


def test_status_works_in_channels_without_alerts(bot):
    text, eph = bot.respond("status", {}, ELSEWHERE, "ana")
    assert eph is True and text.startswith("**labwatch status**")


def test_mute_without_options_mutes_this_channel_for_two_hours(bot, monkeypatch):
    monkeypatch.setattr(discord_bot, "time", SimpleNamespace(time=lambda: 1_800_000_000.0))
    text, eph = bot.respond("mute", {}, FLASH, "ana")
    assert eph is False
    assert text.startswith("🔕 **ana** muted all **Flash** alerts in this channel until ") and "`/unmute`" in text
    assert pm(bot, FLASH) == {"flash": 1_800_000_000.0 + 7200}  # this channel's machine, not "*"
    assert pm(bot, KET) == {} and bot.store.mutes() == []


def test_mute_in_the_status_channel_says_all_alerts(bot):
    text, eph = bot.respond("mute", {"duration": "1h"}, STATUS, "ana")
    assert "muted all alerts in this channel" in text and eph is False


@pytest.mark.parametrize(
    "options, target, seconds",
    [
        ({"what": "anomaly"}, "anomaly", 7200),
        ({"what": "Ket circulation"}, "ket/circulation", 7200),
        ({"what": "flash/anomaly", "duration": "30m"}, "flash/anomaly", 1800),
        ({"what": "Flash 4h"}, "flash", 4 * 3600),  # duration typed into "what"
        ({"what": "Flash 4h", "duration": "1d"}, "flash", 86400),  # the duration option wins
        ({"what": "all", "duration": "forever"}, "*", None),
    ],
)
def test_mute_options(bot, monkeypatch, options, target, seconds):
    now = 1_800_000_000.0
    monkeypatch.setattr(discord_bot, "time", SimpleNamespace(time=lambda: now))
    text, eph = bot.respond("mute", options, FLASH, "ana")
    assert eph is False and text.startswith("🔕 **ana** muted ")
    assert pm(bot, FLASH) == {target: None if seconds is None else now + seconds}


def test_mute_errors_are_private(bot):
    text, eph = bot.respond("mute", {"what": "fridge"}, FLASH, "ana")
    assert eph is True and text.startswith("unknown target 'fridge'. Try `all`, a device, or an alarm type: ")
    assert "anomaly" in text and "pulse_tube" in text
    assert bot.respond("mute", {"what": "Flash", "duration": "soon"}, FLASH, "ana") == (
        "Unknown duration 'soon': use 30m, 4h, 2d or forever.", True)
    assert bot.respond("mute", {"what": "Flash warm-up"}, FLASH, "ana")[1] is True
    assert pm(bot, FLASH) == {}


@pytest.mark.parametrize("name, options", [("mute", {}), ("unmute", {}), ("level", {"level": "info"})])
def test_channels_without_alerts_cannot_mute_or_set_levels(bot, name, options):
    text, eph = bot.respond(name, options, ELSEWHERE, "ana")
    assert (text, eph) == ("This channel doesn't receive labwatch alerts, so there's nothing to mute here.", True)
    assert bot.store.subscriber(sub_id(ELSEWHERE)) is None
    assert bot.store.personal_mutes(sub_id(ELSEWHERE)) == []


def test_unmute(bot):
    for what in ("Flash", "anomaly", "all"):
        bot.respond("mute", {"what": what}, FLASH, "ana")
    bot.respond("mute", {"what": "Flash"}, KET, "ben")
    assert bot.respond("unmute", {"what": "FLASH"}, FLASH, "ben") == ("🔔 **ben** unmuted Flash in this channel.", False)
    assert set(pm(bot, FLASH)) == {"anomaly", "*"}
    assert bot.respond("unmute", {"what": "Flash"}, FLASH, "ben") == (
        "Nothing like 'Flash' was muted in this channel. `/mutes` shows what is.", True)
    assert bot.respond("unmute", {}, FLASH, "ben") == ("🔔 **ben** unmuted everything in this channel.", False)
    assert pm(bot, FLASH) == {} and set(pm(bot, KET)) == {"flash"}
    assert bot.respond("unmute", {"what": "bogus"}, KET, "ben") == ("unknown target 'bogus'.", True)


def test_mutes_shows_this_channel_and_everyone(bot):
    assert bot.respond("mutes", {}, FLASH, "ana") == ("Nothing is muted.", True)
    bot.respond("mute", {"what": "anomaly"}, KET, "ben")
    assert bot.respond("mutes", {}, FLASH, "ana") == ("Nothing is muted.", True)  # #ket's mute isn't ours
    bot.respond("mute", {"what": "Flash", "duration": "4h"}, FLASH, "ana")
    bot.respond("muteall", {"what": "Ket", "duration": "2d", "reason": "warm-up"}, FLASH, "ana")
    text, eph = bot.respond("mutes", {}, FLASH, "ana")
    assert eph is True
    assert text.startswith("**Muted in this channel**\n🔕 flash until ")
    assert "**Muted for everyone**\n🔕 Ket until " in text and "(warm-up; by ana)" in text
    assert "anomaly" not in text
    assert bot.respond("mutes", {}, ELSEWHERE, "x")[0].startswith("**Muted for everyone**")


def test_level(bot):
    assert bot.respond("level", {"level": "critical"}, KET, "ana") == (
        "⚙️ **ana** set this channel to get only critical alerts.", False)
    assert bot.store.subscriber(sub_id(KET)).level == "critical"
    assert bot.store.subscriber(sub_id(FLASH)).level == "warning"
    assert bot.respond("level", {"level": "Info"}, KET, "ana")[0].endswith("to get everything.")
    assert bot.respond("level", {"level": "loud"}, KET, "ana") == ("Use critical, warning or info.", True)
    assert bot.respond("level", {}, KET, "ana") == ("Use critical, warning or info.", True)
    s = bot.store.subscriber(sub_id(KET))
    assert (s.level, s.role) == ("info", "user")


def test_muteall(bot, monkeypatch):
    now = 1_800_000_000.0
    monkeypatch.setattr(discord_bot, "time", SimpleNamespace(time=lambda: now))
    text, eph = bot.respond("muteall", {"what": "Ket", "duration": "2d", "reason": " warm-up "}, FLASH, "ana")
    assert eph is False
    assert text.startswith("🔕 **ana** muted **Ket** for everyone until ")
    assert "All channels and Telegram users will be told." in text
    (m,) = bot.store.mutes(now)
    assert (m.target, m.until, m.reason) == ("Ket", now + 2 * 86400, "warm-up; by ana")
    assert pm(bot, FLASH) == {}  # not a channel mute
    bot.respond("muteall", {"what": "anomaly", "duration": "14d"}, FLASH, "ben")
    assert {m.target: m.reason for m in bot.store.mutes(now)} == {"Ket": "warm-up; by ana", "anomaly": "by ben"}


@pytest.mark.parametrize(
    "options, reply",
    [
        ({"what": "Ket"}, "Give a duration like 4h or 2d (mutes for everyone always end)."),
        ({"what": "Ket", "duration": "forever"}, "Give a duration like 4h or 2d (mutes for everyone always end)."),
        ({"what": "Ket", "duration": "soon"}, "Give a duration like 4h or 2d (mutes for everyone always end)."),
        ({"what": "Ket", "duration": "15d"}, "At most 14 days for everyone."),
        ({"what": "all", "duration": "337h"}, "At most 14 days for everyone."),
        ({"what": "fridge", "duration": "2d"}, "unknown target 'fridge'."),
        ({"duration": "2d"}, "say what to mute."),
    ],
)
def test_muteall_limits(bot, options, reply):
    assert bot.respond("muteall", options, FLASH, "ana") == (reply, True)
    assert bot.store.mutes() == []


def test_unmuteall(bot):
    bot.respond("muteall", {"what": "Ket", "duration": "2d"}, FLASH, "ana")
    assert bot.respond("unmuteall", {"what": "ket"}, KET, "ben") == (
        "🔔 **ben** removed the mute for everyone on **Ket**.", False)
    assert bot.store.mutes() == []
    assert bot.respond("unmuteall", {"what": "Ket"}, KET, "ben") == ("Ket wasn't muted for everyone.", True)
    assert bot.respond("unmuteall", {"what": "fridge"}, KET, "ben") == ("unknown target 'fridge'.", True)


def test_unknown_command(bot):
    assert bot.respond("frobnicate", {}, FLASH, "ana") == ("Unknown command.", True)


def test_channel_mute_does_not_touch_other_channels_or_telegram_people(bot):
    bot.store.upsert_subscriber("200", "Ana", "user", "warning")  # a Telegram user
    bot.respond("mute", {"what": "anomaly", "duration": "forever"}, FLASH, "ana")
    bot.respond("level", {"level": "critical"}, FLASH, "ana")
    assert bot.store.personal_mutes("200") == [] and bot.store.subscriber("200").level == "warning"
    assert pm(bot, KET) == {} and bot.store.subscriber(sub_id(KET)).level == "warning"
    assert bot.store.mutes() == []


# ---------------------------------------------------------------------------
# handle_interaction
# ---------------------------------------------------------------------------


def interaction(name, options=None, channel=FLASH, guild=GUILD, itype=2, member=None, user=None, iid="9001"):
    d = {"id": iid, "token": "itoken-abc_1", "type": itype, "channel_id": channel,
         "data": {"name": name, "options": [{"name": k, "type": 3, "value": v} for k, v in (options or {}).items()]}}
    if guild is not None:
        d["guild_id"] = guild
    if member is not None:
        d["member"] = member
    if user is not None:
        d["user"] = user
    return d


def member(nick=None, global_name="Ana L.", username="ana_l"):
    return {"nick": nick, "user": {"id": "42", "username": username, "global_name": global_name}}


def handle(bot, d):
    asyncio.run(bot.handle_interaction(d))


def test_interaction_reply_is_posted_to_the_callback(bot, api):
    bot.guilds = {GUILD}
    handle(bot, interaction("mute", {"what": "anomaly"}, member=member(nick="Ana")))
    ((path, body),) = api.callbacks()
    assert path == "/interactions/9001/itoken-abc_1/callback"
    assert body["type"] == 4
    assert body["data"]["flags"] == 0  # public: everyone in #flash sees who muted what
    assert body["data"]["allowed_mentions"] == {"parse": []}
    assert body["data"]["content"].startswith("🔕 **Ana** muted **anomaly** alerts in this channel")
    assert set(pm(bot, FLASH)) == {"anomaly"}


def test_interaction_status_reply_is_ephemeral(bot, api):
    handle(bot, interaction("status", {"device": "Flash"}, member=member()))
    ((_, body),) = api.callbacks()
    assert body["data"]["flags"] == EPHEMERAL == 64
    assert body["data"]["content"].startswith("**Flash** · data ")


@pytest.mark.parametrize(
    "who, name",
    [
        ({"member": member(nick="Nick", global_name="Global", username="user")}, "Nick"),
        ({"member": member(nick=None, global_name="Global", username="user")}, "Global"),
        ({"member": member(nick=None, global_name=None, username="user")}, "user"),
        ({"user": {"id": "1", "username": "dm_user"}}, "dm_user"),
        ({}, "someone"),
    ],
)
def test_interaction_user_name(bot, api, who, name):
    handle(bot, interaction("level", {"level": "critical"}, **who))
    ((_, body),) = api.callbacks()
    assert body["data"]["content"] == f"⚙️ **{name}** set this channel to get only critical alerts."


def test_mentions_in_user_names_never_ping(bot, api):
    handle(bot, interaction("mute", {}, member=member(nick="@everyone")))
    ((_, body),) = api.callbacks()
    assert "@everyone" in body["data"]["content"]
    assert body["data"]["allowed_mentions"] == {"parse": []}


def test_interactions_from_other_servers_are_ignored(bot, api):
    bot.guilds = {GUILD}
    handle(bot, interaction("status", guild=OTHER_GUILD, member=member()))
    handle(bot, interaction("mute", {"what": "all"}, guild=OTHER_GUILD, member=member()))
    assert api.requests == [] and pm(bot, FLASH) == {}


@pytest.mark.parametrize("itype", [1, 3, 4, 5])
def test_non_command_interactions_are_ignored(bot, api, itype):
    handle(bot, interaction("mute", {"what": "all"}, itype=itype, member=member()))
    assert api.requests == [] and pm(bot, FLASH) == {}


def test_failing_command_answers_sorry(bot, api, monkeypatch, caplog):
    def broken(*a):
        raise RuntimeError("bug")

    monkeypatch.setattr(bot, "respond", broken)
    with caplog.at_level(logging.ERROR):
        handle(bot, interaction("status", member=member()))
    ((_, body),) = api.callbacks()
    assert body["data"]["content"] == "Sorry, that failed (see the labwatch log)."
    assert body["data"]["flags"] == EPHEMERAL
    assert "command status failed" in caplog.text


def test_long_replies_are_cut_to_discords_limit(bot, api, monkeypatch):
    monkeypatch.setattr(bot, "respond", lambda *a: ("x" * 5000, True))
    handle(bot, interaction("status", member=member()))
    ((_, body),) = api.callbacks()
    content = body["data"]["content"]
    assert len(content) <= 2000 and content.endswith("\n…")


# ---------------------------------------------------------------------------
# gateway session
# ---------------------------------------------------------------------------


@pytest.fixture
def no_jitter(monkeypatch):
    monkeypatch.setattr(discord_bot, "random", SimpleNamespace(random=lambda: 0.0, uniform=lambda a, b: 0.0))


def session(bot, ws):
    asyncio.run(bot._session(ws))


def test_identify_with_token_and_no_intents(monitor, api):
    bot, _ = make_bot(monitor, api)
    ws = FakeWS([HELLO])
    session(bot, ws)
    assert ws.sent[0] == {"op": 2, "d": {"token": TOKEN, "intents": 0, "properties": {
        "os": sys.platform, "browser": "labwatch", "device": "labwatch"}}}


def test_heartbeat_request_is_answered_with_the_last_sequence(monitor, api):
    bot, _ = make_bot(monitor, api)
    ws = FakeWS([HELLO, {"op": 0, "t": "READY", "s": 1, "d": {}}, {"op": 0, "t": "GUILD_CREATE", "s": 7, "d": {}},
                 {"op": 1, "d": None}])
    session(bot, ws)
    assert ws.sent[1:] == [{"op": 1, "d": 7}]
    assert bot._seq == 7


def test_heartbeat_ack_is_recorded(monitor, api):
    bot, _ = make_bot(monitor, api)
    seen = []

    def before(msg):
        if msg["op"] == 11:
            bot._acked = False  # as if a heartbeat had just been sent
        seen.append(msg["op"])

    session(bot, FakeWS([HELLO, {"op": 11}], on_message=before))
    assert seen == [11] and bot._acked is True


def test_heartbeat_loop_sends_beats_and_detects_a_zombie_connection(monitor, api, no_jitter):
    bot, _ = make_bot(monitor, api)
    bot._seq = 5
    acks = {"left": 3}
    ws = FakeWS([])

    async def send(m):
        ws.sent.append(json.loads(m))
        if acks["left"]:
            acks["left"] -= 1
            bot._acked = True  # Discord answered with op 11

    ws.send = send
    bot._acked = True
    asyncio.run(asyncio.wait_for(bot._heartbeat(ws, 0.001), 5))
    assert ws.sent == [{"op": 1, "d": 5}] * 4  # 3 acknowledged + 1 that never was
    assert ws.closed == [(4000, "heartbeat timeout")]


@pytest.mark.parametrize("op", [7, 9])
def test_reconnect_and_invalid_session_end_the_session(monitor, api, no_jitter, op):
    bot, _ = make_bot(monitor, api)
    later = {"op": 0, "t": "INTERACTION_CREATE", "s": 3, "d": interaction("mute", {"what": "all"}, member=member())}
    ws = FakeWS([HELLO, {"op": op, "d": False}, later])
    session(bot, ws)
    assert len(ws.msgs) == 1  # the rest is left for the next connection
    assert pm(bot, FLASH) == {}


def test_interaction_over_the_gateway_is_answered(monitor, api):
    bot, _ = make_bot(monitor, api)
    msg = {"op": 0, "t": "INTERACTION_CREATE", "s": 2, "d": interaction("mute", {"what": "anomaly"}, member=member())}

    async def go():
        await bot._session(FakeWS([HELLO, msg]))
        for _ in range(100):
            if api.callbacks():
                break
            await _real_sleep(0.005)

    asyncio.run(go())
    ((path, body),) = api.callbacks()
    assert path == "/interactions/9001/itoken-abc_1/callback" and body["data"]["flags"] == 0
    assert set(pm(bot, FLASH)) == {"anomaly"}


@pytest.mark.parametrize("code", sorted(FATAL_CLOSE_CODES))
def test_fatal_close_codes_raise(monitor, api, code):
    bot, _ = make_bot(monitor, api)
    with pytest.raises(FatalGatewayError) as exc_info:
        session(bot, FakeWS([HELLO], close_code=code))
    assert str(code) in str(exc_info.value) and TOKEN not in str(exc_info.value)


@pytest.mark.parametrize("code", [None, 1000, 1001, 1006, 4000, 4009])
def test_other_close_codes_just_end_the_session(monitor, api, code):
    bot, _ = make_bot(monitor, api)
    session(bot, FakeWS([HELLO], close_code=code))


# ---------------------------------------------------------------------------
# run(): reconnect, backoff, fatal errors
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_sleep(monkeypatch):
    delays: list[float] = []

    async def sleep(d, *a, **k):
        delays.append(d)
        await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    return delays


def test_run_reconnects_with_backoff_and_stops_on_a_fatal_close(monitor, api, fast_sleep, caplog):
    conn = FakeConnect([OSError("connection refused"), OSError("connection refused"),
                        FakeWS([HELLO], close_code=1000),  # a normal session: backoff starts over
                        ConnectionResetError("reset"),
                        FakeWS([HELLO], close_code=4004)])  # authentication failed
    bot, n = make_bot(monitor, api, connect=conn)
    with caplog.at_level(logging.WARNING):
        asyncio.run(asyncio.wait_for(bot.run(), 10))
    assert [d for d in fast_sleep if d >= 1] == [5.0, 10.0, 5.0]
    assert len(conn.calls) == 5
    assert all(c == ("wss://gateway.discord.gg/?v=10&encoding=json", {"max_size": 2**22}) for c in conn.calls)
    assert api.calls("GET", "/oauth2/applications/@me") == 1  # commands registered once
    assert api.calls("GET", "/gateway/bot") == 5
    assert "Discord commands disabled (alerts are still posted)" in caplog.text
    assert TOKEN not in caplog.text
    assert bot._client.is_closed
    # alerts still go out, with each channel's settings
    monitor.store.upsert_subscriber(sub_id(FLASH), "#Flash", "user", "critical")
    send(n, ev(device="Flash", severity=Severity.WARNING))
    send(n, ev(device="Flash"))
    assert api.posted_channels() == [FLASH]
    assert api.posts(FLASH)[0]["content"].startswith("@here [CRITICAL]")


def test_run_retries_registering_commands(monitor, api, fast_sleep):
    failures = {"n": 1}

    def flaky(request):
        if failures["n"]:
            failures["n"] -= 1
            return httpx.Response(502, text="bad gateway")
        return None

    api.handlers[("GET", "/oauth2/applications/@me")] = flaky
    bot, _ = make_bot(monitor, api, connect=FakeConnect([FakeWS([HELLO], close_code=4014)]))
    asyncio.run(asyncio.wait_for(bot.run(), 10))
    assert api.calls("GET", "/oauth2/applications/@me") == 2
    assert [d for d in fast_sleep if d >= 1] == [5.0]


def test_run_backoff_is_capped(monitor, api, fast_sleep):
    bot, _ = make_bot(monitor, api, connect=FakeConnect([OSError("down")] * 9))
    asyncio.run(asyncio.wait_for(bot.run(), 10))
    assert [d for d in fast_sleep if d >= 1] == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0, 300.0]
