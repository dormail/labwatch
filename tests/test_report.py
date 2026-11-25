"""Telegram HTML status reports (labwatch.report) for a real Bluefors poll (with FSE, without turbo
controllers) and a ping device, plus TelegramCommandBot.respond() for /status, /<device> and /alerts."""

from __future__ import annotations

import asyncio
import html
import re
from html.parser import HTMLParser

import httpx
import pytest

from labwatch import engine, report
from labwatch.config import Config, Section
from labwatch.devices.base import parse_channels
from labwatch.devices.bluefors import DEFAULT_CHANNELS, FSE_CHANNELS, BlueforsDevice
from labwatch.engine import Monitor, build_runtime
from labwatch.state import StateStore
from labwatch.telegram_bot import TelegramCommandBot

KEY = "SECRETKEY-report-test-4b5c6d7e"
TOKEN = "123456:report-test-token-abcdef"
HOST = "fridge-pc.lab"
ALL = {**DEFAULT_CHANNELS, **FSE_CHANNELS}

# A cold, circulating Bluefors fridge with an FSE and without Pfeiffer TC400 turbo controllers.
VALUES = {
    "T50K": "45.2", "T4K": "3.31", "Tstill": "0.861", "TMXC": "0.0108", "T_FSE": "3.9",
    "P1": "3.1e-7", "P2": "0.00012", "P3": "0.25", "P4": "2.1", "P5": "181", "P6": "0.52", "flow": "0.61",
    "pulse_tube": "1", "compressor": "1", "cpa_alarm": "0", "cpa_alarm_text": "", "cpa_warning": "1",
    "cpa_warning_text": "Oil temp > 50 C & rising", "cooling_water_in": "290.1", "cooling_water_out": "297.4",
    "cpa_oil": "305.2", "cpa_helium": "310.4", "cpa_high_pressure": "19.1", "cpa_low_pressure": "6.2",
    "cpa_motor_current": "11.6", "status_text": "Circulating <normal>",
    "scroll1": "1", "scroll2": "0", "turbo1": "1", "turbo2": "2", "ghs_compressor": "0",
    "fse_probe_in": "1", "fse_probe_out": "0", "fse_position": "0.415", "fse_probe_mounted": "1",
    "fse_gate_valve": "0", "fse_pump_valve": "1", "fse_vacuum": "1", "fse_status": "0", "fse_motor_error": "0",
}
TURBO2_RULE = {"channel": "turbo2", "equals": 2, "severity": "critical", "auto_arm": False}
RULES = [
    {"channel": "pulse_tube", "not_equals": 1, "severity": "critical"},  # auto-arms later
    {"channel": "tc400_overtemp", "equals": 1, "auto_arm": False},  # armed, but this system has no TC400
]


def node(source: str, value: str) -> dict:
    latest = {"value": value, "outdated": False, "date": 1795636845763, "status": "INDEPENDENT", "exception": ""}
    return {"name": source, "type": "Value", "content": {"latest_value": latest, "latest_valid_value": latest}}


class API:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = {ALL[name].source: v for name, v in values.items()}
        self.override = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.override is not None:
            return self.override(request)
        source = request.url.path.removeprefix("/values/").strip("/").replace("/", ".")
        if source in self.values:
            return httpx.Response(200, json={"data": {source: node(source, self.values[source])}})
        return httpx.Response(404, json={"error": {"code": 3001, "name": "Value not found",
                                                   "description": "Value tree node not found.",
                                                   "query": f"GET {request.url}"}})


def config(tmp_path) -> Config:
    return Config(path=tmp_path / "config.yaml", state_db=tmp_path / "s.sqlite", history_days=30, rule_defaults={},
                  devices=[], notifiers=[], heartbeat_url=None, heartbeat_interval=60.0, daily_summary=None,
                  notify_startup=False, secrets={KEY})


