from __future__ import annotations

import logging
import os

import pytest


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Work in an empty temp dir and restore ``os.environ`` afterwards.

    ``load_config()`` reads ``.env`` from the config's directory *and* from the
    current directory, and writes what it finds into ``os.environ``. Without this
    fixture a test run from the repo root would load the developer's real ``.env``.
    """
    saved = dict(os.environ)
    monkeypatch.chdir(tmp_path)
    yield tmp_path
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def restore_logging():
    """``cli.main()`` replaces the root logger's handlers; put pytest's back afterwards."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    others = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, lvl in others.items():
        logging.getLogger(name).setLevel(lvl)
