"""Bluefors Control Software HTTP API driver, against an httpx.MockTransport speaking the real format."""

from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import datetime, timezone

import httpx
import pytest

from labwatch import engine
from labwatch.config import Config, Section
from labwatch.devices import ChannelSpec, DeviceConfigError, DeviceError, DeviceUnreachable, build_device
from labwatch.devices.bluefors import DEFAULT_CHANNELS, BlueforsDevice, node_to_reading
from labwatch.engine import Monitor, build_runtime
from labwatch.models import AlertEvent, EventKind, Severity
from labwatch.notifiers.base import Notifier
from labwatch.state import StateStore

KEY = "SECRETKEY-not-a-real-key-7f3a9c"
HOST = "fridge-pc.lab"
DATE_MS = 1795636845763  # from the real example node
CHANNELS = {k: DEFAULT_CHANNELS[k] for k in ("T50K", "TMXC", "pulse_tube", "cpa_alarm_text", "P1")}


def node(source: str, value: str, status: str = "INDEPENDENT", date: int = DATE_MS, outdated: bool = False,
         type_: str = "Value.Number.Float.Unit.temperature") -> dict:
    latest = {"value": value, "outdated": outdated, "date": date, "status": status, "exception": ""}
    return {
        "name": source,
        "type": type_,
        "content": {
            "read_only": True, "maximum_age": 900000, "lockable": False, "locked": False,
            "owner": "mapper.temperature_control.sensors.t1.temperature",
            "latest_valid_value": latest, "latest_value": latest,
        },
    }


def api_error(request: httpx.Request, code: int, name: str, description: str, data=None) -> dict:
    # The real API echoes the whole query - including ?key=... - in error bodies.
    return {"error": {"code": code, "name": name, "description": description,
                      "query": f"GET {request.url}", "query_data": "", "data": data}}


class FakeAPI:
    """Minimal Bluefors CS 2.x API: GET /system/ and GET /values/<path>[?recursion=N]."""

    def __init__(self, values: dict[str, dict] | None = None, clock=None) -> None:
        self.values = values or {}  # source -> kwargs for node()
        self.clock = clock  # if set, value dates follow this fake clock
        self.requests: list[httpx.Request] = []
        self.override = None  # callable(request) -> Response, or raises

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.override is not None:
            return self.override(request)
        if request.url.params.get("key") != KEY:
            return httpx.Response(403, json=api_error(request, 2003, "Access denied",
                                                      "Supplied key has no rights to the content"))
        path = request.url.path
        if path == "/system/":
            return httpx.Response(200, json={"data": {"system_name": "Flash", "sw_version": "2.6.1"}})
        source = path.removeprefix("/values/").strip("/").replace("/", ".")
        if source in self.values:
            kw = dict(self.values[source])
            if self.clock is not None:
                kw.setdefault("date", int(self.clock.t * 1000))
            return httpx.Response(200, json={"data": {source: node(source, **kw)}})
        return httpx.Response(404, json=api_error(request, 3001, "Value not found", "Value tree node not found.",
                                                  {"target": source}))


def device(api: FakeAPI, channels=None, key: str = KEY) -> BlueforsDevice:
    return BlueforsDevice("Flash", HOST, key, dict(channels or CHANNELS), transport=httpx.MockTransport(api))


def run(dev: BlueforsDevice, method: str = "poll", *args):
    async def go():
        try:
            return await getattr(dev, method)(*args)
        finally:
            await dev.aclose()

    return asyncio.run(go())


def raised(dev: BlueforsDevice, method: str = "poll", *args) -> DeviceError:
    with pytest.raises(DeviceError) as ei:
        run(dev, method, *args)
    return ei.value


def assert_no_key(exc: BaseException) -> None:
    text = "\n".join([str(exc), repr(exc), "".join(traceback.format_exception(exc))])
    assert KEY not in text


GOOD_VALUES = {
    "mapper.bf.temperatures.t50k": {"value": "269.096969"},
    "mapper.bf.temperatures.tmixing": {"value": "0.0123", "status": "SYNCHRONIZED"},
    "mapper.bf.pulsetube": {"value": "1", "type_": "Value.Number.Integer.Enumeration.onOffError"},
    "driver.cpa.alarm_state_text": {"value": "Helium temperature high", "type_": "Value.String"},
    "mapper.bf.pressures.p1": {"value": "3.2e-7", "outdated": True},
}