@pytest.fixture
def make(tmp_path, monkeypatch):
    """make(...) -> (monitor, runtimes) after one poll of each device."""

    def build(*specs):
        cfg = config(tmp_path)
        runtimes = []
        for spec in specs:
            sec = Section(dict(spec), set())
            if spec["type"] == "bluefors":
                api = spec["api"]
                defaults = {**DEFAULT_CHANNELS, **(FSE_CHANNELS if spec.get("fse") else {})}
                dev = BlueforsDevice(spec["name"], HOST, KEY, parse_channels(spec["name"], spec, defaults),
                                     transport=httpx.MockTransport(api))
                with monkeypatch.context() as m:
                    m.setattr(engine, "build_device", lambda c, dev=dev: dev)
                    rt = build_runtime(cfg, sec)
            else:
                rt = build_runtime(cfg, sec)
                if spec["type"] == "ping":
                    up = spec.get("up", True)

                    async def tcp(up=up, dev=rt.device):
                        dev.last_error = "" if up else "tcp 3389: ConnectionRefusedError"
                        return 4.2 if up else None

                    rt.device._tcp = tcp
            cfg.devices.append(sec)
            runtimes.append(rt)
        mon = Monitor(cfg, runtimes, [], StateStore(":memory:"))

        async def go():
            for rt in runtimes:
                rt.unreachable.started = engine.time.time()
                await mon.poll_once(rt)
                await rt.device.aclose()

        asyncio.run(go())
        return mon, runtimes

    return build


def flash(fse: bool = True, rules=RULES, values=VALUES, **kw) -> dict:
    return {"name": "Flash", "type": "bluefors", "host": HOST, "api_key": KEY, "fse": fse, "rules": list(rules),
            "api": API(values), **kw}


def plassys(up: bool = True) -> dict:
    return {"name": "Plassys", "type": "ping", "host": "plassys-pc", "method": "tcp", "port": 3389, "up": up,
            "rules": [{"channel": "reachable", "not_equals": 1, "for": "10m", "auto_arm": False}]}


def pre_lines(text: str) -> list[str]:
    m = re.search(r"<pre>(.*)</pre>", text, re.S)
    assert m, text
    return html.unescape(m.group(1)).splitlines()


