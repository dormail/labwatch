"""labwatch backup: a consistent, restorable bundle of config, .env and state."""

from __future__ import annotations

import sqlite3
import stat
import tarfile

from labwatch import cli
from labwatch.state import StateStore

CONFIG = """
state_db: labwatch.sqlite
devices:
  - {name: Sim, type: simulated}
"""


def test_backup_bundles_config_env_and_consistent_db(isolated_env, tmp_path, capsys):
    (tmp_path / "config.yaml").write_text(CONFIG)
    (tmp_path / ".env").write_text("SOME_TOKEN=abcdef123456\n")
    store = StateStore(tmp_path / "labwatch.sqlite")
    store.mute("Sim", None, "kept across machines")  # lives in the WAL until checkpointed
    # Leave the connection open (like a running monitor): the backup must still see the data.
    out = tmp_path / "bundle.tar.gz"
    assert cli.main(["-c", str(tmp_path / "config.yaml"), "backup", "-o", str(out)]) == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600  # contains secrets
    with tarfile.open(out) as tar:
        assert sorted(tar.getnames()) == [".env", "config.yaml", "labwatch.sqlite"]
        dest = tmp_path / "restored"
        tar.extractall(dest, filter="data")
    db = sqlite3.connect(dest / "labwatch.sqlite")
    assert db.execute("SELECT reason FROM mutes WHERE target='Sim'").fetchone() == ("kept across machines",)
    assert "scp" in capsys.readouterr().out
    store.close()


def test_checkpoint_folds_wal_into_main_file(tmp_path):
    path = tmp_path / "s.sqlite"
    store = StateStore(path)
    store.mute("X", None)
    store.checkpoint()
    wal = path.with_name(path.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    store.close()
