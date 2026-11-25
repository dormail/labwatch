"""End to end: two scripted fridges in a Monitor on a fake clock, the Telegram notifier + bot and the
Discord bot notifier + bot on fake APIs. Assertions are on the HTTP requests that were actually made.
Also: global mute announcements, command_bots(), the helper supervisor and category mutes."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time as _time
from datetime import datetime, timezone

import httpx
import pytest

from labwatch import discord_bot, engine, report, state, subscriptions, telegram_bot
from labwatch.config import Config, Section
from labwatch.devices.base import Device, register
from labwatch.devices.bluefors import DEFAULT_CHANNELS, bluefors_category
from labwatch.discord_bot import DiscordCommandBot, sub_id
from labwatch.engine import Monitor, build_runtime
from labwatch.models import AlertEvent, EventKind, Reading, Severity
from labwatch.notifiers.base import is_lifecycle
from labwatch.notifiers.discord_bot import DiscordBotNotifier
from labwatch.notifiers.telegram import TelegramNotifier
from labwatch.state import StateStore
from labwatch.telegram_bot import TelegramCommandBot

TG_TOKEN = "123456789:AAH-scenario-telegram-s3cr3t"
DC_TOKEN = "MTIzNDU2Nzg5.scenario.discord-s3cr3t"
TG_HOST, DC_HOST = "api.telegram.org", "discord.com"
ADMIN, ANA, BEN, EVE = "100", "200", "300", "999"
FLASH, KET = "111", "222"
GUILD, APP = "5550001", "7770001"
ACCESS = ("👋 This is a private lab monitoring bot. I've asked an admin to give you access; "
          "you'll get a message when that happens.")
WAITING = "⏳ Still waiting for an admin to approve you."
_real_sleep = asyncio.sleep

CHANNELS = ("T50K", "T4K", "Tstill", "pulse_tube", "compressor", "flow", "cooling_water_in")
COLD = {"T50K": 45.0, "T4K": 3.2, "Tstill": 0.8, "pulse_tube": 1.0, "compressor": 1.0, "flow": 0.6,
        "cooling_water_in": 290.0}
DEFAULTS = {"for": 60, "clear_for": 120, "auto_arm": True, "arm_after": "10m", "repeat_interval": "2h"}
RULES = [
    {"channel": "pulse_tube", "not_equals": 1, "severity": "critical"},  # category pulse_tube
    {"channel": "T4K", "above": 5, "severity": "warning"},  # temperature
    {"channel": "cooling_water_in", "deviation": 5, "baseline": "1h", "exclude_recent": "5m",  # anomaly
     "min_baseline": "20m", "name": "cooling water unusual"},
]


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


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def time(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t

    def __getattr__(self, name):
        return getattr(_time, name)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    """One fake clock for the engine, both bots, the subscriptions and the state store."""
    c = Clock(1_800_000_000.0)
    for mod in (engine, telegram_bot, discord_bot, subscriptions, state, report):
        monkeypatch.setattr(mod, "time", c)
    return c


class FakeAPIs:
    """Telegram Bot API + Discord REST API on one MockTransport."""

    def __init__(self) -> None:
        self.tg: list[tuple[str, dict]] = []
        self.dc: list[tuple[str, str, object]] = []
        self.violations: list[str] = []
        self.tg_delay = 0.0
        self.transport = httpx.MockTransport(self)

    def __call__(self, request: httpx.Request):
        url = request.url
        body = json.loads(request.content) if request.content else None
        if url.scheme != "https":
            self.violations.append(f"{request.method} {url.scheme}://{url.host}")
            return httpx.Response(599)
        if url.host == TG_HOST and url.path.startswith(f"/bot{TG_TOKEN}/"):
            method = url.path.rsplit("/", 1)[1]
            self.tg.append((method, body or {}))
            resp = httpx.Response(200, json={"ok": True, "result": [] if method == "getUpdates" else {"message_id": 1}})
            if method == "sendMessage" and self.tg_delay:
                return self._later(resp, self.tg_delay)
            return resp
        if url.host == DC_HOST and url.path.startswith("/api/v10/"):
            if request.headers.get("authorization") != f"Bot {DC_TOKEN}":
                self.violations.append("discord request without the bot token")
            path = url.path[len("/api/v10"):]
            self.dc.append((request.method, path, body))
            if m := re.fullmatch(r"/channels/(\d+)", path):
                return httpx.Response(200, json={"id": m.group(1), "guild_id": GUILD, "name": f"chan-{m.group(1)}"})
            if path == "/oauth2/applications/@me":
                return httpx.Response(200, json={"id": APP})
            if path.startswith("/interactions/"):
                return httpx.Response(204)
            return httpx.Response(200, json=body if request.method == "PUT" else {"id": "1"})
        self.violations.append(f"{request.method} {url.host}")
        return httpx.Response(599)

    @staticmethod
    async def _later(resp, delay):
        await _real_sleep(delay)
        return resp

    # Telegram
    def tg_messages(self, chat=None) -> list[dict]:
        return [p for m, p in self.tg if m == "sendMessage" and (chat is None or str(p["chat_id"]) == chat)]

    def tg_texts(self, chat=None) -> list[str]:
        return [p["text"] for p in self.tg_messages(chat)]

    def tg_chats(self, contains: str = "") -> list[str]:
        return sorted(str(p["chat_id"]) for p in self.tg_messages() if contains in p["text"])

    # Discord
    def dc_posts(self, cid=None) -> list[dict]:
        return [b for m, p, b in self.dc if m == "POST" and re.fullmatch(r"/channels/\d+/messages", p)
                and (cid is None or p == f"/channels/{cid}/messages")]

    def dc_titles(self, cid) -> list[str]:
        return [b["embeds"][0]["title"] for b in self.dc_posts(cid)]

    def dc_channels(self) -> list[str]:
        return sorted({p.split("/")[2] for m, p, _ in self.dc if m == "POST" and p.endswith("/messages")})

    def callbacks(self) -> list[dict]:
        return [b for m, p, b in self.dc if p.startswith("/interactions/")]

    def clear(self) -> None:
        self.tg.clear()
        self.dc.clear()


@pytest.fixture
def api():
    a = FakeAPIs()
    yield a
    assert not a.violations, a.violations


@register("chatfridge")
class ChatFridge(Device):
    """A Bluefors-like fridge (Bluefors alarm categories) returning the values a test sets."""

    summary_channels = ("T4K", "pulse_tube")
    overview_channels = ("T4K", "pulse_tube")

    def __init__(self, name: str, clock: Clock) -> None:
        super().__init__(name, {k: DEFAULT_CHANNELS[k] for k in CHANNELS}, poll_interval=30.0,
                         unreachable_after=300.0)
        self.clock = clock
        self.values: dict[str, float] = {}
        self.polls = 0

    def category_for(self, channel: str) -> str:
        return bluefors_category(channel)

    @classmethod
    def from_config(cls, name, cfg):
        return cls(name, cfg["clock"])

    async def poll(self) -> list[Reading]:
        self.polls += 1
        ts = datetime.fromtimestamp(self.clock.t, timezone.utc)
        return [Reading(self.name, ch, self.channels[ch].source, v, ts, self.channels[ch].unit, raw=str(v),
                        status="OK", label=self.channels[ch].label_for(v)) for ch, v in self.values.items()]


_ids = iter(range(1, 10**6))


def tg_update(chat, text, first) -> dict:
    uid = next(_ids)
    return {"update_id": uid, "message": {"message_id": uid, "chat": {"id": int(chat), "type": "private"},
                                          "from": {"id": int(chat), "first_name": first}, "text": text}}


class Lab:
    """Flash and Ket in a Monitor, polled every 30 s of fake time, with chat bots as `labwatch run` sets them up."""

    def __init__(self, tmp_path, clock: Clock, api: FakeAPIs, *, store: StateStore | None = None,
                 telegram: bool = True, discord: bool = False, status_channel: str | None = None) -> None:
        self.clock, self.api = clock, api
        self.config = Config(path=tmp_path / "config.yaml", state_db=tmp_path / "state.sqlite", history_days=30,
                             rule_defaults=dict(DEFAULTS), devices=[], notifiers=[], heartbeat_url=None,
                             heartbeat_interval=60.0, daily_summary=None, notify_startup=False, secrets=set())
        self.rts = {}
        for name in ("Flash", "Ket"):
            sec = Section({"name": name, "type": "chatfridge", "clock": clock, "rules": RULES}, set())
            self.config.devices.append(sec)
            self.rts[name] = build_runtime(self.config, sec)
            self.rts[name].unreachable.started = clock.t
        self.store = store if store is not None else StateStore(":memory:")
        notifiers = []
        if telegram:
            self.tg = TelegramNotifier("telegram", Severity.WARNING, TG_TOKEN, ADMIN, transport=api.transport)
            notifiers.append(self.tg)
        if discord:
            self.dc = DiscordBotNotifier("discord", Severity.WARNING, DC_TOKEN, {"Flash": FLASH, "Ket": KET},
                                         status_channel=status_channel, mention="@here", transport=api.transport)
            notifiers.append(self.dc)
        self.monitor = Monitor(self.config, list(self.rts.values()), notifiers, self.store)
        self.monitor._restore_state()
        self.tbot = self.dbot = None
        for bot in self.monitor.command_bots():  # what Monitor.run() does (without running them)
            old = bot._client
            if isinstance(bot, TelegramCommandBot):
                bot._client = httpx.AsyncClient(timeout=10, transport=api.transport)
                self.tbot = bot
            else:
                bot._client = httpx.AsyncClient(base_url=bot.n.api_base, headers=bot.n.headers, timeout=20,
                                                transport=api.transport)
                self.dbot = bot
                self.dbot.guilds = {GUILD}  # as after register_commands()
            asyncio.run(old.aclose())
        self.monitor.check_mute_changes()  # the mute watcher's first look

    def set(self, device: str, **values) -> None:
        self.rts[device].device.values.update(values)

    def step(self, dt: float = 30.0) -> None:
        self.clock.t += dt

        async def go():
            for rt in self.rts.values():
                await self.monitor.poll_once(rt)
            self.monitor.check_mute_changes()  # the mute watcher (every 15 s in `labwatch run`)
            while not self.monitor._queue.empty():
                await self.monitor.dispatch(self.monitor._queue.get_nowait(), retries=False)

        asyncio.run(go())

    def run_for(self, seconds: float) -> None:
        for _ in range(round(seconds / 30)):
            self.step()

    def cool_down(self) -> None:
        for name in self.rts:
            self.set(name, **COLD)
        self.run_for(40 * 60)
        for rt in self.rts.values():
            assert all(r.state.armed for r in rt.all_rules), [(r.rule_id, r.status_text()) for r in rt.all_rules]

    # chat
    def say(self, chat, text, first="Ana") -> None:
        asyncio.run(self.tbot.handle_update(tg_update(chat, text, first)))

    def approve(self, chat, first) -> None:
        self.say(chat, "/start", first)
        self.say(ADMIN, f"/approve {chat}", "Admin")

    def slash(self, name, options, channel, user) -> dict:
        n = len(self.api.callbacks())
        d = {"id": str(next(_ids)), "token": "itok", "type": 2, "guild_id": GUILD, "channel_id": channel,
             "member": {"nick": user, "user": {"id": "1", "username": user.lower()}},
             "data": {"name": name, "options": [{"name": k, "type": 3, "value": v} for k, v in options.items()]}}
        asyncio.run(self.dbot.handle_interaction(d))
        (cb,) = self.api.callbacks()[n:]
        return cb["data"]


# ---------------------------------------------------------------------------
# scenario 1: three Telegram people
# ---------------------------------------------------------------------------


def test_three_telegram_people_levels_personal_mutes_and_mute_for_everyone(tmp_path, clock, api):
    lab = Lab(tmp_path, clock, api)
    lab.approve(ANA, "Ana")
    lab.approve(BEN, "Ben")
    lab.say(ANA, "/level critical", "Ana")
    lab.say(BEN, "/mute anomaly 1d", "Ben")
    api.clear()

    lab.cool_down()
    for chat in (ADMIN, ANA, BEN):  # status messages reach everybody, whatever their level
        assert any("Ket: alarms armed" in t for t in api.tg_texts(chat)), chat
    api.clear()

    # A critical pulse-tube alert reaches all three, and it rings.
    lab.set("Ket", pulse_tube=0.0)
    lab.run_for(120)
    msgs = api.tg_messages()
    assert sorted(str(m["chat_id"]) for m in msgs) == [ADMIN, ANA, BEN]
    for m in msgs:
        assert m["text"].startswith("<b>[CRITICAL] Ket: pulse_tube is not on</b>")
        assert m["disable_notification"] is False
    api.clear()

    # An anomaly warning reaches only the admin (Ana: critical only, Ben: muted anomalies).
    lab.set("Flash", cooling_water_in=300.0)
    lab.run_for(180)
    (m,) = api.tg_messages()
    assert str(m["chat_id"]) == ADMIN and m["disable_notification"] is True
    assert m["text"].startswith("<b>[WARNING] Flash: cooling water unusual</b>")
    api.clear()

    # Ben's mute doesn't affect Ana: once she wants warnings, she gets the anomalies Ben muted.
    lab.say(ANA, "/level warning", "Ana")
    api.clear()
    lab.set("Ket", cooling_water_in=300.0)
    lab.run_for(180)
    assert api.tg_chats("[WARNING] Ket: cooling water unusual") == [ADMIN, ANA]
    assert api.tg_texts(BEN) == []
    api.clear()

    # /muteall Ket 2d: announced to everybody, then Ket is silent for everyone.
    lab.say(ANA, "/muteall Ket 2d warm-up", "Ana")
    assert api.tg_texts(ANA) == [api.tg_texts(ANA)[0]] and api.tg_texts(ANA)[0].startswith("🔕 Muted Ket for everyone")
    api.clear()
    lab.step()
    assert api.tg_chats("Ket muted for everyone") == [ADMIN, ANA, BEN]
    assert all("warm-up; by Ana" in t for t in api.tg_texts() if "muted for everyone" in t)
    api.clear()
    lab.set("Ket", pulse_tube=1.0, T4K=6.0, cooling_water_in=290.0)
    lab.set("Flash", T4K=6.0)
    lab.run_for(3600)
    assert not [t for t in api.tg_texts() if "Ket" in t]
    assert api.tg_chats("[WARNING] Flash: T4K above 5 K") == [ADMIN, ANA, BEN]  # Flash isn't muted
    lab.say(BEN, "/mutes", "Ben")
    mutes = api.tg_texts(BEN)[-1]
    assert "<b>Muted for you</b>\n  🔕 anomaly" in mutes and "<b>Muted for everyone</b>\n  🔕 Ket" in mutes


# ---------------------------------------------------------------------------
# scenario 2: Discord channels per machine (+ a Telegram admin)
# ---------------------------------------------------------------------------


def test_discord_channel_per_machine_with_channel_mutes_and_levels(tmp_path, clock, api):
    lab = Lab(tmp_path, clock, api, discord=True)
    asyncio.run(lab.dbot.register_commands())
    assert lab.dbot.guilds == {GUILD}
    api.clear()
    lab.cool_down()
    assert "[INFO] Flash: alarms armed" in api.dc_titles(FLASH)
    assert "[INFO] Flash: alarms armed" not in api.dc_titles(KET)
    api.clear()

    # Flash alerts go only to #flash, and critical ones carry the @here mention.
    lab.set("Flash", pulse_tube=0.0)
    lab.run_for(120)
    assert api.dc_channels() == [FLASH]
    (body,) = api.dc_posts(FLASH)
    assert body["embeds"][0]["title"] == "[CRITICAL] Flash: pulse_tube is not on"
    assert body["content"] == "@here [CRITICAL] Flash: pulse_tube is not on"
    assert body["allowed_mentions"] == {"parse": ["everyone"]}
    assert api.tg_chats("[CRITICAL] Flash: pulse_tube is not on") == [ADMIN]
    api.clear()

    # /mute anomaly in #flash: silences anomalies in #flash only.
    reply = lab.slash("mute", {"what": "anomaly"}, FLASH, "Ana")
    assert reply["flags"] == 0 and reply["content"].startswith("🔕 **Ana** muted **anomaly** alerts in this channel")
    api.clear()
    lab.set("Flash", cooling_water_in=300.0)
    lab.set("Ket", cooling_water_in=300.0)
    lab.run_for(180)
    assert api.dc_posts(FLASH) == []
    (body,) = api.dc_posts(KET)
    assert body["embeds"][0]["title"] == "[WARNING] Ket: cooling water unusual"
    assert "content" not in body and body["allowed_mentions"] == {"parse": []}  # warnings don't ping
    assert api.tg_chats("[WARNING] Flash: cooling water unusual") == [ADMIN]  # Telegram is unaffected
    assert api.tg_chats("[WARNING] Ket: cooling water unusual") == [ADMIN]
    api.clear()

    # /level critical in #ket: warnings no longer arrive there, critical alerts (with @here) still do.
    reply = lab.slash("level", {"level": "critical"}, KET, "Ben")
    assert reply["flags"] == 0 and "only critical alerts" in reply["content"]
    api.clear()
    lab.set("Ket", T4K=6.0)
    lab.run_for(180)
    assert api.dc_posts(KET) == []
    assert api.tg_chats("[WARNING] Ket: T4K above 5 K") == [ADMIN]
    lab.set("Ket", pulse_tube=0.0)
    lab.run_for(120)
    (body,) = api.dc_posts(KET)
    assert body["content"] == "@here [CRITICAL] Ket: pulse_tube is not on"
    assert api.dc_posts(FLASH) == []
    api.clear()

    # A mute for everyone from Discord is announced in both channels and on Telegram.
    reply = lab.slash("muteall", {"what": "Ket", "duration": "2d", "reason": "warm-up"}, KET, "Ben")
    assert reply["flags"] == 0
    lab.step()
    for cid in (FLASH, KET):
        assert api.dc_titles(cid) == ["[INFO] 🔕 Ket muted for everyone"]
    assert api.tg_chats("Ket muted for everyone") == [ADMIN]


def test_discord_status_channel_gets_labwatch_news(tmp_path, clock, api):
    lab = Lab(tmp_path, clock, api, discord=True, status_channel="333")
    lab.store.mute("anomaly", None, "by the CLI")  # `labwatch mute anomaly`
    lab.step()
    assert api.dc_channels() == ["333"]
    assert api.dc_titles("333") == ["[INFO] 🔕 anomaly muted for everyone"]


# ---------------------------------------------------------------------------
# scenario 3: a stranger never sees lab data
# ---------------------------------------------------------------------------


def test_stranger_never_receives_lab_data_until_approved(tmp_path, clock, api):
    lab = Lab(tmp_path, clock, api)
    lab.cool_down()
    lab.set("Flash", pulse_tube=0.0)
    lab.run_for(120)  # an alert is firing
    api.clear()

    for cmd in ("/start", "/status", "/flash", "/ket", "/alerts", "/mutes", "/help", "/users", "/status Flash",
                "hello", f"/approve {EVE}", "/mute all", "/level info", "/STATUS@labwatch_bot"):
        lab.say(EVE, cmd, "Eve")
        clock.t += 700  # past the "still waiting" rate limit every time
        lab.step()  # and the monitor keeps alerting meanwhile
    lab.set("Ket", pulse_tube=0.0, T4K=6.0)
    lab.run_for(300)
    assert api.tg_texts(ADMIN)  # the lab is busy ...
    assert set(api.tg_texts(EVE)) == {ACCESS, WAITING}  # ... but Eve hears nothing about it
    assert api.tg_texts(EVE)[0] == ACCESS
    assert len([t for t in api.tg_texts(ADMIN) if "Access request" in t]) == 1
    s = lab.store.subscriber(EVE)
    assert (s.role, s.level) == ("pending", "warning") and lab.store.personal_mutes(EVE) == []

    # approved: welcome, then data and alerts
    api.clear()
    lab.say(ADMIN, f"/approve {EVE}", "Admin")
    assert api.tg_texts(EVE)[0].startswith("✅ You now receive labwatch alerts.")
    lab.say(EVE, "/status", "Eve")
    assert api.tg_texts(EVE)[-1].startswith("<b>labwatch status</b>\n🔴 Flash: ")
    lab.set("Flash", T4K=6.0)
    lab.run_for(120)
    assert EVE in api.tg_chats("[WARNING] Flash: T4K above 5 K")


# ---------------------------------------------------------------------------
# scenario 4: restart on the same database
# ---------------------------------------------------------------------------


def test_people_levels_and_mutes_survive_a_restart(tmp_path, clock, api):
    db = tmp_path / "state.sqlite"
    lab = Lab(tmp_path, clock, api, store=StateStore(db), discord=True)
    lab.approve(ANA, "Ana")
    lab.approve(BEN, "Ben")
    lab.say(EVE, "/start", "Eve")  # still pending
    lab.say(ADMIN, "/level info", "Admin")
    lab.say(ANA, "/level critical", "Ana")
    lab.say(BEN, "/mute anomaly forever", "Ben")
    lab.slash("level", {"level": "critical"}, KET, "Ben")
    lab.slash("mute", {"what": "anomaly", "duration": "1d"}, FLASH, "Ana")
    lab.say(ANA, "/muteall Ket 2d warm-up", "Ana")
    lab.step()
    assert api.tg_chats("Ket muted for everyone") == [ADMIN, ANA, BEN]
    lab.store.close()
    api.clear()

    clock.t += 600
    lab2 = Lab(tmp_path, clock, api, store=StateStore(db), discord=True)
    subs = {s.chat_id: (s.role, s.level) for s in lab2.store.subscribers(("admin", "user", "pending"))}
    assert subs == {ADMIN: ("admin", "info"), ANA: ("user", "critical"), BEN: ("user", "warning"),
                    EVE: ("pending", "warning"), sub_id(FLASH): ("user", "warning"),
                    sub_id(KET): ("user", "critical")}
    assert [(m.target, m.until) for m in lab2.store.personal_mutes(BEN)] == [("anomaly", None)]
    assert [m.target for m in lab2.store.personal_mutes(sub_id(FLASH))] == ["anomaly"]
    assert [m.target for m in lab2.store.mutes()] == ["Ket"]

    lab2.cool_down()  # rules re-arm; the mute for everyone is not announced again
    assert not [t for t in api.tg_texts() if "muted for everyone" in t]
    assert not [t for t in api.tg_texts() if "Ket" in t]  # Ket is muted: not even "alarms armed"
    api.clear()

    lab2.set("Flash", cooling_water_in=300.0)
    lab2.run_for(180)
    assert api.tg_chats("Flash: cooling water unusual") == [ADMIN]  # Ana: critical only, Ben: muted
    assert api.dc_posts(FLASH) == []  # #flash muted anomalies
    lab2.set("Flash", T4K=6.0)
    lab2.run_for(120)
    assert api.tg_chats("[WARNING] Flash: T4K above 5 K") == [ADMIN, BEN]
    assert api.dc_titles(FLASH) == ["[WARNING] Flash: T4K above 5 K"]
    lab2.set("Ket", pulse_tube=0.0)
    lab2.run_for(300)
    assert not [t for t in api.tg_texts() if "Ket" in t] and api.dc_posts(KET) == []
    lab2.say(EVE, "/status", "Eve")
    assert api.tg_texts(EVE) == [WAITING]  # still not approved


def test_alert_still_firing_when_a_personal_mute_ends_reaches_that_person(tmp_path, clock, api):
    lab = Lab(tmp_path, clock, api)
    lab.approve(BEN, "Ben")
    lab.cool_down()
    lab.say(BEN, "/mute Ket 1h", "Ben")
    api.clear()
    lab.set("Ket", pulse_tube=0.0)
    lab.run_for(120)
    assert api.tg_chats("[CRITICAL] Ket: pulse_tube is not on") == [ADMIN]  # Ben muted Ket
    lab.run_for(3600)  # Ben's mute is over, the pulse tube is still off
    assert BEN in api.tg_chats("Ket: pulse_tube is not on")


# ---------------------------------------------------------------------------
# global mutes: categories and announcements
# ---------------------------------------------------------------------------


def test_global_category_mute_silences_that_alarm_type_everywhere(tmp_path, clock, api):
    lab = Lab(tmp_path, clock, api)
    lab.cool_down()
    lab.store.mute("ANOMALY", None)  # `labwatch mute anomaly`; matching is case-insensitive
    lab.store.mute("Ket/pulse_tube", None)
    api.clear()
    for dev in ("Flash", "Ket"):
        lab.set(dev, cooling_water_in=300.0, pulse_tube=0.0)
    lab.run_for(300)
    texts = [t for t in api.tg_texts() if "muted for everyone" not in t]
    assert [t.split("</b>")[0] for t in texts] == ["<b>[CRITICAL] Flash: pulse_tube is not on"]
    rules = {r.rule_id: r.state for rt in lab.rts.values() for r in rt.rules}
    assert rules["Ket/cooling water unusual"].firing and not rules["Ket/cooling water unusual"].delivered


def test_check_mute_changes_announces_added_changed_removed_and_expired(tmp_path, clock):
    store = StateStore(":memory:")
    mon = Monitor(Config(path=tmp_path / "c.yaml", state_db=tmp_path / "s", history_days=30, rule_defaults={},
                         devices=[], notifiers=[], heartbeat_url=None, heartbeat_interval=60.0,
                         daily_summary=None, notify_startup=False, secrets=set()), [], [], store)
    t0 = clock.t
    store.mute("Flash", t0 + 3600, "warm-up; by Ana")
    assert mon.check_mute_changes() == []  # first look: only remember what's there
    assert mon.check_mute_changes() == []

    store.mute("Ket", t0 + 7200)
    (added,) = mon.check_mute_changes()
    assert (added.kind, added.rule_id, added.device, added.severity, added.category) == (
        EventKind.INFO, "labwatch/mute", "labwatch", Severity.INFO, "labwatch")
    assert is_lifecycle(added)
    assert added.title == "🔕 Ket muted for everyone"
    assert added.message.startswith("Alerts for 'Ket' are muted for everyone until ")
    assert added.message.endswith("\nUndo: /unmuteall Ket")
    assert mon.check_mute_changes() == []

    store.mute("Ket", t0 + 9000, "longer")  # extended
    (changed,) = mon.check_mute_changes()
    assert changed.title == "🔕 Ket muted for everyone" and "(longer)" in changed.message

    store.mute("anomaly", None)
    (forever,) = mon.check_mute_changes()
    assert "muted for everyone until unmuted." in forever.message

    store.unmute("flash")
    (removed,) = mon.check_mute_changes()
    assert removed.title == "🔔 Flash unmuted"
    assert removed.message == "Alerts for 'Flash' are active again."
    assert removed.rule_id == "labwatch/mute" and removed.category == "labwatch"

    clock.t = t0 + 9001
    (expired,) = mon.check_mute_changes()
    assert expired.title == "🔔 Ket unmuted"
    assert expired.message == "Alerts for 'Ket' are active again (mute expired)."
    assert mon._queue.qsize() == 5  # every announcement was emitted


# ---------------------------------------------------------------------------
# command_bots() and the supervisor
# ---------------------------------------------------------------------------


def test_command_bots_one_per_token(tmp_path, clock, api):
    lab = Lab(tmp_path, clock, api, telegram=False)
    t = api.transport
    tg1 = TelegramNotifier("tg", Severity.WARNING, TG_TOKEN, ADMIN, transport=t)
    tg2 = TelegramNotifier("tg-critical", Severity.CRITICAL, TG_TOKEN, "555", transport=t)  # same bot
    tg3 = TelegramNotifier("tg-quiet", Severity.WARNING, "42:no-commands", "777", commands=False, transport=t)
    tg4 = TelegramNotifier("tg-other", Severity.WARNING, "43:another-bot", "888", transport=t)
    dc1 = DiscordBotNotifier("dc", Severity.WARNING, DC_TOKEN, {"Flash": FLASH}, transport=t)
    dc2 = DiscordBotNotifier("dc-again", Severity.WARNING, DC_TOKEN, {"Ket": KET}, transport=t)
    dc3 = DiscordBotNotifier("dc-quiet", Severity.WARNING, "other-token", {"Ket": KET}, commands=False, transport=t)
    lab.monitor.notifiers = [tg1, tg2, tg3, tg4, dc1, dc2, dc3]
    bots = lab.monitor.command_bots()
    try:
        assert [type(b).__name__ for b in bots] == ["TelegramCommandBot", "TelegramCommandBot", "DiscordCommandBot"]
        assert [b.notifier for b in bots[:2]] == [tg1, tg4] and bots[2].n is dc1
        assert tg1.recipients_fn is not None and tg4.recipients_fn is not None and dc1.recipients_fn is not None
        assert tg2.recipients_fn is None and tg3.recipients_fn is None and dc2.recipients_fn is None
        assert dc3.recipients_fn is None
    finally:
        async def close():
            for b in bots:
                await b._client.aclose()

        asyncio.run(close())


def test_supervisor_restarts_a_crashing_bot_and_polling_goes_on(tmp_path, api, monkeypatch, caplog):
    lab = Lab(tmp_path, Clock(_time.time()), api, telegram=False)  # real clock: Monitor.run()
    for name in lab.rts:
        lab.set(name, **COLD)

    class CrashingBot:
        def __init__(self) -> None:
            self.calls = 0
            self.polls_at_crash = 0

        async def run(self) -> None:
            self.calls += 1
            if self.calls <= 2:
                self.polls_at_crash = lab.rts["Flash"].device.polls
                raise RuntimeError("bot bug")
            await asyncio.Event().wait()

    bot = CrashingBot()
    monkeypatch.setattr(lab.monitor, "command_bots", lambda: [bot])
    delays: list[float] = []

    async def fast_sleep(d, *a, **k):
        delays.append(d)
        await _real_sleep(0)

    async def go():
        monkeypatch.setattr(asyncio, "sleep", fast_sleep)
        task = asyncio.create_task(lab.monitor.run())
        try:
            for _ in range(400):
                if bot.calls >= 3 and lab.rts["Flash"].device.polls >= bot.polls_at_crash + 3:
                    break
                await _real_sleep(0.005)
            assert not task.done()
            assert all(not t.done() for t in lab.monitor._device_tasks)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    with caplog.at_level(logging.ERROR):
        asyncio.run(go())
    assert bot.calls >= 3
    assert lab.rts["Flash"].device.polls >= bot.polls_at_crash + 3 and lab.rts["Ket"].device.polls > 3
    assert delays.count(30) >= 2
    assert caplog.text.count("commands:CrashingBot crashed; restarting in 30 s") == 2


# ---------------------------------------------------------------------------
# delivery under a slow Telegram API
# ---------------------------------------------------------------------------


def test_slow_telegram_api_still_reaches_everyone(tmp_path, clock, api, monkeypatch):
    lab = Lab(tmp_path, clock, api)
    people = [str(400 + i) for i in range(5)]
    for i, chat in enumerate(people):
        lab.store.upsert_subscriber(chat, f"user{i}", "user", "warning")
    monkeypatch.setattr(engine, "SEND_TIMEOUT", 0.2)
    monkeypatch.setattr(engine, "RETRY_DELAYS", (0, 0, 0))
    api.tg_delay = 0.05  # each sendMessage takes a while (scaled down)
    event = AlertEvent(EventKind.FIRING, "Ket/pulse_tube is not on", "Ket", "Ket: pulse_tube is not on",
                       "pulse_tube = off", Severity.CRITICAL, category="pulse_tube")
    asyncio.run(lab.monitor.dispatch(event))
    assert set(api.tg_chats()) == {ADMIN, *people}