# ---------------------------------------------------------------------------
# value parsing
# ---------------------------------------------------------------------------


def test_poll_parses_real_api_nodes():
    api = FakeAPI(GOOD_VALUES)
    readings = {r.channel: r for r in run(device(api))}
    assert list(readings) == ["T50K", "TMXC", "pulse_tube", "cpa_alarm_text", "P1"]

    t50k = readings["T50K"]
    assert t50k.value == pytest.approx(269.096969)
    assert (t50k.unit, t50k.raw, t50k.status, t50k.source) == ("K", "269.096969", "INDEPENDENT",
                                                                "mapper.bf.temperatures.t50k")
    assert t50k.timestamp == datetime.fromtimestamp(DATE_MS / 1000, timezone.utc)
    assert t50k.format_value() == "269.1 K"

    assert readings["TMXC"].value == pytest.approx(12.3)  # K -> mK via scale
    assert readings["TMXC"].unit == "mK"
    assert (readings["pulse_tube"].value, readings["pulse_tube"].label) == (1.0, "on")
    alarm = readings["cpa_alarm_text"]
    assert alarm.value is None and alarm.format_value() == "Helium temperature high"
    assert readings["P1"].value == pytest.approx(3.2e-7) and readings["P1"].outdated


def test_poll_requests_each_channel_with_key_and_recursion_0():
    api = FakeAPI(GOOD_VALUES)
    run(device(api))
    paths = sorted(r.url.path for r in api.requests)
    assert "/values/mapper/bf/temperatures/t50k" in paths and "/values/driver/cpa/alarm_state_text" in paths
    for r in api.requests:
        assert r.url.scheme == "https" and r.url.host == HOST and r.url.port == 49098
        assert r.url.params["key"] == KEY and r.url.params["recursion"] == "0"


@pytest.mark.parametrize(
    "value, status, expected",
    [
        ("269.096969", "INDEPENDENT", 269.096969),
        ("4.2", "SYNCHRONIZED", 4.2),
        ("4.2", "CHANGED", 4.2),
        ("", "INDEPENDENT", None),  # invalid values come back as ""
        ("  ", "INDEPENDENT", None),
        ("4.2", "INVALID", None),  # e.g. RuOx out of calibration range
        ("4.2", "ERROR", None),
        ("4.2", "DISCONNECTED", None),
        ("nan", "INDEPENDENT", None),
        ("true", "INDEPENDENT", 1.0),
        ("False", "INDEPENDENT", 0.0),
        ("Helium temperature high", "INDEPENDENT", None),
    ],
)
def test_node_value_parsing(value, status, expected):
    r = node_to_reading("Flash", DEFAULT_CHANNELS["T4K"], node("mapper.bf.temperatures.t4k", value, status))
    assert r.value == (None if expected is None else pytest.approx(expected))
    assert r.raw == value and r.status == status


@pytest.mark.parametrize("raw, label", [("0", "off"), ("1", "on"), ("2", "error"), ("1.5", None), ("", None)])
def test_pulse_tube_enum_labels(raw, label):
    r = node_to_reading("Flash", DEFAULT_CHANNELS["pulse_tube"], node("mapper.bf.pulsetube", raw))
    assert r.label == label
    if label:
        assert r.format_value() == label


def test_compressor_running_labels():
    r = node_to_reading("Flash", DEFAULT_CHANNELS["compressor"], node("driver.cpa.compressor_running_status", "0"))
    assert (r.value, r.label) == (0.0, "stopped")


def test_tmxc_scale_applies_only_to_valid_values():
    spec = DEFAULT_CHANNELS["TMXC"]
    assert node_to_reading("Flash", spec, node("x", "0.0105")).value == pytest.approx(10.5)
    assert node_to_reading("Flash", spec, node("x", "", "INVALID")).value is None


@pytest.mark.parametrize("n", [None, {}, {"name": "x"}, {"content": None}])
def test_missing_or_malformed_node_is_not_found(n):
    r = node_to_reading("Flash", DEFAULT_CHANNELS["T4K"], n)
    assert (r.value, r.timestamp, r.status) == (None, None, "NOT_FOUND")
    assert r.format_value() == "invalid (NOT_FOUND)"


def test_bad_date_gives_no_timestamp():
    r = node_to_reading("Flash", DEFAULT_CHANNELS["T4K"], node("x", "4.2", date="yesterday"))
    assert r.value == 4.2 and r.timestamp is None


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


