"""Config loading: YAML file + secrets from environment variables / a ``.env`` file.

Secrets never go into ``config.yaml`` directly; reference them as ``${VAR}`` (or
``${VAR:-default}``) and define them in ``.env`` next to the config file or in
the process environment. Every substituted secret is remembered so logging can
redact it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from labwatch.rules import parse_duration

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")
_SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASS", "WEBHOOK", "TOPIC", "HEARTBEAT", "URL", "CHAT")


class ConfigError(ValueError):
    pass


def default_config_path() -> Path:
    env = os.environ.get("LABWATCH_CONFIG")
    if env:
        return Path(env)
    for p in (Path("config.yaml"), Path.home() / ".config" / "labwatch" / "config.yaml"):
        if p.exists():
            return p
    return Path("config.yaml")


def load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env parser (KEY=VALUE, # comments, optional quotes). Doesn't override os.environ."""
    loaded: dict[str, str] = {}
    if not path.is_file():
        return loaded
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


@dataclass
class Expander:
    secrets: set[str] = field(default_factory=set)
    missing: set[str] = field(default_factory=set)

    def expand(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: self.expand(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.expand(v) for v in obj]
        if isinstance(obj, str):
            return self._expand_str(obj)
        return obj

    def _expand_str(self, s: str) -> Any:
        def repl(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            val = os.environ.get(name)
            if val is None or val == "":
                if default is not None:
                    return default
                self.missing.add(name)
                return ""
            if any(h in name.upper() for h in _SECRET_HINTS) and len(val) >= 6:
                self.secrets.add(val)
            return val

        out = _VAR.sub(repl, s)
        # A value that is *only* a variable reference gets number/bool typing ("30" -> 30),
        # unless it's a secret or the conversion would change it ("007" stays a string).
        m = _VAR.fullmatch(s)
        if m and not any(h in m.group(1).upper() for h in _SECRET_HINTS):
            if re.fullmatch(r"[+-]?\d+", out):
                return int(out) if str(int(out)) == out.lstrip("+") else out
            if re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?", out):
                return float(out)
            if out.lower() in ("true", "false"):
                return out.lower() == "true"
        return out


@dataclass
class Section:
    """A config section (one device or notifier) plus env vars it referenced but are unset."""

    cfg: dict[str, Any]
    missing_env: set[str]

    @property
    def name(self) -> str:
        return str(self.cfg.get("name") or self.cfg.get("type") or "?")

    def require_env(self, what: str) -> None:
        if self.missing_env:
            names = ", ".join(sorted(self.missing_env))
            raise ConfigError(f"{what} {self.name!r}: environment variable(s) not set: {names} (add them to .env)")


@dataclass
class Config:
    path: Path
    state_db: Path
    history_days: float
    rule_defaults: dict[str, Any]
    devices: list[Section]
    notifiers: list[Section]
    heartbeat_url: str | None
    heartbeat_interval: float
    daily_summary: str | None  # "HH:MM" local time
    notify_startup: bool
    secrets: set[str]

    def device_section(self, name: str) -> Section:
        for d in self.devices:
            if d.name.lower() == name.lower():
                return d
        known = ", ".join(d.name for d in self.devices) or "(none)"
        raise ConfigError(f"no device named {name!r} in {self.path} (known: {known})")


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path) if path else default_config_path()
    if not path.is_file():
        raise ConfigError(
            f"config file {path} not found. Copy config.example.yaml to config.yaml and edit it, "
            "or pass --config / set LABWATCH_CONFIG."
        )
    load_dotenv(path.parent / ".env")
    if path.parent.resolve() != Path.cwd().resolve():
        load_dotenv(Path.cwd() / ".env")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    secrets: set[str] = set()

    def section(cfg: Any, what: str) -> Section | None:
        if not isinstance(cfg, dict):
            raise ConfigError(f"{path}: each {what} entry must be a mapping, got {cfg!r}")
        ex = Expander()
        expanded = ex.expand(cfg)
        enabled = expanded.pop("enabled", True)
        if enabled is False or (isinstance(enabled, str) and enabled.strip().lower() in ("false", "no", "0", "off")):
            return None
        secrets.update(ex.secrets)
        return Section(expanded, ex.missing)

    devices = [s for s in (section(c, "device") for c in raw.get("devices") or []) if s]
    notifiers = [s for s in (section(c, "notifier") for c in raw.get("notifiers") or []) if s]
    names = [d.name for d in devices]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(f"{path}: duplicate device names: {sorted(dupes)}")

    top = Expander()
    hb = top.expand(raw.get("heartbeat") or {})
    general = top.expand({k: v for k, v in raw.items() if k not in ("devices", "notifiers", "heartbeat")})
    secrets.update(top.secrets)
    if top.missing:
        # Top-level vars (e.g. HEARTBEAT_URL) are optional: missing -> feature disabled.
        pass

    state_db = Path(general.get("state_db", "labwatch.sqlite")).expanduser()
    if not state_db.is_absolute():
        state_db = path.parent / state_db

    summary = general.get("daily_summary")
    if summary is not None:
        summary = str(summary)
        if not re.fullmatch(r"\d{1,2}:\d{2}", summary):
            raise ConfigError(f"{path}: daily_summary must look like '09:00', got {summary!r}")

    return Config(
        path=path,
        state_db=state_db,
        history_days=float(general.get("history_days", 30)),
        rule_defaults=dict(general.get("rule_defaults") or {}),
        devices=devices,
        notifiers=notifiers,
        heartbeat_url=(hb.get("url") or None) if isinstance(hb, dict) else None,
        heartbeat_interval=parse_duration(hb.get("interval", 60)) if isinstance(hb, dict) else 60.0,
        daily_summary=summary,
        notify_startup=bool(general.get("notify_startup", True)),
        secrets=secrets,
    )
