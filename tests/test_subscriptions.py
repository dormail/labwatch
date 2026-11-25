"""Who gets which alert: mute targets, per-subscriber levels and personal mutes (labwatch.subscriptions,
labwatch.state), and alarm categories from the device (engine.build_runtime + Device.category_for)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from labwatch import subscriptions
from labwatch.config import Config, Section
from labwatch.engine import Monitor, build_runtime
from labwatch.models import AlertEvent, EventKind, Severity
from labwatch.state import Mute, StateStore, Subscriber, mute_matches
from labwatch.subscriptions import (
    CATEGORY_HELP,
    DEFAULT_MUTE,
    MAX_GLOBAL_MUTE,
    categories,
    find_device,
    fmt_until,
    is_duration,
    parse_duration,
    parse_target,
    wants,
)

NOW = 1_800_000_000.0
PT = "Flash/pulse_tube is not on"


def config(tmp_path) -> Config:
    return Config(path=tmp_path / "config.yaml", state_db=tmp_path / "s.sqlite", history_days=30, rule_defaults={},
                  devices=[], notifiers=[], heartbeat_url=None, heartbeat_interval=60.0, daily_summary=None,
                  notify_startup=False, secrets=set())


FLASH_RULES = [
    {"channel": "pulse_tube", "not_equals": 1, "severity": "critical"},
    {"channel": "T4K", "above": 5},
    {"channel": "cooling_water_in", "deviation": 5},
    {"channel": "P1", "above": 1e-3},
    {"channel": "flow", "below": 0.1},
    {"channel": "fse_motor_error", "equals": 1},
    {"channel": "status_text", "max_age": "1h"},  # an "other" channel
]
KET_RULES = [
    {"channel": "scroll1", "not_equals": 1},
    {"channel": "cpa_helium", "above": 330, "category": " Helium "},
]


@pytest.fixture
def monitor(tmp_path):
    """Two Bluefors fridges (never polled: no network) in a Monitor."""
    cfg = config(tmp_path)
    rts = []
    for name, rules, fse in (("Flash", FLASH_RULES, True), ("Ket", KET_RULES, False)):
        sec = Section({"name": name, "type": "bluefors", "host": f"{name.lower()}-pc.invalid", "api_key": "KEY",
                       "fse": fse, "rules": rules}, set())
        cfg.devices.append(sec)
        rts.append(build_runtime(cfg, sec))
    mon = Monitor(cfg, rts, [], StateStore(":memory:"))
    yield mon

    async def close():
        for rt in rts:
            await rt.device.aclose()

    asyncio.run(close())


def event(kind=EventKind.FIRING, severity=Severity.WARNING, device="Flash", rule_id=PT, category="pulse_tube"):
    return AlertEvent(kind, rule_id, device, f"{device}: something", "details", severity, category=category)


def lifecycle(rule_id="labwatch/startup", device="labwatch"):
    return AlertEvent(EventKind.INFO, rule_id, device, "labwatch started", "", Severity.INFO, category="labwatch")


def sub(chat="1", role="user", level="warning") -> Subscriber:
    return Subscriber(chat, "someone", role, level, NOW)


# ---------------------------------------------------------------------------
# mute_matches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target, device, rule_id, category, hit",
    [
        ("*", "Flash", PT, "pulse_tube", True),
        ("*", "labwatch", "labwatch/startup", "labwatch", True),
        ("Flash", "Flash", PT, "pulse_tube", True),
        ("flash", "Flash", PT, "pulse_tube", True),
        ("FLASH", "flash", PT, "pulse_tube", True),
        ("  Flash ", "Flash", PT, "pulse_tube", True),
        ("Ket", "Flash", PT, "pulse_tube", False),
        ("Fla", "Flash", PT, "pulse_tube", False),  # no prefix matching
        ("Flash", "Flash 2", "Flash 2/x", None, False),
        ("pulse_tube", "Flash", PT, "pulse_tube", True),
        ("PULSE_TUBE", "Flash", PT, "pulse_tube", True),
        ("anomaly", "Flash", PT, "pulse_tube", False),
        ("anomaly", "Ket", "Ket/cooling_water_in unusual", "anomaly", True),
        ("Flash/pulse_tube", "Flash", PT, "pulse_tube", True),
        ("flash/PULSE_TUBE", "Flash", PT, "pulse_tube", True),
        ("Ket/pulse_tube", "Flash", PT, "pulse_tube", False),
        ("Flash/anomaly", "Flash", PT, "pulse_tube", False),
        (PT, "Flash", PT, "pulse_tube", True),
        (PT.upper(), "Flash", PT, "pulse_tube", True),
        ("Flash/T4K above 5 K", "Flash", PT, "temperature", False),
        # without a category only "*", the device and the rule id match
        ("pulse_tube", "Flash", PT, None, False),
        ("Flash/pulse_tube", "Flash", PT, None, False),
        ("Flash", "Flash", PT, None, True),
        ("labwatch", "labwatch", "labwatch/mute", "labwatch", True),
    ],
)
def test_mute_matches(target, device, rule_id, category, hit):
    assert mute_matches(target, device, rule_id, category) is hit


def test_mute_dataclass_matches_like_mute_matches():
    m = Mute("Flash/anomaly", None, "", NOW)
    assert m.matches("flash", "Flash/x", "anomaly")
    assert not m.matches("Flash", "Flash/x", "temperature")
    assert not m.matches("Ket", "Ket/x", "anomaly")


# ---------------------------------------------------------------------------
# subscribers in the state store
# ---------------------------------------------------------------------------


@pytest.fixture
def store():
    s = StateStore(":memory:")
    yield s
    s.db.close()


def test_subscriber_roundtrip_and_int_ids(store):
    assert store.subscriber("100") is None
    store.upsert_subscriber(100, "Matthias", "admin", "warning")
    s = store.subscriber("100")
    assert (s.chat_id, s.name, s.role, s.level) == ("100", "Matthias", "admin", "warning")
    assert store.subscriber(100) == s  # int and str ids are the same chat


def test_subscribers_default_excludes_pending_and_filters_roles(store):
    store.upsert_subscriber("1", "admin", "admin", "warning")
    store.upsert_subscriber("2", "Ana", "user", "critical")
    store.upsert_subscriber("3", "stranger", "pending", "warning")
    store.upsert_subscriber("discord:111", "#flash", "user", "warning")
    assert [s.chat_id for s in store.subscribers()] == ["1", "2", "discord:111"]
    assert [s.chat_id for s in store.subscribers(("pending",))] == ["3"]
    assert [s.chat_id for s in store.subscribers(("admin",))] == ["1"]
    assert [s.chat_id for s in store.subscribers(("admin", "user", "pending"))] == ["1", "2", "3", "discord:111"]


def test_upsert_updates_fields_but_keeps_created(store):
    store.upsert_subscriber("2", "", "pending", "warning")
    created = store.subscriber("2").created
    store.upsert_subscriber("2", "Ana", "user", "critical")
    s = store.subscriber("2")
    assert (s.name, s.role, s.level, s.created) == ("Ana", "user", "critical", created)


def test_remove_subscriber_also_removes_only_their_personal_mutes(store):
    store.upsert_subscriber("2", "Ana", "user", "warning")
    store.upsert_subscriber("3", "Ben", "user", "warning")
    store.personal_mute("2", "Flash", None)
    store.personal_mute("3", "Flash", None)
    assert store.remove_subscriber("2") is True
    assert store.subscriber("2") is None
    assert store.personal_mutes("2") == []
    assert [m.target for m in store.personal_mutes("3")] == ["flash"]
    assert store.remove_subscriber("2") is False


# ---------------------------------------------------------------------------
# personal mutes
# ---------------------------------------------------------------------------


def test_personal_mute_is_stored_lowercase_and_replaced(store):
    store.personal_mute("2", " Ket/Circulation ", NOW + 60)
    store.personal_mute("2", "ket/circulation", NOW + 3600)  # same target: replaced, not duplicated
    (m,) = store.personal_mutes("2", NOW)
    assert (m.target, m.until) == ("ket/circulation", NOW + 3600)


def test_personal_mutes_are_per_chat(store):
    store.personal_mute("3", "anomaly", None)
    assert store.personal_mutes("2", NOW) == []
    assert [m.target for m in store.personal_mutes(3, NOW)] == ["anomaly"]


def test_personal_mutes_filter_expired_and_keep_forever(store):
    store.personal_mute("2", "Flash", NOW + 10)
    store.personal_mute("2", "anomaly", None)
    assert {m.target for m in store.personal_mutes("2", NOW)} == {"flash", "anomaly"}
    assert {m.target for m in store.personal_mutes("2", NOW + 10)} == {"anomaly"}  # until is exclusive
    assert {m.target for m in store.personal_mutes("2", NOW + 10**9)} == {"anomaly"}


def test_personal_unmute_one_all_and_case_insensitive(store):
    for t in ("Flash", "anomaly", "*"):
        store.personal_mute("2", t, None)
    store.personal_mute("3", "Flash", None)
    assert store.personal_unmute("2", "FLASH") == 1
    assert store.personal_unmute("2", "Flash") == 0
    assert {m.target for m in store.personal_mutes("2")} == {"anomaly", "*"}
    assert store.personal_unmute("2", "All") == 2
    assert store.personal_mutes("2") == []
    assert [m.target for m in store.personal_mutes("3")] == ["flash"]  # Ben's mute is untouched


def test_prune_removes_expired_personal_and_global_mutes_only(store):
    import time

    now = time.time()
    store.personal_mute("2", "old", now - 1)
    store.personal_mute("2", "active", now + 3600)
    store.personal_mute("2", "forever", None)
    store.personal_mute("3", "old", now - 1)
    store.mute("Ket", now - 1)
    store.mute("Flash", now + 3600)
    store.prune(history_days=30)
    rows = store.db.execute("SELECT chat_id, target FROM personal_mutes ORDER BY chat_id, target").fetchall()
    assert [tuple(r) for r in rows] == [("2", "active"), ("2", "forever")]
    assert [r["target"] for r in store.db.execute("SELECT target FROM mutes")] == ["Flash"]


# ---------------------------------------------------------------------------
# durations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tok, seconds",
    [("90s", 90), ("30m", 1800), ("4h", 4 * 3600), ("2d", 2 * 86400), ("1.5h", 5400), ("4H", 4 * 3600),
     ("2", 2 * 3600), ("0.5", 1800), ("14d", MAX_GLOBAL_MUTE),
     ("forever", None), ("Forever", None), ("off", None), ("indefinitely", None), ("permanent", None),
     ("PERMANENTLY", None)],
)
def test_parse_duration(tok, seconds):
    assert is_duration(tok)
    assert parse_duration(tok) == seconds


@pytest.mark.parametrize("tok", ["", "h", "4x", "-1h", "1e3", "4 h", "abc", "1.h", "4hh", ".5h", "warm-up", "all"])
def test_not_a_duration(tok):
    assert not is_duration(tok)


def test_mute_constants():
    assert DEFAULT_MUTE == 2 * 3600
    assert MAX_GLOBAL_MUTE == 14 * 86400


def test_fmt_until():
    assert fmt_until(None) == "until unmuted"
    assert fmt_until(NOW).startswith("until ") and len(fmt_until(NOW)) > len("until ")


# ---------------------------------------------------------------------------
# categories from the device (engine.build_runtime)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "channel, category",
    [("T50K", "temperature"), ("T4K", "temperature"), ("Tstill", "temperature"), ("TMXC", "temperature"),
     ("pulse_tube", "pulse_tube"), ("compressor", "pulse_tube"), ("cpa_oil", "pulse_tube"),
     ("cpa_alarm", "pulse_tube"), ("cooling_water_in", "cooling_water"), ("cooling_water_dT", "cooling_water"),
     ("flow", "circulation"), ("scroll1", "circulation"), ("turbo2", "circulation"),
     ("ghs_compressor", "circulation"), ("tc400_overtemp", "circulation"), ("P1", "pressure"), ("P6", "pressure"),
     ("T_FSE", "fse"), ("fse_position", "fse"), ("status_text", "other")],
)
def test_bluefors_rule_category_from_channel(tmp_path, channel, category):
    cfg = config(tmp_path)
    rule = {"channel": channel, "max_age": "1h"}
    sec = Section({"name": "Flash", "type": "bluefors", "host": "x.invalid", "api_key": "K", "fse": True,
                   "rules": [rule]}, set())
    rt = build_runtime(cfg, sec)
    try:
        assert rt.rules[0].category == category
        assert rt.unreachable.category == "connection"
    finally:
        asyncio.run(rt.device.aclose())


def test_rule_category_deviation_explicit_and_unreachable(monitor):
    flash, ket = monitor.runtimes
    cats = {r.channel: r.category for r in flash.rules}
    assert cats == {"pulse_tube": "pulse_tube", "T4K": "temperature", "cooling_water_in": "anomaly", "P1": "pressure",
                    "flow": "circulation", "fse_motor_error": "fse", "status_text": "other"}
    assert {r.channel: r.category for r in ket.rules} == {"scroll1": "circulation", "cpa_helium": "helium"}
    assert flash.unreachable.category == ket.unreachable.category == "connection"


def test_explicit_category_wins_over_deviation_default(tmp_path):
    cfg = config(tmp_path)
    sec = Section({"name": "Flash", "type": "bluefors", "host": "x.invalid", "api_key": "K",
                   "rules": [{"channel": "cpa_oil", "deviation": 4, "category": "pulse_tube"}]}, set())
    rt = build_runtime(cfg, sec)
    try:
        assert rt.rules[0].category == "pulse_tube"
    finally:
        asyncio.run(rt.device.aclose())


def test_categories_lists_rule_and_builtin_categories_without_other(monitor):
    cats = categories(monitor)
    assert cats == sorted(cats)
    assert "other" not in cats
    assert set(CATEGORY_HELP) <= set(cats)
    assert "helium" in cats  # from an explicit `category:`


def test_find_device(monitor):
    flash, ket = monitor.runtimes
    assert find_device(monitor, "Flash") is flash
    assert find_device(monitor, "flash") is flash
    assert find_device(monitor, "KET") is ket
    assert find_device(monitor, "fridge") is None


def test_find_device_by_command_name(tmp_path):
    cfg = config(tmp_path)
    sec = Section({"name": "Ket 2", "type": "ping", "host": "ket2-pc.invalid", "method": "tcp", "port": 22,
                   "rules": []}, set())
    mon = Monitor(cfg, [build_runtime(cfg, sec)], [], StateStore(":memory:"))
    assert find_device(mon, "ket_2") is mon.runtimes[0]
    assert find_device(mon, "Ket 2") is mon.runtimes[0]


# ---------------------------------------------------------------------------
# parse_target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tokens, target, rest",
    [
        (["all"], "*", []),
        (["*", "8h"], "*", ["8h"]),
        (["Everything", "4h", "maintenance"], "*", ["4h", "maintenance"]),
        (["flash"], "Flash", []),
        (["FLASH", "4h", "warm", "up"], "Flash", ["4h", "warm", "up"]),
        (["Ket", "4h"], "Ket", ["4h"]),
        (["Ket", "circulation", "1d"], "Ket/circulation", ["1d"]),
        (["ket", "Anomaly"], "Ket/anomaly", []),
        (["ket/anomaly", "2h"], "Ket/anomaly", ["2h"]),
        (["KET/Pulse_Tube"], "Ket/pulse_tube", []),
        (["anomaly", "4h"], "anomaly", ["4h"]),
        (["Pulse_Tube"], "pulse_tube", []),
        (["connection"], "connection", []),
        (["labwatch"], "labwatch", []),
        (["helium"], "helium", []),
    ],
)
def test_parse_target(monitor, tokens, target, rest):
    assert parse_target(monitor, tokens) == (target, rest, "")


@pytest.mark.parametrize(
    "tokens, err",
    [
        ([], "say what to mute"),
        (["fridge"], "unknown target 'fridge'"),
        (["4h"], "unknown target '4h'"),
        (["other"], "unknown target 'other'"),
        (["Ket/bogus"], "unknown target 'Ket/bogus'"),
        (["nope/anomaly"], "unknown target 'nope/anomaly'"),
        (["flash/"], "unknown target 'flash/'"),
    ],
)
def test_parse_target_errors(monitor, tokens, err):
    assert parse_target(monitor, tokens) == (None, [], err)


# ---------------------------------------------------------------------------
# wants(): level, lifecycle, tests, personal mutes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "level, kind, severity, expected",
    [
        ("warning", EventKind.FIRING, Severity.WARNING, True),
        ("warning", EventKind.FIRING, Severity.CRITICAL, True),
        ("warning", EventKind.FIRING, Severity.INFO, False),
        ("critical", EventKind.FIRING, Severity.WARNING, False),
        ("critical", EventKind.FIRING, Severity.CRITICAL, True),
        ("critical", EventKind.REMINDER, Severity.CRITICAL, True),
        ("critical", EventKind.REMINDER, Severity.WARNING, False),
        ("critical", EventKind.RESOLVED, Severity.CRITICAL, True),  # resolved carries the alert's severity
        ("critical", EventKind.RESOLVED, Severity.WARNING, False),
        ("info", EventKind.FIRING, Severity.INFO, True),
        ("info", EventKind.INFO, Severity.INFO, True),
        ("warning", EventKind.INFO, Severity.INFO, False),  # not a lifecycle message
    ],
)
def test_wants_by_level(store, level, kind, severity, expected):
    assert wants(sub(level=level), event(kind, severity), store, now=NOW) is expected


@pytest.mark.parametrize("rule_id, device", [("labwatch/startup", "labwatch"), ("labwatch/shutdown", "labwatch"),
                                              ("labwatch/summary", "labwatch"), ("labwatch/mute", "labwatch"),
                                              ("Flash/armed", "Flash")])
def test_lifecycle_messages_ignore_level_but_respect_setting(store, rule_id, device):
    ev = lifecycle(rule_id, device)
    assert wants(sub(level="critical"), ev, store, lifecycle=True, now=NOW)
    assert not wants(sub(level="info"), ev, store, lifecycle=False, now=NOW)


def test_test_events_only_for_admins(store):
    ev = AlertEvent(EventKind.TEST, "labwatch/test", "labwatch", "test", "", Severity.INFO)
    assert wants(sub(role="admin", level="critical"), ev, store, now=NOW)
    assert wants(sub(role="admin"), ev, store, lifecycle=False, now=NOW)
    assert not wants(sub(role="user", level="info"), ev, store, now=NOW)
    assert not wants(sub(role="pending", level="info"), ev, store, now=NOW)


@pytest.mark.parametrize(
    "target, ev, muted",
    [
        ("Flash", event(device="Flash"), True),
        ("Flash", event(device="Ket", rule_id="Ket/pulse_tube is not on"), False),
        ("anomaly", event(category="anomaly", rule_id="Flash/cw unusual"), True),
        ("anomaly", event(category="pulse_tube"), False),
        ("Flash/anomaly", event(category="anomaly", rule_id="Flash/cw unusual"), True),
        ("Flash/anomaly", event(device="Ket", category="anomaly", rule_id="Ket/cw unusual"), False),
        (PT, event(), True),
        ("*", event(), True),
        ("*", lifecycle(), True),
        ("labwatch", lifecycle("labwatch/mute"), True),
        ("labwatch", event(), False),
        ("Ket", lifecycle("labwatch/mute"), False),  # a device mute never hides "muted for everyone" news
    ],
)
def test_personal_mutes_in_wants(store, target, ev, muted):
    store.personal_mute("3", target, NOW + 3600)
    assert wants(sub("3"), ev, store, now=NOW) is (not muted)


def test_expired_personal_mute_no_longer_applies(store):
    store.personal_mute("3", "Flash", NOW + 60)
    assert not wants(sub("3"), event(), store, now=NOW)
    assert wants(sub("3"), event(), store, now=NOW + 60)


def test_one_persons_mute_does_not_affect_another(store):
    store.personal_mute("3", "*", None)
    store.personal_mute("3", "anomaly", None)
    ana, ben = sub("2", level="warning"), sub("3", level="warning")
    anomaly = event(category="anomaly", rule_id="Flash/cw unusual")
    assert wants(ana, anomaly, store, now=NOW)
    assert wants(ana, event(), store, now=NOW)
    assert not wants(ben, anomaly, store, now=NOW)
    assert not wants(ben, event(), store, now=NOW)


def test_wants_uses_current_time_by_default(store, monkeypatch):
    store.personal_mute("3", "Flash", NOW + 60)
    monkeypatch.setattr(subscriptions, "time", SimpleNamespace(time=lambda: NOW))
    assert not wants(sub("3"), event(), store)
    monkeypatch.setattr(subscriptions, "time", SimpleNamespace(time=lambda: NOW + 61))
    assert wants(sub("3"), event(), store)