def test_value_not_found_gives_invalid_reading_and_poll_succeeds(caplog):
    values = dict(GOOD_VALUES)
    del values["driver.cpa.alarm_state_text"]  # e.g. a fridge without a CPA compressor driver
    caplog.set_level(logging.INFO, logger="labwatch")  # a missing feature is normal: INFO, not WARNING
    dev = device(FakeAPI(values))
    readings = {r.channel: r for r in run(dev)}
    missing = readings["cpa_alarm_text"]
    assert (missing.value, missing.status, missing.source) == (None, "NOT_FOUND", "driver.cpa.alarm_state_text")
    assert readings["T50K"].value == pytest.approx(269.096969)
    msgs = [r.getMessage() for r in caplog.records if r.name.startswith("labwatch")]
    assert any("3001 Value not found" in m and "cpa_alarm_text" in m for m in msgs)
    assert not any(KEY in m for m in msgs)


def test_value_not_found_is_logged_only_once(caplog):
    caplog.set_level(logging.INFO, logger="labwatch.devices.bluefors")
    # One good channel keeps the poll successful; the missing one is logged once.
    dev = device(FakeAPI(GOOD_VALUES), {"T50K": DEFAULT_CHANNELS["T50K"],
                                        "bogus": ChannelSpec("bogus", "mapper.bf.does_not_exist")})

    async def go():
        for _ in range(3):
            await dev.poll()
        await dev.aclose()

    asyncio.run(go())
    assert len([r for r in caplog.records if r.name == "labwatch.devices.bluefors"]) == 1


def test_api_key_rejected_is_device_error_without_key():
    api = FakeAPI(GOOD_VALUES)
    api.override = lambda request: httpx.Response(403, json=api_error(
        request, 2003, "Access denied", "Supplied key has no rights to the content"))
    e = raised(device(api))
    assert not isinstance(e, DeviceUnreachable)
    assert str(e) == "Flash: API key rejected (2003 Access denied: Supplied key has no rights to the content)"
    assert KEY in api.requests[0].url.params["key"]  # (the mock really did echo the key)
    assert_no_key(e)


def test_wrong_key_rejected():
    e = raised(device(FakeAPI(GOOD_VALUES), key="WRONG-KEY-123456"))
    assert "API key rejected" in str(e)
    assert "WRONG-KEY-123456" not in str(e) + repr(e)


def connect_error(request):
    raise httpx.ConnectError(f"[Errno 111] Connection refused: {request.url}", request=request)


def connect_timeout(request):
    raise httpx.ConnectTimeout(f"timed out: {request.url}", request=request)


def read_timeout(request):
    raise httpx.ReadTimeout(f"timed out: {request.url}", request=request)


@pytest.mark.parametrize(
    "handler, message",
    [
        (connect_error, "Flash: cannot connect to fridge-pc.lab:49098 (ConnectError)"),
        (connect_timeout, "Flash: timeout talking to fridge-pc.lab:49098"),
        (read_timeout, "Flash: timeout talking to fridge-pc.lab:49098"),
    ],
)
def test_transport_errors_are_unreachable_without_key(handler, message):
    api = FakeAPI(GOOD_VALUES)
    api.override = handler
    e = raised(device(api))
    assert isinstance(e, DeviceUnreachable)
    assert str(e) == message
    assert_no_key(e)


def html_500(request):
    return httpx.Response(500, text=f"<html>Internal error while handling {request.url}</html>")


def json_404(request):
    return httpx.Response(404, json=api_error(request, 3001, "Value not found", "Value tree node not found.",
                                              {"target": "mapper.bf.nonexistent"}))


def no_data(request):
    return httpx.Response(200, json={"query": f"GET {request.url}"})


def data_not_a_dict(request):
    return httpx.Response(200, json={"data": f"GET {request.url}"})


@pytest.mark.parametrize(
    "handler, method, message",
    [
        (html_500, "describe", "Flash: /system/: HTTP 500"),
        (json_404, "describe", "Flash: /system/: 3001 Value not found: Value tree node not found."),
        (no_data, "describe", "Flash: /system/: HTTP 200"),
        (json_404, "discover", "Flash: /values/mapper/bf: 3001 Value not found: Value tree node not found."),
        (data_not_a_dict, "poll", "Flash: unexpected error: AttributeError"),
    ],
)
def test_error_messages_never_contain_key(handler, method, message):
    api = FakeAPI(GOOD_VALUES)
    api.override = handler
    args = ("mapper.bf",) if method == "discover" else ()
    e = raised(device(api), method, *args)
    assert str(e) == message
    assert_no_key(e)


