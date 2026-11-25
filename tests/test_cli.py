"""CLI: secret redaction in logs, and the commands that work offline (simulated device, console notifier)."""

from __future__ import annotations

import io
import logging
import os
import textwrap
import time

import pytest

from labwatch.cli import RedactingFilter, main, setup_logging
from labwatch.state import StateStore

SECRET = "lwtest-api-key-5f2e9b"


# ---------------------------------------------------------------------------
# RedactingFilter
# ---------------------------------------------------------------------------


@pytest.fixture
def capture(request):
    """Logger whose output goes through a RedactingFilter into a StringIO."""
    loggers = []

    def make(secrets):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        handler.addFilter(RedactingFilter(secrets))
        logger = logging.getLogger(f"lwtest.redact.{request.node.name}.{len(loggers)}")
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        loggers.append((logger, handler))
        return logger, stream

    yield make
    for logger, handler in loggers:
        logger.removeHandler(handler)


def test_redacts_message_and_args(capture):
    log, out = capture([SECRET])
    log.warning(f"literal {SECRET} in message")
    log.warning("GET https://fridge:49098/values/?key=%s failed (%d)", SECRET, 403)
    log.info("dict arg: %s", {"api_key": SECRET})
    text = out.getvalue()
    assert SECRET not in text
    assert "literal *** in message" in text
    assert "GET https://fridge:49098/values/?key=*** failed (403)" in text
    assert "{'api_key': '***'}" in text


def test_redacts_exception_text(capture):
    log, out = capture([SECRET])
    try:
        raise RuntimeError(f"HTTP 403 for GET /values/?key={SECRET}")
    except RuntimeError:
        log.exception("poll failed")
    text = out.getvalue()
    assert SECRET not in text
    assert "RuntimeError: HTTP 403 for GET /values/?key=***" in text
    assert "Traceback" in text


def test_longest_secret_is_replaced_first(capture):
    short, long_ = "abcdef12", "abcdef12-and-more-secret"
    log, out = capture([short, long_])
    log.warning("token=%s other=%s", long_, short)
    assert out.getvalue().strip() == "WARNING token=*** other=***"


def test_empty_secrets_are_ignored(capture):
    log, out = capture(["", SECRET])
    log.warning("nothing to hide")
    assert out.getvalue().strip() == "WARNING nothing to hide"


def test_without_secrets_records_are_untouched():
    f = RedactingFilter([])
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "value %s", ("a",), None)
    assert f.filter(rec) is True
    assert (rec.msg, rec.args) == ("value %s", ("a",))


def test_setup_logging_installs_redaction_and_quiets_httpx(restore_logging, capsys):
    setup_logging(False, {SECRET})
    logging.getLogger("labwatch.something").warning("key=%s", SECRET)
    err = capsys.readouterr().err
    assert "key=***" in err and SECRET not in err
    assert logging.getLogger("httpx").level == logging.WARNING  # it logs request URLs (with the key)
    assert logging.getLogger().level == logging.INFO


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

CONFIG = """\
state_db: state.sqlite
devices:
  - name: Sim
    type: simulated
    scenario: stable
    api_key: ${LWTEST_CLI_API_KEY}
    rules:
      - {channel: pulse_tube, not_equals: 1, severity: critical}
      - {channel: T4K, above: 3.3, hysteresis: 0.2, name: t4k-high}
notifiers:
  - {name: console, type: console, stdout: true}
"""


@pytest.fixture
def cli(isolated_env, restore_logging, capsys):
    """Run ``labwatch -c <tmp>/config.yaml ...``; returns (exit code, stdout, stderr)."""
    os.environ["LWTEST_CLI_API_KEY"] = SECRET
    path = isolated_env / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")

    def run(*argv):
        rc = main(["-c", str(path), *argv])
        out = capsys.readouterr()
        return rc, out.out, out.err

    run.db = isolated_env / "state.sqlite"
    return run


