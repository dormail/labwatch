"""Lifecycle messages (started/stopped, alarms armed, daily summary) bypass min_severity unless disabled."""

from __future__ import annotations

import pytest

from labwatch.models import AlertEvent, EventKind, Severity
from labwatch.notifiers import NotifierConfigError, build_notifier
from labwatch.notifiers.base import is_lifecycle


def event(kind: EventKind, rule_id: str, severity: Severity = Severity.INFO) -> AlertEvent:
    return AlertEvent(kind, rule_id, "Fridge", "title", "message", severity)


LIFECYCLE = [
    event(EventKind.INFO, "labwatch/startup"),
    event(EventKind.INFO, "labwatch/shutdown"),
    event(EventKind.INFO, "labwatch/summary"),
    event(EventKind.INFO, "Fridge/armed"),
]
NOT_LIFECYCLE = [
    event(EventKind.INFO, "Fridge/T4K above 5 K"),  # some other INFO event
    event(EventKind.INFO, "labwatch/startup-ish"),
    event(EventKind.FIRING, "Fridge/armed"),  # a user rule named "armed" is an alert, not lifecycle
    event(EventKind.RESOLVED, "labwatch/summary"),
]


@pytest.mark.parametrize("ev", LIFECYCLE, ids=lambda e: e.rule_id)
def test_is_lifecycle(ev):
    assert is_lifecycle(ev)


@pytest.mark.parametrize("ev", NOT_LIFECYCLE, ids=lambda e: f"{e.kind.value}:{e.rule_id}")
def test_is_not_lifecycle(ev):
    assert not is_lifecycle(ev)


def warning_console(**cfg):
    return build_notifier({"type": "console", "name": "telegram-ish", "min_severity": "warning", **cfg})


def test_lifecycle_on_by_default_lets_status_messages_through():
    n = warning_console()
    assert n.lifecycle is True
    assert all(n.accepts(ev) for ev in LIFECYCLE)


def test_normal_info_event_still_filtered_by_min_severity():
    n = warning_console()
    assert not n.accepts(event(EventKind.INFO, "Fridge/T4K above 5 K"))
    assert not n.accepts(event(EventKind.FIRING, "Fridge/armed"))
    assert n.accepts(event(EventKind.FIRING, "Fridge/T4K above 5 K", Severity.WARNING))
    assert n.accepts(event(EventKind.TEST, "labwatch/test"))  # test messages always go through


def test_lifecycle_false_from_config():
    n = warning_console(lifecycle=False)
    assert n.lifecycle is False
    assert not any(n.accepts(ev) for ev in LIFECYCLE)
    assert n.accepts(event(EventKind.FIRING, "Fridge/pulse_tube is not on", Severity.CRITICAL))
    assert n.accepts(event(EventKind.TEST, "labwatch/test"))


def test_lifecycle_does_not_override_info_level_notifier():
    n = build_notifier({"type": "console", "min_severity": "info", "lifecycle": False})
    assert n.accepts(event(EventKind.INFO, "labwatch/startup"))  # INFO >= INFO anyway


def test_lifecycle_is_per_instance():
    a, b = warning_console(lifecycle=False), warning_console()
    assert (a.lifecycle, b.lifecycle) == (False, True)


@pytest.mark.parametrize("value", ["false", "no", 0, 1, None])
def test_lifecycle_must_be_bool(value):
    with pytest.raises(NotifierConfigError, match="'lifecycle' must be true or false"):
        warning_console(lifecycle=value)


def test_lifecycle_key_is_consumed_not_passed_to_notifier(caplog):
    warning_console(lifecycle=True)
    assert "unknown option" not in caplog.text