class TagChecker(HTMLParser):
    """Telegram's HTML mode: only known tags, balanced, everything else escaped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.tags: set[str] = set()

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag, f"unbalanced </{tag}>"


def assert_telegram_html(text: str) -> None:
    p = TagChecker()
    p.feed(text)
    p.close()
    assert p.stack == [] and p.tags <= {"b", "pre"}, (p.stack, p.tags)
    assert len(text) < 4000  # TelegramCommandBot.reply() cuts at 4000 characters


def now_of(rt) -> float:
    return rt.unreachable.last_success + 12


# ---------------------------------------------------------------------------
# device_html for a Bluefors fridge
# ---------------------------------------------------------------------------


def test_device_html_bluefors_with_fse_and_without_tc400(make):
    mon, (rt,) = make(flash(rules=[*RULES, TURBO2_RULE]))
    text = report.device_html(rt, [], now_of(rt))
    assert_telegram_html(text)
    assert text.startswith("<b>Flash</b> · data 12 s ago\n<pre>")
    lines = pre_lines(text)
    headings = [line for line in lines if line and not line.startswith(" ") and ":" not in line]
    assert headings == ["Temperatures", "Pulse tube", "Cooling water", "Circulation", "Pressures", "FSE"]
    for expected in [
        "  T50K               45.2 K",
        "  T4K                3.31 K",
        "  Tstill             0.861 K",
        "  TMXC               10.8 mK",
        "  T FSE              3.9 K",
        "  pulse tube         on",
        "  compressor         running",
        "  cpa alarm text     -",
        "  cpa warning text   Oil temp > 50 C & rising",
        "  cpa motor current  11.6 A",
        "  cooling water in   290.1 K",
        "  cooling water out  297.4 K",
        "  cooling water dT   7.3 K",
        "  flow               0.61 mmol/s",
        "  scroll1            on",
        "  scroll2            off",
        "  turbo2             error",
        "  ghs compressor     off",
        "  P1                 3.100e-07 mbar",
        "  probe in           yes",
        "  probe out          no",
        "  position           0.415 m",
        "  gate valve         closed",
        "  pump valve         open",
        "  status             ok",
        "  motor error        no",
        "status text: Circulating <normal>",
    ]:
        assert expected in lines, expected
    # this system has no TC400/TC4002: those channels are NOT_FOUND and not shown at all
    assert rt.latest["tc400_speed"].status == "NOT_FOUND"
    assert "tc400" not in text and "tc4002" not in text
    # escaped for Telegram's HTML mode
    assert "Oil temp &gt; 50 C &amp; rising" in text and "Circulating &lt;normal&gt;" in text
    # alerts and alarms
    assert "\n🔴 turbo2 is error: turbo2 = error" in text
    assert "\nAlarms: 2/3 armed (the others arm once their value has been normal for a while)" in text


def test_device_html_without_fse_has_no_fse_section(make):
    _, (rt,) = make(flash(fse=False))
    text = report.device_html(rt, [], now_of(rt))
    lines = pre_lines(text)
    assert "FSE" not in lines and not any("FSE" in line for line in lines)
    assert "  probe in           yes" not in lines
    assert "Temperatures" in lines and "  T4K                3.31 K" in lines
    assert "\nAlarms: 1/2 armed" in text


def test_missing_status_text_is_hidden(make):
    values = {k: v for k, v in VALUES.items() if k != "status_text"}
    _, (rt,) = make(flash(values=values))
    assert "status text" not in report.device_html(rt, [], now_of(rt))


def test_empty_status_text_is_hidden(make):
    _, (rt,) = make(flash(values=dict(VALUES, status_text="  ")))
    assert "status text" not in report.device_html(rt, [], now_of(rt))


def test_unreachable_bluefors_report(make):
    def refused(request):
        raise httpx.ConnectError(f"[Errno 111] Connection refused: {request.url}", request=request)

    api = API(VALUES)
    api.override = refused
    _, (rt,) = make(flash(api=api))
    now = engine.time.time()
    text = report.device_html(rt, [], now)
    assert text.startswith("<b>Flash</b> · data never\n⚠️ last poll failed: "
                           "Flash: cannot connect to fridge-pc.lab:49098 (ConnectError)")
    assert "<pre>" not in text
    assert report.device_headline(rt, now) == "Flash: UNREACHABLE (never)"
    assert "🔴 Flash: UNREACHABLE" in report.overview_html([rt], [], now)
    assert KEY not in text + report.overview_html([rt], [], now)


def test_computed_channel_of_missing_inputs_is_hidden_too(make):
    values = {k: v for k, v in VALUES.items() if not ALL[k].source.startswith("driver.cpa.")}
    _, (rt,) = make(flash(values=values))
    lines = pre_lines(report.device_html(rt, [], now_of(rt)))
    assert "Cooling water" not in lines, lines


# ---------------------------------------------------------------------------
# headline / overview / alerts
# ---------------------------------------------------------------------------


def test_device_headline_states(make):
    mon, (ok_rt, alert_rt) = make(flash(), dict(flash(rules=[*RULES, TURBO2_RULE]), name="Ket"))
    assert report.device_headline(ok_rt, now_of(ok_rt)) == "Flash: OK · T4K 3.31 K · TMXC 10.8 mK · pulse tube on (12 s ago)"
    assert report.device_headline(alert_rt, now_of(alert_rt)).startswith("Ket: 1 ALERT · T4K 3.31 K")
    assert report.device_headline(ok_rt, now_of(ok_rt), muted=True).startswith("Flash: OK (muted) · ")
    second = dict(TURBO2_RULE, channel="pulse_tube", equals=1, name="pt on")
    _, (rt,) = make(dict(flash(rules=[TURBO2_RULE, second]), name="Two"))
    assert report.device_headline(rt, now_of(rt)).startswith("Two: 2 ALERTS · ")


def test_ping_device_that_is_down_shows_down(make):
    mon, (rt,) = make(plassys(up=False))
    now = now_of(rt)
    assert rt.latest["reachable"].value == 0 and rt.last_poll_ok is True
    assert report.device_headline(rt, now) == "Plassys: DOWN · reachable down (12 s ago)"
    overview = report.overview_html([rt], [], now)
    assert "🔴 Plassys: DOWN · reachable down (12 s ago)" in overview
    text = report.device_html(rt, [], now)
    assert_telegram_html(text)
    assert pre_lines(text)[:2] == ["Network", "  reachable          down"]


def test_ping_device_that_is_up(make):
    _, (rt,) = make(plassys(up=True))
    assert report.device_headline(rt, now_of(rt)) == "Plassys: OK · reachable up · rtt ms 4.2 ms (12 s ago)"


def test_overview_html(make):
    mon, rts = make(flash(), dict(flash(rules=[TURBO2_RULE]), name="Ket"), plassys(up=False))
    now = max(now_of(rt) for rt in rts)
    text = report.overview_html(rts, [], now)
    assert_telegram_html(text)
    lines = text.splitlines()
    assert lines[0] == "<b>labwatch status</b>"
    assert lines[1].startswith("🟢 Flash: OK · T4K 3.31 K")
    assert lines[2].startswith("🔴 Ket: 1 ALERT · T4K 3.31 K")
    assert lines[3].startswith("🔴 Plassys: DOWN")
    assert lines[-1] == "Details: /flash /ket /plassys"


def test_overview_escapes_device_names(make):
    _, (rt,) = make(dict(flash(), name="Flash <B&C>"))
    text = report.overview_html([rt], [], now_of(rt))
    assert_telegram_html(text)
    assert "Flash &lt;B&amp;C&gt;: OK" in text
    assert text.endswith("Details: /flash__b_c")


def test_mute_is_shown(make):
    mon, (rt,) = make(flash())
    mon.store.mute("Flash", None, "maintenance")
    mutes = mon.store.mutes()
    assert "🟢 Flash: OK (muted)" in report.overview_html([rt], mutes, now_of(rt))
    assert "🔕 muted (Flash)" in report.device_html(rt, mutes, now_of(rt))


def test_alerts_html(make):
    mon, (ok_rt, alert_rt) = make(flash(), dict(flash(rules=[TURBO2_RULE]), name="Ket"))
    assert report.alerts_html([ok_rt]) == "🟢 No active alerts."
    fired = alert_rt.rules[0].state.fired_at
    text = report.alerts_html([ok_rt, alert_rt], fired + 300)
    assert_telegram_html(text)
    assert text == "🔴 <b>Ket</b>: turbo2 is error for 5 min\n   turbo2 = error"


@pytest.mark.parametrize("name, cmd", [("Flash", "flash"), ("Ket 2", "ket_2"), ("Kühl-Schrank", "k_hl_schrank"),
                                       ("--", "device"), ("x" * 40, "x" * 32)])
def test_command_name(name, cmd):
    assert report.command_name(name) == cmd


# ---------------------------------------------------------------------------
# TelegramCommandBot.respond(): /status, /<device>, /alerts
# ---------------------------------------------------------------------------


@pytest.fixture
def bot(make):
    mon, rts = make(flash(rules=[*RULES, TURBO2_RULE]), plassys(up=False))
    b = TelegramCommandBot(mon, TOKEN, {"42"})
    yield b
    asyncio.run(b._client.aclose())


def test_respond_status_overview(bot):
    text = bot.respond("/status")
    assert text.startswith("<b>labwatch status</b>\n🔴 Flash: 1 ALERT · T4K 3.31 K")
    assert "🔴 Plassys: DOWN" in text and text.endswith("Details: /flash /plassys")
    assert bot.respond("/status@labwatch_bot").startswith("<b>labwatch status</b>\n🔴 Flash: 1 ALERT")
    assert bot.respond("/STATUS").startswith("<b>labwatch status</b>")


@pytest.mark.parametrize("text", ["/flash", "/Flash", "/flash@labwatch_bot", "/status flash", "/status Flash"])
def test_respond_device_details(bot, text):
    out = bot.respond(text)
    assert out.startswith("<b>Flash</b> · data ")
    assert "  probe in           yes" in pre_lines(out)
    assert "tc400" not in out
    assert "🔴 turbo2 is error: turbo2 = error" in out


def test_respond_ping_device_details(bot):
    out = bot.respond("/plassys")
    assert out.startswith("<b>Plassys</b> · data ")
    assert "  reachable          down" in pre_lines(out)


def test_respond_status_of_unknown_device(bot):
    assert bot.respond("/status ket").startswith("Unknown device 'ket'.")


def test_respond_alerts(bot):
    out = bot.respond("/alerts")
    assert out.startswith("🔴 <b>Flash</b>: turbo2 is error for ")
    assert "turbo2 = error" in out
    assert TOKEN not in out and KEY not in out
