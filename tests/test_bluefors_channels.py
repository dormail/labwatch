"""Bluefors driver: ValueNotFound (error 3001), the cache of missing paths, and the new default channels."""

from __future__ import annotations

import asyncio
import logging
import traceback

import httpx
import pytest

from labwatch.devices import DeviceError, DeviceUnreachable, build_device
from labwatch.devices import bluefors
from labwatch.devices.bluefors import (
    DEFAULT_CHANNELS,
    FSE_CHANNELS,
    ON_OFF_ERROR,
    STATUS_SECTIONS,
    BlueforsDevice,
    ValueNotFound,
)

KEY = "SECRETKEY-cache-test-0d1e2f3a"
HOST = "fridge-pc.lab"
T50K = "mapper.bf.temperatures.t50k"
T4K = "mapper.bf.temperatures.t4k"
TC = "driver.tc400.active_rotational_speed"
DATE_MS = 1795636845763


def node(source: str, value: str) -> dict:
    latest = {"value": value, "outdated": False, "date": DATE_MS, "status": "INDEPENDENT", "exception": ""}
    return {"name": source, "type": "Value.Number.Float", "content": {"latest_valid_value": latest,
                                                                      "latest_value": latest}}


class API:
    """Fake CS 2.x value API that counts requests per value path."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.requests: list[str] = []
        self.down: set[str] = set()  # paths whose request fails at the connection level
        self.broken: set[str] = set()  # paths that answer HTTP 500

    def __call__(self, request: httpx.Request) -> httpx.Response:
        source = request.url.path.removeprefix("/values/").strip("/").replace("/", ".")
        self.requests.append(source)
        assert request.url.params["key"] == KEY
        if source in self.down:
            raise httpx.ConnectError(f"[Errno 111] Connection refused: {request.url}", request=request)
        if source in self.broken:
            return httpx.Response(500, text=f"<html>error at {request.url}</html>")
        if source in self.values:
            return httpx.Response(200, json={"data": {source: node(source, self.values[source])}})
        return httpx.Response(404, json={"error": {
            "code": 3001, "name": "Value not found", "description": "Value tree node not found.",
            "query": f"GET {request.url}", "data": {"target": source}}})

    def count(self, source: str) -> int:
        return self.requests.count(source)


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def time(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    c = FakeClock()
    monkeypatch.setattr(bluefors, "time", c)
    return c


def device(api: API, names=("T50K", "tc400_speed")) -> BlueforsDevice:
    channels = {n: DEFAULT_CHANNELS[n] for n in names}
    return BlueforsDevice("Flash", HOST, KEY, channels, transport=httpx.MockTransport(api))


def by_channel(readings):
    return {r.channel: r for r in readings}


def run(coro_fn):
    asyncio.run(coro_fn())


# ---------------------------------------------------------------------------
# ValueNotFound
# ---------------------------------------------------------------------------


def test_error_3001_is_value_not_found_without_key():
    dev = device(API())

    async def go():
        with pytest.raises(ValueNotFound) as ei:
            await dev._get("/values/driver/tc400/active_rotational_speed", recursion=0)
        await dev.aclose()
        return ei.value

    e = asyncio.run(go())
    assert isinstance(e, DeviceError) and not isinstance(e, DeviceUnreachable)
    assert str(e) == ("Flash: /values/driver/tc400/active_rotational_speed: 3001 Value not found: "
                      "Value tree node not found.")
    assert KEY not in "".join(traceback.format_exception(e)) + repr(e)


# ---------------------------------------------------------------------------
# the cache of missing paths
# ---------------------------------------------------------------------------


def test_missing_path_is_not_requested_again_for_30_min(clock):
    api = API({T50K: "45.1"})
    dev = device(api)
    start = clock.t

    async def go():
        r = by_channel(await dev.poll())
        assert r["T50K"].value == pytest.approx(45.1)
        assert (r["tc400_speed"].value, r["tc400_speed"].status) == (None, "NOT_FOUND")
        assert api.count(TC) == 1
        for minutes in (0.5, 10, 29.9):
            clock.t = start + minutes * 60
            r = by_channel(await dev.poll())
            assert r["tc400_speed"].status == "NOT_FOUND"
            assert r["tc400_speed"].source == TC
        assert api.count(TC) == 1  # served from the cache
        assert api.count(T50K) == 4  # everything else is still read every poll
        clock.t = start + 1800  # 30 min: probe again (still missing)
        await dev.poll()
        assert api.count(TC) == 2
        clock.t = start + 1800 + 600
        await dev.poll()
        assert api.count(TC) == 2  # cached for another 30 min
        clock.t = start + 3600
        await dev.poll()
        assert api.count(TC) == 3
        await dev.aclose()

    run(go)


def test_path_that_appears_later_is_read(clock):
    api = API({T50K: "45.1"})
    dev = device(api)
    start = clock.t

    async def go():
        await dev.poll()
        api.values[TC] = "820"  # e.g. the turbo controller driver was enabled
        clock.t = start + 600
        assert by_channel(await dev.poll())["tc400_speed"].status == "NOT_FOUND"
        assert api.count(TC) == 1
        clock.t = start + 1800
        r = by_channel(await dev.poll())["tc400_speed"]
        assert (r.value, r.status, r.unit) == (820.0, "INDEPENDENT", "Hz")
        for k in range(1, 4):  # read on every poll from now on
            clock.t = start + 1800 + 30 * k
            assert by_channel(await dev.poll())["tc400_speed"].value == 820.0
        assert api.count(TC) == 5
        # ... and if it disappears again, it's cached again
        del api.values[TC]
        clock.t = start + 2000
        await dev.poll()
        clock.t = start + 2100
        await dev.poll()
        assert api.count(TC) == 6
        await dev.aclose()

    run(go)


def test_poll_where_nothing_is_found_raises_and_caches_nothing(clock):
    api = API()  # value tree empty: control software still starting
    dev = device(api)

    async def go():
        with pytest.raises(DeviceError) as ei:
            await dev.poll()
        assert not isinstance(ei.value, DeviceUnreachable)
        assert str(ei.value) == "Flash: none of the 2 channels could be read (control software not ready?)"
        clock.t += 30
        with pytest.raises(DeviceError):
            await dev.poll()
        assert (api.count(T50K), api.count(TC)) == (2, 2)  # nothing was cached
        api.values.update({T50K: "45.1", TC: "820"})
        clock.t += 30
        r = by_channel(await dev.poll())
        assert r["T50K"].value == pytest.approx(45.1) and r["tc400_speed"].value == 820.0
        await dev.aclose()

    run(go)


def test_value_tree_gone_after_a_good_poll_caches_nothing_new(clock):
    api = API({T50K: "45.1", T4K: "3.2"})
    dev = device(api, ("T50K", "T4K", "tc400_speed"))

    async def go():
        await dev.poll()  # tc400 cached as missing
        saved, api.values = api.values, {}  # control software restarting: every path is 3001
        clock.t += 30
        with pytest.raises(DeviceError, match="none of the 3 channels"):
            await dev.poll()
        api.values = saved
        clock.t += 30
        r = by_channel(await dev.poll())
        assert r["T50K"].value == pytest.approx(45.1) and r["T4K"].value == pytest.approx(3.2)
        assert (api.count(T50K), api.count(T4K), api.count(TC)) == (3, 3, 1)
        await dev.aclose()

    run(go)


def test_failed_poll_caches_nothing(clock):
    api = API({T50K: "45.1"})
    api.down = {T4K}
    dev = device(api, ("T50K", "T4K", "tc400_speed"))

    async def go():
        with pytest.raises(DeviceUnreachable):
            await dev.poll()
        api.down = set()
        api.values[T4K] = "3.2"
        clock.t += 30
        r = by_channel(await dev.poll())
        assert r["T4K"].value == pytest.approx(3.2)
        assert api.count(TC) == 2  # the failed poll didn't cache it
        await dev.aclose()

    run(go)


def test_other_per_channel_errors_are_not_cached(clock):
    api = API({T50K: "45.1"})
    api.broken = {TC}
    dev = device(api)

    async def go():
        for _ in range(3):
            assert by_channel(await dev.poll())["tc400_speed"].status == "NOT_FOUND"
            clock.t += 30
        assert api.count(TC) == 3
        await dev.aclose()

    run(go)


def test_missing_channel_is_logged_once_including_reprobes(clock, caplog):
    caplog.set_level(logging.INFO, logger="labwatch.devices.bluefors")
    api = API({T50K: "45.1"})
    dev = device(api)
    start = clock.t

    async def go():
        for minutes in (0, 10, 30, 45, 60, 90):
            clock.t = start + minutes * 60
            await dev.poll()
        await dev.aclose()

    run(go)
    msgs = [r.getMessage() for r in caplog.records if r.name == "labwatch.devices.bluefors"]
    assert len(msgs) == 1
    assert msgs[0].startswith("Flash: channel tc400_speed (driver.tc400.active_rotational_speed) not available")
    assert caplog.records[0].levelno == logging.INFO
    assert KEY not in msgs[0]
    assert api.count(TC) == 4  # 0, 30, 60, 90 min


def test_computed_channels_are_not_requested_from_the_api(clock):
    names = ("cooling_water_in", "cooling_water_out", "cooling_water_dT")
    api = API({"driver.cpa.coolant_in_temperature": "290.1", "driver.cpa.coolant_out_temperature": "297.4"})
    dev = device(api, names)

    async def go():
        readings = await dev.poll()
        await dev.aclose()
        return readings

    readings = asyncio.run(go())
    assert [r.channel for r in readings] == ["cooling_water_in", "cooling_water_out"]  # the engine adds dT
    assert sorted(api.requests) == ["driver.cpa.coolant_in_temperature", "driver.cpa.coolant_out_temperature"]


# ---------------------------------------------------------------------------
# default channels, FSE, status sections
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, source, unit, labels",
    [
        ("cpa_motor_current", "driver.cpa.motor_current", "A", ()),
        ("scroll1", "mapper.bf.pumps.scroll1", None, ON_OFF_ERROR),
        ("scroll2", "mapper.bf.pumps.scroll2", None, ON_OFF_ERROR),
        ("turbo1", "mapper.bf.pumps.turbo1", None, ON_OFF_ERROR),
        ("turbo2", "mapper.bf.pumps.turbo2", None, ON_OFF_ERROR),
        ("ghs_compressor", "mapper.bf.pumps.compressor", None, ON_OFF_ERROR),
        ("tc400_speed", "driver.tc400.active_rotational_speed", "Hz", ()),
        ("tc400_power", "driver.tc400.drive_power", "W", ()),
        ("tc400_at_speed", "driver.tc400.setting_speed_attained", None, ((0, "no"), (1, "yes"))),
        ("tc400_overtemp", "driver.tc400.pump_temperature_too_high", None, ((0, "no"), (1, "YES"))),
        ("tc400_driver_overtemp", "driver.tc400.driver_temperature_too_high", None, ((0, "no"), (1, "YES"))),
        ("tc4002_speed", "driver.tc4002.active_rotational_speed", "Hz", ()),
        ("tc4002_power", "driver.tc4002.drive_power", "W", ()),
        ("tc4002_at_speed", "driver.tc4002.setting_speed_attained", None, ((0, "no"), (1, "yes"))),
        ("tc4002_overtemp", "driver.tc4002.pump_temperature_too_high", None, ((0, "no"), (1, "YES"))),
        ("tc4002_driver_overtemp", "driver.tc4002.driver_temperature_too_high", None, ((0, "no"), (1, "YES"))),
    ],
)
def test_new_default_channels(name, source, unit, labels):
    spec = DEFAULT_CHANNELS[name]
    assert (spec.source, spec.unit, spec.labels, spec.computed) == (source, unit, labels, False)


def test_default_channel_names_and_sources_are_unique():
    assert len({s.source for s in DEFAULT_CHANNELS.values()}) == len(DEFAULT_CHANNELS)
    assert not set(DEFAULT_CHANNELS) & set(FSE_CHANNELS)
    assert all(name == spec.name for name, spec in {**DEFAULT_CHANNELS, **FSE_CHANNELS}.items())
    assert [n for n, s in DEFAULT_CHANNELS.items() if s.computed] == ["cooling_water_dT"]


def test_status_sections_only_name_known_channels():
    known = set(DEFAULT_CHANNELS) | set(FSE_CHANNELS)
    for title, channels in STATUS_SECTIONS:
        assert set(channels) <= known, title
    shown = [c for _, chans in STATUS_SECTIONS for c in chans]
    assert len(shown) == len(set(shown))
    assert set(FSE_CHANNELS) <= set(shown)


def build(**cfg) -> BlueforsDevice:
    return build_device({"name": "Flash", "type": "bluefors", "host": HOST, "api_key": KEY, **cfg})


def test_fse_channels_only_with_fse_true():
    plain, fse = build(), build(fse=True)
    try:
        assert not set(FSE_CHANNELS) & set(plain.channels)
        assert set(FSE_CHANNELS) <= set(fse.channels)
        assert set(DEFAULT_CHANNELS) <= set(fse.channels)
        assert fse.channels["fse_gate_valve"].labels == ((0, "closed"), (1, "open"))
        assert fse.channels["T_FSE"].unit == "K"
        sections = dict(fse.status_sections)
        assert "T_FSE" in sections["Temperatures"]
        assert sections["FSE"] == tuple(n for n in FSE_CHANNELS if n != "T_FSE")
        assert dict(plain.status_sections)["FSE"] == ()
        assert "T_FSE" not in dict(plain.status_sections)["Temperatures"]
    finally:
        asyncio.run(plain.aclose())
        asyncio.run(fse.aclose())


def test_status_sections_follow_the_configured_channels():
    dev = build(exclude_channels=["tc400_speed", "tc400_power", "cooling_water_in"],
                channels={"still_heater": {"path": "mapper.heater_mappings_bftc.device.c.power", "unit": "W"}})
    try:
        sections = dict(dev.status_sections)
        assert "tc400_speed" not in sections["Circulation"] and "tc4002_speed" in sections["Circulation"]
        assert sections["Cooling water"] == ("cooling_water_out",)  # dT dropped with its input
        assert "cooling_water_dT" not in dev.channels
        assert [t for t, _ in dev.status_sections] == [t for t, _ in STATUS_SECTIONS]
    finally:
        asyncio.run(dev.aclose())


def test_fse_channels_can_be_excluded():
    dev = build(fse=True, exclude_channels=["fse_motor_error"])
    try:
        assert "fse_motor_error" not in dev.channels and "fse_status" in dev.channels
    finally:
        asyncio.run(dev.aclose())