def test_per_channel_http_errors_give_invalid_readings():
    api = FakeAPI(GOOD_VALUES)

    def flaky(request):
        if request.url.path.endswith("/t50k"):
            return html_500(request)
        api.override = None
        try:
            return api(request)
        finally:
            api.override = flaky

    api.override = flaky
    readings = {r.channel: r for r in run(device(api))}
    assert readings["T50K"].status == "NOT_FOUND"
    assert readings["pulse_tube"].label == "on"


def test_poll_fails_when_no_channel_could_be_read():
    api = FakeAPI(GOOD_VALUES)
    api.override = html_500
    with pytest.raises(DeviceError):
        run(device(api))


def test_repr_and_config_never_show_key():
    dev = device(FakeAPI())
    assert repr(dev) == "<BlueforsDevice 'Flash' fridge-pc.lab:49098>"
    assert KEY not in repr(dev) + repr(dev._client)
    asyncio.run(dev.aclose())


# ---------------------------------------------------------------------------
# describe / discover
# ---------------------------------------------------------------------------


def test_describe_returns_system_info():
    assert run(device(FakeAPI()), "describe") == {"system_name": "Flash", "sw_version": "2.6.1"}


def test_discover_lists_values_below_prefix():
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    def subtree(request):
        assert request.url.path == "/values/mapper/bf"
        assert "recursion" not in request.url.params  # whole subtree
        assert request.url.params["key"] == KEY
        return httpx.Response(200, json={"data": {
            "mapper.bf.temperatures.t50k": node("mapper.bf.temperatures.t50k", "45.1", date=now_ms - 10_000),
            "mapper.bf.pulsetube": node("mapper.bf.pulsetube", "1", type_="Value.Number.Integer.Enumeration"),
            "mapper.bf.reset": dict(node("mapper.bf.reset", ""), type="Method"),
            "mapper.bf.temperatures": {"name": "mapper.bf.temperatures", "type": "Folder"},
            "mapper.bf.broken": "not a node",
        }})

    api = FakeAPI()
    api.override = subtree
    found = run(device(api), "discover", "mapper.bf.")
    assert [v.source for v in found] == ["mapper.bf.pulsetube", "mapper.bf.temperatures.t50k"]
    pt, t50k = found
    assert (pt.type, pt.raw, pt.status) == ("Value.Number.Integer.Enumeration", "1", "INDEPENDENT")
    assert 9 < t50k.age < 60


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_from_config_requires_host_and_key():
    with pytest.raises(DeviceConfigError, match="'host' is required"):
        build_device({"name": "Flash", "type": "bluefors", "api_key": KEY})
    with pytest.raises(DeviceConfigError, match="'api_key' is required"):
        build_device({"name": "Flash", "type": "bluefors", "host": HOST, "api_key": ""})


def test_from_config_channels_and_options():
    dev = build_device({
        "name": "Flash", "type": "bluefors", "host": HOST, "api_key": KEY, "port": "49100",
        "poll_interval": "1m", "unreachable_after": "5m", "verify_tls": "false",
        "channels": {"still_heater": {"path": "mapper.heater_mappings_bftc.device.c.power", "unit": "W"},
                     "T4K": None},
        "exclude_channels": ["P6", "status_text"],
    })
    try:
        assert (dev.port, dev.poll_interval, dev.unreachable_after) == (49100, 60, 300)
        assert dev.channels["still_heater"].unit == "W"
        assert not {"T4K", "P6", "status_text"} & set(dev.channels)
        assert "T50K" in dev.channels and "pulse_tube" in dev.channels
        assert KEY not in repr(dev)
    finally:
        asyncio.run(dev.aclose())


def test_from_config_without_default_channels():
    dev = build_device({"name": "Flash", "type": "bluefors", "host": HOST, "api_key": KEY,
                        "default_channels": False, "channels": {"T4K": "mapper.bf.temperatures.t4k"}})
    assert list(dev.channels) == ["T4K"]
    asyncio.run(dev.aclose())
    with pytest.raises(DeviceConfigError, match="no channels"):
        build_device({"name": "Flash", "type": "bluefors", "host": HOST, "api_key": KEY, "default_channels": False})


