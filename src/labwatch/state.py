"""Persistent state in a small SQLite file.

Shared between the running monitor (``labwatch run``) and the CLI
(``labwatch status``, ``labwatch mute``) - and later a dashboard.

Tables:
  latest      last reading per device/channel
  readings    time series of numeric readings (pruned after ``history_days``)
  rule_state  alert state machine per rule (survives restarts)
  events      every alert/notification and where it was delivered
  mutes       silenced devices/rules
  devices     last successful poll / last error per device
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from labwatch.models import AlertEvent, Reading

SCHEMA = """
CREATE TABLE IF NOT EXISTS latest (
    device TEXT NOT NULL, channel TEXT NOT NULL, source TEXT, value REAL, raw TEXT, label TEXT,
    unit TEXT, status TEXT, ts REAL, polled_at REAL NOT NULL,
    PRIMARY KEY (device, channel)
);
CREATE TABLE IF NOT EXISTS readings (
    device TEXT NOT NULL, channel TEXT NOT NULL, ts REAL NOT NULL, value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS readings_idx ON readings (device, channel, ts);
CREATE TABLE IF NOT EXISTS rule_state (
    rule_id TEXT PRIMARY KEY, device TEXT NOT NULL, data TEXT NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, rule_id TEXT,
    device TEXT, severity TEXT, title TEXT, message TEXT, delivery TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS mutes (
    target TEXT PRIMARY KEY, until REAL, reason TEXT, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    device TEXT PRIMARY KEY, last_success REAL, last_error TEXT, last_error_at REAL, info TEXT
);
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id TEXT PRIMARY KEY, name TEXT, role TEXT NOT NULL, level TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS personal_mutes (
    chat_id TEXT NOT NULL, target TEXT NOT NULL, until REAL, created REAL NOT NULL,
    PRIMARY KEY (chat_id, target)
);
"""


def mute_matches(target: str, device: str, rule_id: str = "", category: str | None = None) -> bool:
    """Does a mute target cover this alert?

    Targets (case-insensitive): "*" (everything), a device ("Flash"), an alarm category
    ("anomaly", "pumps"...), a device + category ("Flash/anomaly") or a rule id
    ("Flash/T4K above 5 K").
    """
    t = target.strip().lower()
    dev = device.lower()
    cands = {"*", dev, rule_id.lower()}
    if category:
        cands |= {category.lower(), f"{dev}/{category.lower()}"}
    return t in cands


@dataclass
class Mute:
    target: str
    until: float | None  # None = until unmuted
    reason: str
    created: float

    def matches(self, device: str, rule_id: str, category: str | None = None) -> bool:
        return mute_matches(self.target, device, rule_id, category)


@dataclass
class Subscriber:
    """A Telegram chat that receives alerts (a person or a group)."""

    chat_id: str
    name: str
    role: str  # "admin" | "user" | "pending"
    level: str  # minimum severity this person wants: info / warning / critical
    created: float


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.checkpoint()
        self.db.close()

    def checkpoint(self) -> None:
        """Fold the write-ahead log into the main file (so copying labwatch.sqlite is enough)."""
        if self.path != ":memory:":
            try:
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass

    def backup_to(self, dest: str | Path) -> None:
        """Consistent copy of the database, safe while the monitor is running."""
        target = sqlite3.connect(str(dest))
        try:
            self.db.backup(target)
        finally:
            target.close()

    # -- readings ---------------------------------------------------------

    def save_readings(self, readings: Iterable[Reading], polled_at: float, history: bool = True) -> None:
        latest_rows, hist_rows = [], []
        for r in readings:
            ts = r.timestamp.timestamp() if r.timestamp else None
            latest_rows.append(
                (r.device, r.channel, r.source, r.value, r.raw, r.label, r.unit, r.status, ts, polled_at)
            )
            if history and r.value is not None:
                hist_rows.append((r.device, r.channel, ts or polled_at, r.value))
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO latest VALUES (?,?,?,?,?,?,?,?,?,?)", latest_rows
            )
            if hist_rows:
                self.db.executemany("INSERT INTO readings VALUES (?,?,?,?)", hist_rows)

    def latest(self, device: str | None = None) -> list[sqlite3.Row]:
        if device:
            return self.db.execute(
                "SELECT * FROM latest WHERE device=? ORDER BY rowid", (device,)
            ).fetchall()
        return self.db.execute("SELECT * FROM latest ORDER BY device, rowid").fetchall()

    def history(self, device: str, channel: str, since: float) -> list[tuple[float, float]]:
        rows = self.db.execute(
            "SELECT ts, value FROM readings WHERE device=? AND channel=? AND ts>=? ORDER BY ts",
            (device, channel, since),
        ).fetchall()
        return [(r["ts"], r["value"]) for r in rows]

    def prune(self, history_days: float, events_days: float = 365) -> None:
        now = time.time()
        with self.db:
            self.db.execute("DELETE FROM readings WHERE ts < ?", (now - history_days * 86400,))
            self.db.execute("DELETE FROM events WHERE ts < ?", (now - events_days * 86400,))
            self.db.execute("DELETE FROM mutes WHERE until IS NOT NULL AND until < ?", (now,))
            self.db.execute("DELETE FROM personal_mutes WHERE until IS NOT NULL AND until < ?", (now,))

    # -- devices ----------------------------------------------------------

    def device_ok(self, device: str, ts: float, info: dict[str, Any] | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO devices (device, last_success, info) VALUES (?,?,?) "
                "ON CONFLICT(device) DO UPDATE SET last_success=excluded.last_success, "
                "info=COALESCE(excluded.info, devices.info)",
                (device, ts, json.dumps(info) if info else None),
            )

    def device_error(self, device: str, ts: float, error: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO devices (device, last_error, last_error_at) VALUES (?,?,?) "
                "ON CONFLICT(device) DO UPDATE SET last_error=excluded.last_error, "
                "last_error_at=excluded.last_error_at",
                (device, error, ts),
            )

    def devices(self) -> dict[str, sqlite3.Row]:
        return {r["device"]: r for r in self.db.execute("SELECT * FROM devices")}

    # -- rule state -------------------------------------------------------

    def save_rule_states(self, items: Iterable[tuple[str, str, dict[str, Any]]]) -> None:
        now = time.time()
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO rule_state VALUES (?,?,?,?)",
                [(rid, dev, json.dumps(d), now) for rid, dev, d in items],
            )

    def load_rule_states(self) -> dict[str, dict[str, Any]]:
        return {r["rule_id"]: json.loads(r["data"]) for r in self.db.execute("SELECT * FROM rule_state")}

    def rule_states(self, device: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM rule_state" + (" WHERE device=?" if device else "") + " ORDER BY rule_id"
        return self.db.execute(q, (device,) if device else ()).fetchall()

    # -- events -----------------------------------------------------------

    def log_event(self, ev: AlertEvent, delivery: dict[str, str]) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO events (ts, kind, rule_id, device, severity, title, message, delivery) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ev.timestamp.timestamp(), ev.kind.value, ev.rule_id, ev.device, ev.severity.label,
                 ev.title, ev.message, json.dumps(delivery)),
            )

    def events(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # -- mutes ------------------------------------------------------------

    def mute(self, target: str, until: float | None, reason: str = "") -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO mutes VALUES (?,?,?,?)", (target, until, reason, time.time())
            )

    def unmute(self, target: str) -> bool:
        with self.db:
            cur = self.db.execute("DELETE FROM mutes WHERE lower(target)=lower(?)", (target,))
        return cur.rowcount > 0

    def mutes(self, now: float | None = None) -> list[Mute]:
        now = time.time() if now is None else now
        rows = self.db.execute(
            "SELECT * FROM mutes WHERE until IS NULL OR until > ? ORDER BY created", (now,)
        ).fetchall()
        return [Mute(r["target"], r["until"], r["reason"] or "", r["created"]) for r in rows]

    # -- subscribers (Telegram users) ---------------------------------------------

    def subscriber(self, chat_id: str | int) -> Subscriber | None:
        r = self.db.execute("SELECT * FROM subscribers WHERE chat_id=?", (str(chat_id),)).fetchone()
        return Subscriber(r["chat_id"], r["name"] or "", r["role"], r["level"], r["created"]) if r else None

    def subscribers(self, roles: tuple[str, ...] = ("admin", "user")) -> list[Subscriber]:
        q = f"SELECT * FROM subscribers WHERE role IN ({','.join('?' * len(roles))}) ORDER BY created"
        return [Subscriber(r["chat_id"], r["name"] or "", r["role"], r["level"], r["created"])
                for r in self.db.execute(q, roles)]

    def upsert_subscriber(self, chat_id: str | int, name: str, role: str, level: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO subscribers VALUES (?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET "
                "name=excluded.name, role=excluded.role, level=excluded.level",
                (str(chat_id), name, role, level, time.time()),
            )

    def remove_subscriber(self, chat_id: str | int) -> bool:
        with self.db:
            cur = self.db.execute("DELETE FROM subscribers WHERE chat_id=?", (str(chat_id),))
            self.db.execute("DELETE FROM personal_mutes WHERE chat_id=?", (str(chat_id),))
        return cur.rowcount > 0

    def personal_mute(self, chat_id: str | int, target: str, until: float | None) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO personal_mutes VALUES (?,?,?,?)",
                            (str(chat_id), target.strip().lower(), until, time.time()))

    def personal_unmute(self, chat_id: str | int, target: str) -> int:
        with self.db:
            if target.strip().lower() == "all":
                cur = self.db.execute("DELETE FROM personal_mutes WHERE chat_id=?", (str(chat_id),))
            else:
                cur = self.db.execute("DELETE FROM personal_mutes WHERE chat_id=? AND target=?",
                                      (str(chat_id), target.strip().lower()))
        return cur.rowcount

    def personal_mutes(self, chat_id: str | int, now: float | None = None) -> list[Mute]:
        now = time.time() if now is None else now
        rows = self.db.execute(
            "SELECT * FROM personal_mutes WHERE chat_id=? AND (until IS NULL OR until > ?) ORDER BY created",
            (str(chat_id), now),
        ).fetchall()
        return [Mute(r["target"], r["until"], "", r["created"]) for r in rows]