def test_main_redacts_config_secrets_in_logs(cli, capsys):
    rc, _, _ = cli("mute", "Sim")
    assert rc == 0
    logging.getLogger("labwatch").error("leaked %s", SECRET)
    err = capsys.readouterr().err
    assert "leaked ***" in err and SECRET not in err


def test_mute_and_unmute(cli):
    before = time.time()
    rc, out, _ = cli("mute", "Sim", "--for", "30m", "-r", "warming up")
    assert rc == 0
    assert out.startswith("muted 'Sim' until ")
    store = StateStore(cli.db)
    (m,) = store.mutes()
    assert (m.target, m.reason) == ("Sim", "warming up")
    assert before + 1800 <= m.until <= time.time() + 1800
    store.close()

    rc, out, _ = cli("mute", "Sim/t4k-high", "--indefinitely")
    assert rc == 0 and "until unmuted" in out

    assert cli("unmute", "Sim")[:2] == (0, "unmuted 'Sim'\n")
    assert cli("unmute", "Sim")[:2] == (1, "'Sim' was not muted\n")
    store = StateStore(cli.db)
    assert [m.target for m in store.mutes()] == ["Sim/t4k-high"]
    store.close()


def test_check_polls_and_shows_rules(cli):
    rc, out, _ = cli("check")
    assert rc == 0
    assert out.startswith("== Sim\n")
    assert "T4K" in out and "pulse_tube" in out and "on" in out
    rows = {line.split("  ")[0]: line for line in out.splitlines()}
    assert "ok" in rows["pulse_tube is not on"] and "disarmed" in rows["pulse_tube is not on"]
    assert "'disarmed' rules activate" in out


def test_check_unknown_device_is_config_error(cli):
    rc, _, err = cli("check", "Flash")
    assert rc == 2
    assert "config error: no device named 'Flash'" in err


def test_check_honours_hysteresis_of_firing_rule(cli):
    store = StateStore(cli.db)
    store.save_rule_states([("Sim/t4k-high", "Sim", {"armed": True, "firing": True, "delivered": True})])
    store.close()
    rc, out, _ = cli("check")  # simulated T4K ~3.2 K: above 3.3 - 0.2 -> still bad while firing
    row = next(line for line in out.splitlines() if line.startswith("t4k-high"))
    assert "FIRING" in row
    assert "TRIGGERED" in row


def test_status_shows_saved_state(cli):
    store = StateStore(cli.db)
    store.device_ok("Sim", time.time() - 120)
    store.save_rule_states([("Sim/t4k-high", "Sim", {"armed": True, "firing": True, "last_detail": "T4K = 3.4 K"})])
    store.close()
    cli("mute", "*", "--indefinitely", "-r", "holiday")
    rc, out, _ = cli("status")
    assert rc == 0
    assert "== Sim: last data 2 min ago" in out
    assert "t4k-high" in out and "FIRING" in out and "T4K = 3.4 K" in out
    assert "Muted:" in out and "*  until unmuted  holiday" in out


def test_test_notify_console(cli):
    rc, out, _ = cli("test-notify", "--severity", "critical", "-m", "hello")
    assert rc == 0
    assert "console: sent" in out
    assert "[CRITICAL] Test notification from labwatch" in out
    assert cli("test-notify", "nope")[0] == 1


def test_missing_config_file_exit_code(isolated_env, restore_logging, capsys):
    assert main(["-c", str(isolated_env / "nope.yaml"), "status"]) == 2
    assert "config error" in capsys.readouterr().err


def test_bad_rule_config_exit_code(isolated_env, restore_logging, capsys):
    path = isolated_env / "config.yaml"
    path.write_text(textwrap.dedent("""\
        devices:
          - name: Sim
            type: simulated
            rules:
              - {channel: T5K, above: 5}
    """), encoding="utf-8")
    assert main(["-c", str(path), "check"]) == 2
    assert "unknown channel 'T5K'" in capsys.readouterr().err
