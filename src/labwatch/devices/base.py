"""Device interface and registry."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, Callable

from labwatch.models import Reading


class DeviceError(Exception):
    """The device answered, but not usefully (bad key, bad path, ...)."""


class DeviceUnreachable(DeviceError):
    """The device (or the PC running its control software) could not be reached."""


class DeviceConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ChannelSpec:
    """Maps a friendly channel name to a device-native source path."""

    name: str
    source: str
    unit: str | None = None
    scale: float = 1.0  # multiply raw values, e.g. 1000 to show K as mK
    labels: tuple[tuple[int, str], ...] = ()  # enumeration value -> text, e.g. ((0, "off"), (1, "on"))

    def label_for(self, value: float | None) -> str | None:
        if value is None or not self.labels or value != int(value):
            return None
        return dict(self.labels).get(int(value))

    @classmethod
    def parse(cls, name: str, spec: Any) -> ChannelSpec:
        if isinstance(spec, str):
            return cls(name=name, source=spec)
        if isinstance(spec, dict):
            source = spec.get("path") or spec.get("source")
            if not source:
                raise DeviceConfigError(f"channel {name!r}: missing 'path'")
            labels = spec.get("labels") or {}
            if not isinstance(labels, dict):
                raise DeviceConfigError(f"channel {name!r}: 'labels' must be a mapping like {{0: off, 1: on}}")
            return cls(
                name=name,
                source=str(source),
                unit=spec.get("unit"),
                scale=float(spec.get("scale", 1.0)),
                labels=tuple((int(k), str(v)) for k, v in labels.items()),
            )
        raise DeviceConfigError(f"channel {name!r}: expected a path string or a mapping, got {spec!r}")


@dataclass(frozen=True)
class DiscoveredValue:
    source: str
    type: str | None
    raw: str | None
    status: str | None
    age: float | None  # seconds


class Device(abc.ABC):
    type_name: str = ""
    summary_channels: tuple[str, ...] = ()  # shown as context in alert messages

    def __init__(
        self,
        name: str,
        channels: dict[str, ChannelSpec],
        poll_interval: float = 30.0,
        unreachable_after: float = 300.0,
    ) -> None:
        self.name = name
        self.channels = channels
        self.poll_interval = poll_interval
        self.unreachable_after = unreachable_after

    @abc.abstractmethod
    async def poll(self) -> list[Reading]:
        """Read all configured channels.

        Raise :class:`DeviceUnreachable` if the device can't be contacted at all.
        Individual channels that fail are returned as invalid readings.
        """

    async def discover(self, prefix: str = "") -> list[DiscoveredValue]:
        """List the values the device offers (for writing configs)."""
        raise NotImplementedError(f"{self.type_name} devices don't support discovery")

    async def describe(self) -> dict[str, Any]:
        """Short identification info (software version, ...)."""
        return {}

    async def aclose(self) -> None:  # noqa: B027
        pass

    @classmethod
    @abc.abstractmethod
    def from_config(cls, name: str, cfg: dict[str, Any]) -> Device: ...

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"


_REGISTRY: dict[str, type[Device]] = {}


def register(type_name: str) -> Callable[[type[Device]], type[Device]]:
    def deco(cls: type[Device]) -> type[Device]:
        cls.type_name = type_name
        _REGISTRY[type_name] = cls
        return cls

    return deco


def build_device(cfg: dict[str, Any]) -> Device:
    cfg = dict(cfg)
    name = cfg.pop("name", None)
    if not name:
        raise DeviceConfigError("device config is missing 'name'")
    type_name = cfg.pop("type", None)
    cls = _REGISTRY.get(type_name or "")
    if cls is None:
        known = ", ".join(sorted(_REGISTRY))
        raise DeviceConfigError(f"device {name!r}: unknown type {type_name!r}; known: {known}")
    return cls.from_config(name, cfg)


def parse_channels(
    name: str,
    cfg: dict[str, Any],
    defaults: dict[str, ChannelSpec] | None = None,
) -> dict[str, ChannelSpec]:
    """Merge default channels with the ``channels``/``exclude_channels`` config keys."""
    channels: dict[str, ChannelSpec] = {}
    if defaults and cfg.get("default_channels", True):
        channels.update(defaults)
    for ch_name, spec in (cfg.get("channels") or {}).items():
        if spec is None:  # `T4K: null` removes a default channel
            channels.pop(ch_name, None)
            continue
        channels[ch_name] = ChannelSpec.parse(ch_name, spec)
    for ch_name in cfg.get("exclude_channels") or []:
        channels.pop(ch_name, None)
    if not channels:
        raise DeviceConfigError(f"device {name!r}: no channels configured")
    return channels