# ---------------------------------------------------------------------------
# the whole chain: Bluefors API -> engine -> notifier
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def time(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t


class _Inbox(Notifier):
    def __init__(self) -> None:
        super().__init__("inbox", Severity.INFO)
        self.events: list[AlertEvent] = []

    async def send(self, event: AlertEvent) -> None:
        self.events.append(event)

    @classmethod
    def from_config(cls, name, min_severity, cfg):
        return cls()


def test_power_outage_end_to_end(tmp_path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(engine, "time", clock)
    api = FakeAPI({
        "mapper.bf.temperatures.t50k": {"value": "45.2"},
        "mapper.bf.temperatures.t4k": {"value": "3.21"},
        "mapper.bf.pulsetube": {"value": "1"},
        "driver.cpa.compressor_running_status": {"value": "1"},
    }, clock=clock)
    rules = [
        {"channel": "pulse_tube", "not_equals": 1, "severity": "critical",
         "message": "Pulse tube is not running! Check power, compressor, cooling water."},
        {"channel": "compressor", "not_equals": 1, "severity": "critical"},
        {"channel": "T4K", "above": 5, "hysteresis": 0.3, "severity": "critical"},
    ]
    cfg = {"name": "Flash", "type": "bluefors", "host": HOST, "api_key": KEY, "rules": rules}
    channels = {k: DEFAULT_CHANNELS[k] for k in ("T50K", "T4K", "pulse_tube", "compressor")}
    dev = BlueforsDevice("Flash", HOST, KEY, channels, transport=httpx.MockTransport(api))
    monkeypatch.setattr(engine, "build_device", lambda c: dev)  # build_runtime() wires rules as usual
    config = Config(path=tmp_path / "c.yaml", state_db=tmp_path / "s.sqlite", history_days=30,
                    rule_defaults={"for": 60, "clear_for": 120, "arm_after": "10m", "repeat_interval": "2h"},
                    devices=[Section(cfg, set())], notifiers=[], heartbeat_url=None, heartbeat_interval=60,
                    daily_summary=None, notify_startup=False, secrets={KEY})
    rt = build_runtime(config, config.devices[0])
    inbox = _Inbox()
    mon = Monitor(config, [rt], [inbox], StateStore(":memory:"))
    rt.unreachable.started = clock.t

    async def poll_for(seconds: float) -> list[AlertEvent]:
        n = len(inbox.events)
        for _ in range(int(seconds // 30)):
            clock.t += 30
            await mon.poll_once(rt)
            while not mon._queue.empty():
                await mon.dispatch(mon._queue.get_nowait())
        return inbox.events[n:]

    async def scenario():
        # cold fridge: alarms arm after 10 minutes
        events = await poll_for(660)
        assert [e.title for e in events] == ["Flash: alarms armed"]

        # power outage: compressor and pulse tube stop, the control PC (on a UPS) keeps answering
        api.values["mapper.bf.pulsetube"] = {"value": "0"}
        api.values["driver.cpa.compressor_running_status"] = {"value": "0"}
        events = await poll_for(90)
        assert sorted((e.kind, e.title) for e in events) == [
            (EventKind.FIRING, "Flash: compressor is not running"),
            (EventKind.FIRING, "Flash: pulse_tube is not on"),
        ]
        pt = next(e for e in events if "pulse_tube" in e.title)
        assert pt.severity is Severity.CRITICAL
        assert "pulse_tube = off" in pt.message and "Check power, compressor" in pt.message
        assert "T4K 3.21 K" in pt.message and "compressor stopped" in pt.message

        # the plates warm up
        api.values["mapper.bf.temperatures.t4k"] = {"value": "5.6"}
        events = await poll_for(90)
        assert [(e.kind, e.title) for e in events] == [(EventKind.FIRING, "Flash: T4K above 5 K")]

        # then the UPS runs out: the API goes away
        api.override = connect_error
        events = await poll_for(420)
        assert [(e.kind, e.title, e.severity) for e in events] == [
            (EventKind.FIRING, "Flash: unreachable", Severity.CRITICAL)]
        assert "cannot connect to fridge-pc.lab:49098 (ConnectError)" in events[0].message
        await dev.aclose()

    asyncio.run(scenario())
    for e in inbox.events:
        assert KEY not in e.title + e.message
