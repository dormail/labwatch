"""Config loading: YAML, ${VAR} expansion, .env files, secrets."""

from __future__ import annotations

import os
import textwrap

import pytest

from labwatch.config import ConfigError, Expander, load_config, load_dotenv
from labwatch.devices.bluefors import DEFAULT_CHANNELS
from labwatch.rules import build_rules

API_KEY = "lwtest-bluefors-key-0123456789"


def write(path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def config(tmp_path, text: str, env: str | None = None):
    path = write(tmp_path / "config.yaml", text)
    if env is not None:
        write(tmp_path / ".env", env)
    return load_config(path)


def test_env_substitution_typing_and_defaults(isolated_env):
    os.environ.update({"LWTEST_HOST": "192.0.2.10", "LWTEST_PORT": "49098", "LWTEST_TIMEOUT": "2.5",
                       "LWTEST_VERIFY": "true", "LWTEST_API_KEY": API_KEY})
    cfg = config(isolated_env, """
        devices:
          - name: Flash
            type: bluefors
            host: ${LWTEST_HOST}
            api_key: ${LWTEST_API_KEY}
            port: ${LWTEST_PORT}
            timeout: ${LWTEST_TIMEOUT}
            verify_tls: ${LWTEST_VERIFY}
            url: "https://${LWTEST_HOST}:${LWTEST_PORT}/x"
            poll_interval: ${LWTEST_UNSET_INTERVAL:-30}
            note: ${LWTEST_UNSET_NOTE:-n/a}
            empty: ${LWTEST_UNSET_EMPTY:-}
    """)
    (dev,) = cfg.devices
    c = dev.cfg
    assert c["host"] == "192.0.2.10"
    assert c["port"] == 49098 and isinstance(c["port"], int)  # only-a-variable values get typed
    assert c["timeout"] == 2.5
    assert c["verify_tls"] is True
    assert c["url"] == "https://192.0.2.10:49098/x"  # embedded: stays a string
    assert c["poll_interval"] == 30
    assert c["note"] == "n/a"
    assert c["empty"] == ""
    assert dev.missing_env == set()
    dev.require_env("device")  # no error


def test_missing_env_vars_are_recorded_per_section(isolated_env):
    os.environ["LWTEST_EMPTY_KEY"] = ""  # empty counts as missing
    cfg = config(isolated_env, """
        devices:
          - name: Flash
            type: bluefors
            host: ${LWTEST_MISSING_HOST}
            api_key: ${LWTEST_EMPTY_KEY}
          - name: Sim
            type: simulated
        notifiers:
          - name: discord
            type: discord
            webhook_url: ${LWTEST_MISSING_WEBHOOK}
    """)
    flash, sim = cfg.devices
    assert flash.missing_env == {"LWTEST_MISSING_HOST", "LWTEST_EMPTY_KEY"}
    assert flash.cfg["host"] == ""
    assert sim.missing_env == set()
    with pytest.raises(ConfigError, match=r"device 'Flash': environment variable\(s\) not set: "
                                          r"LWTEST_EMPTY_KEY, LWTEST_MISSING_HOST \(add them to .env\)"):
        flash.require_env("device")
    with pytest.raises(ConfigError, match="notifier 'discord'.*LWTEST_MISSING_WEBHOOK"):
        cfg.notifiers[0].require_env("notifier")


def test_dotenv_next_to_config_is_loaded_but_does_not_override(isolated_env):
    os.environ["LWTEST_HOST"] = "from-process-env"
    cfg = config(isolated_env, """
        devices:
          - name: Flash
            type: bluefors
            host: ${LWTEST_HOST}
            api_key: ${LWTEST_API_KEY}
            chat: ${LWTEST_QUOTED}
            other: ${LWTEST_SINGLE}
    """, env=f"""
        # comment
        LWTEST_HOST=from-dotenv
        export LWTEST_API_KEY={API_KEY}
        LWTEST_QUOTED = "a value with spaces"
        LWTEST_SINGLE='single'
        not a valid line
    """)
    c = cfg.devices[0].cfg
    assert c["host"] == "from-process-env"
    assert c["api_key"] == API_KEY
    assert c["chat"] == "a value with spaces"
    assert c["other"] == "single"
    assert os.environ["LWTEST_API_KEY"] == API_KEY


def test_dotenv_in_working_directory_is_loaded_too(isolated_env):
    write(isolated_env / ".env", "LWTEST_CWD_HOST=from-cwd\n")
    path = write(isolated_env / "elsewhere" / "config.yaml", """
        devices:
          - {name: Flash, type: bluefors, host: "${LWTEST_CWD_HOST}"}
    """)
    assert load_config(path).devices[0].cfg["host"] == "from-cwd"


def test_load_dotenv_returns_values(tmp_path):
    saved = dict(os.environ)
    try:
        env = write(tmp_path / ".env", "LWTEST_A=1\nLWTEST_B=\"two\"\n")
        assert load_dotenv(env) == {"LWTEST_A": "1", "LWTEST_B": "two"}
        assert load_dotenv(tmp_path / "missing.env") == {}
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_secrets_are_collected_for_redaction(isolated_env):
    os.environ.update({
        "LWTEST_API_KEY": API_KEY,
        "LWTEST_HOST": "192.0.2.10",  # not a secret
        "LWTEST_SHORT_TOKEN": "abc",  # too short to redact safely
        "LWTEST_WEBHOOK_URL": "https://example.org/hook/lwtest-webhook-secret",
        "LWTEST_HEARTBEAT_URL": "https://hc.example.org/lwtest-ping-uuid",
        "LWTEST_SMTP_PASSWORD": "lwtest-hunter2-pw",
    })
    cfg = config(isolated_env, """
        heartbeat:
          url: ${LWTEST_HEARTBEAT_URL}
          interval: 2m
        devices:
          - {name: Flash, type: bluefors, host: "${LWTEST_HOST}", api_key: "${LWTEST_API_KEY}"}
        notifiers:
          - {name: d, type: discord, webhook_url: "${LWTEST_WEBHOOK_URL}", token: "${LWTEST_SHORT_TOKEN}"}
          - {name: e, type: email, password: "${LWTEST_SMTP_PASSWORD}", enabled: false}
    """)
    assert cfg.secrets == {API_KEY, "https://example.org/hook/lwtest-webhook-secret",
                           "https://hc.example.org/lwtest-ping-uuid"}
    assert cfg.heartbeat_url == "https://hc.example.org/lwtest-ping-uuid"
    assert cfg.heartbeat_interval == 120


def test_disabled_sections_are_skipped(isolated_env):
    cfg = config(isolated_env, """
        devices:
          - {name: Flash, type: bluefors, host: h, api_key: "${LWTEST_MISSING_KEY}", enabled: false}
          - {name: Sim, type: simulated, enabled: true}
        notifiers:
          - {name: console, type: console}
          - {name: phone, type: ntfy, topic: "${LWTEST_MISSING_TOPIC}", enabled: false}
    """)
    assert [d.name for d in cfg.devices] == ["Sim"]
    assert "enabled" not in cfg.devices[0].cfg
    assert [n.name for n in cfg.notifiers] == ["console"]


def test_enabled_can_come_from_env(isolated_env):
    cfg = config(isolated_env, """
        notifiers:
          - {name: console, type: console}
          - {name: phone, type: ntfy, topic: "${LWTEST_MISSING_TOPIC}", enabled: "${LWTEST_PHONE_ENABLED:-false}"}
    """)
    assert [n.name for n in cfg.notifiers] == ["console"]


def test_numeric_looking_secret_keeps_leading_zeros(isolated_env):
    os.environ["LWTEST_SMTP_PASSWORD"] = "007123456"
    cfg = config(isolated_env, """
        notifiers:
          - {name: e, type: email, password: "${LWTEST_SMTP_PASSWORD}"}
    """)
    assert cfg.notifiers[0].cfg["password"] == "007123456"


def test_yaml_anchor_shares_rules_between_devices(isolated_env):
    cfg = config(isolated_env, """
        devices:
          - name: Flash
            type: bluefors
            host: a
            api_key: k
            rules: &bluefors_rules
              - {channel: pulse_tube, not_equals: 1, severity: critical}
              - {channel: T4K, above: 5, hysteresis: 0.3}
          - name: Mercury
            type: bluefors
            host: b
            api_key: k
            rules: *bluefors_rules
    """)
    flash, mercury = cfg.devices
    assert flash.cfg["rules"] == mercury.cfg["rules"]
    assert flash.cfg["rules"] is not mercury.cfg["rules"]  # independent copies after expansion
    ids = [r.rule_id for d in (flash, mercury) for r in build_rules(d.name, d.cfg["rules"], {}, DEFAULT_CHANNELS)]
    assert ids == ["Flash/pulse_tube is not on", "Flash/T4K above 5 K",
                   "Mercury/pulse_tube is not on", "Mercury/T4K above 5 K"]


def test_general_options_and_defaults(isolated_env):
    cfg = config(isolated_env, """
        state_db: state/lab.sqlite
        history_days: 7
        notify_startup: false
        daily_summary: "09:30"
        rule_defaults: {for: 60, arm_after: 10m}
        heartbeat:
          url: ${LWTEST_UNSET_HEARTBEAT:-}
        devices:
          - {name: Sim, type: simulated}
    """)
    assert cfg.state_db == isolated_env / "state" / "lab.sqlite"  # relative to the config file
    assert cfg.history_days == 7
    assert cfg.notify_startup is False
    assert cfg.daily_summary == "09:30"
    assert cfg.rule_defaults == {"for": 60, "arm_after": "10m"}
    assert cfg.heartbeat_url is None  # empty default -> feature off
    assert cfg.heartbeat_interval == 60
    assert cfg.device_section("sim").name == "Sim"
    with pytest.raises(ConfigError, match=r"no device named 'Flash'.*known: Sim"):
        cfg.device_section("Flash")


def test_empty_config_defaults(isolated_env):
    cfg = config(isolated_env, "")
    assert cfg.devices == [] and cfg.notifiers == []
    assert cfg.state_db == isolated_env / "labwatch.sqlite"
    assert cfg.notify_startup is True and cfg.daily_summary is None


@pytest.mark.parametrize(
    "text, match",
    [
        ("devices: [\n", "invalid YAML"),
        ("- just\n- a list\n", "top level must be a mapping"),
        ("devices:\n  - just-a-string\n", "each device entry must be a mapping"),
        ("devices:\n  - {name: A, type: simulated}\n  - {name: A, type: simulated}\n",
         r"duplicate device names: \['A'\]"),
        ("daily_summary: 9am\n", "daily_summary must look like '09:00'"),
    ],
)
def test_invalid_config(isolated_env, text, match):
    with pytest.raises(ConfigError, match=match):
        config(isolated_env, text)


def test_missing_config_file(isolated_env):
    with pytest.raises(ConfigError, match="not found"):
        load_config(isolated_env / "nope.yaml")


def test_expander_on_nested_structures(isolated_env):
    os.environ["LWTEST_X"] = "1"
    ex = Expander()
    out = ex.expand({"a": ["${LWTEST_X}", {"b": "x${LWTEST_X}"}], "n": 5, "none": None, "m": "${LWTEST_NOPE}"})
    assert out == {"a": [1, {"b": "x1"}], "n": 5, "none": None, "m": ""}
    assert ex.missing == {"LWTEST_NOPE"}
