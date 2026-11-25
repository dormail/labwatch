"""Bluefors dilution refrigerator via the Control Software (CS1) v2.x HTTP API.

Tested against Control Software 2.6.1 (``api-2.6.1``) and 2.7.0 (``api-2.7.0``). The API is served over
HTTPS on port 49098 with a self-signed certificate. Authentication is an API
key (created in the control software) passed as the ``key`` query parameter.

Endpoints used:

* ``GET /system/?key=..`` -> ``{"data": {"system_name": "Flash", "sw_version": "2.6.1", ...}}``
* ``GET /values/<dotted/path/as/slashes>?key=..`` -> ``{"data": {"<dotted.path>": node, ...}}``
  Without a ``recursion`` parameter the whole subtree below the path is returned;
  ``recursion=N`` limits the depth.

A value node looks like::

    {"name": "mapper.bf.temperatures.t50k",
     "type": "Value.Number.Float.Unit.temperature",
     "content": {"maximum_age": 900000,
                 "latest_value":       {"value": "269.09", "date": 1795636845763,
                                        "status": "INDEPENDENT", "outdated": false, "exception": ""},
                 "latest_valid_value": {...same shape, or null...}}}

``value`` is always a string ("" when invalid), ``date`` is ms since the epoch,
``status`` is e.g. SYNCHRONIZED / INDEPENDENT / CHANGED / INVALID.

Errors come back as ``{"error": {"code": 2003, "name": "Access denied", ...}}``.
NOTE: error bodies echo the full query *including the API key* - never log them raw.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from datetime import datetime, timezone
from typing import Any

import httpx

from labwatch.devices.base import (
    ChannelSpec,
    Device,
    DeviceConfigError,
    DeviceError,
    DeviceUnreachable,
    DiscoveredValue,
    parse_channels,
    register,
)
from labwatch.models import Reading, parse_duration, utcnow

log = logging.getLogger(__name__)

DEFAULT_PORT = 49098

# Paths verified on a Bluefors system running CS 2.6.1 with a Cryomech CPA compressor.
# Enumerations: pulse tube / pumps / valves (onOffError): 0 = off, 1 = on, 2 = error.
#               CPA compressor_running_status: 0 = stopped, 1 = running.
ON_OFF_ERROR = ((0, "off"), (1, "on"), (2, "error"))
RUNNING = ((0, "stopped"), (1, "running"))

DEFAULT_CHANNELS: dict[str, ChannelSpec] = {
    spec.name: spec
    for spec in [
        ChannelSpec("T50K", "mapper.bf.temperatures.t50k", "K"),
        ChannelSpec("T4K", "mapper.bf.temperatures.t4k", "K"),
        ChannelSpec("Tstill", "mapper.bf.temperatures.tstill", "K"),
        ChannelSpec("TMXC", "mapper.bf.temperatures.tmixing", "mK", scale=1000.0),
        ChannelSpec("P1", "mapper.bf.pressures.p1", "mbar"),
        ChannelSpec("P2", "mapper.bf.pressures.p2", "mbar"),
        ChannelSpec("P3", "mapper.bf.pressures.p3", "mbar"),
        ChannelSpec("P4", "mapper.bf.pressures.p4", "mbar"),
        ChannelSpec("P5", "mapper.bf.pressures.p5", "mbar"),
        ChannelSpec("P6", "mapper.bf.pressures.p6", "mbar"),
        ChannelSpec("flow", "mapper.bf.flow", "mmol/s"),
        ChannelSpec("pulse_tube", "mapper.bf.pulsetube", labels=ON_OFF_ERROR),
        ChannelSpec("compressor", "driver.cpa.compressor_running_status", labels=RUNNING),
        ChannelSpec("cpa_alarm", "driver.cpa.alarm_state"),
        ChannelSpec("cpa_alarm_text", "driver.cpa.alarm_state_text"),
        ChannelSpec("cpa_warning", "driver.cpa.warning_state"),
        ChannelSpec("cpa_warning_text", "driver.cpa.warning_state_text"),
        ChannelSpec("cooling_water_in", "driver.cpa.coolant_in_temperature", "K"),
        ChannelSpec("cooling_water_out", "driver.cpa.coolant_out_temperature", "K"),
        ChannelSpec("cpa_oil", "driver.cpa.oil_temperature", "K"),
        ChannelSpec("cpa_helium", "driver.cpa.helium_temperature", "K"),
        ChannelSpec("cpa_high_pressure", "driver.cpa.high_pressure"),
        ChannelSpec("cpa_low_pressure", "driver.cpa.low_pressure"),
        ChannelSpec("status_text", "general.status.status"),
    ]
}

INVALID_STATUSES = {"INVALID", "ERROR", "DISCONNECTED"}


def _make_verify(verify_tls: Any) -> bool | ssl.SSLContext:
    """``True``/``False`` or a path to the fridge's self-signed certificate (pinning).

    The Bluefors certificate's CN is "Self signed certificate", so when pinning we
    trust exactly that certificate but skip the hostname check.
    """
    if isinstance(verify_tls, bool):
        return verify_tls
    if isinstance(verify_tls, str) and verify_tls.lower() in {"true", "false"}:
        return verify_tls.lower() == "true"
    ctx = ssl.create_default_context(cafile=str(verify_tls))
    ctx.check_hostname = False
    return ctx


def _parse_date(ms: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _parse_float(raw: Any) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in {"none", "null", "nan"}:
        return None
    if s.lower() in {"true", "false"}:
        return 1.0 if s.lower() == "true" else 0.0
    try:
        return float(s)
    except ValueError:
        return None


def node_to_reading(device: str, spec: ChannelSpec, node: dict[str, Any] | None) -> Reading:
    """Convert one API value node into a :class:`Reading`."""
    if not node or not isinstance(node.get("content"), dict):
        return Reading(device, spec.name, spec.source, None, None, spec.unit, status="NOT_FOUND")
    content = node["content"]
    latest = content.get("latest_value") or {}
    raw = latest.get("value")
    status = latest.get("status")
    value = _parse_float(raw)
    if status in INVALID_STATUSES:
        value = None
    if value is not None:
        value *= spec.scale
    return Reading(
        label=spec.label_for(value),
        device=device,
        channel=spec.name,
        source=spec.source,
        value=value,
        timestamp=_parse_date(latest.get("date")),
        unit=spec.unit,
        raw=None if raw is None else str(raw),
        status=status,
        outdated=bool(latest.get("outdated")),
    )


def _api_error(body: Any) -> str | None:
    """Return 'code name: description' from an API error body (without the echoed query/key)."""
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        e = body["error"]
        return f"{e.get('code')} {e.get('name')}: {e.get('description')}"
    return None


# Channels shown in every alert message, to give the full picture at a glance.
SUMMARY_CHANNELS = ("T50K", "T4K", "Tstill", "TMXC", "pulse_tube", "compressor", "flow", "cooling_water_in")


@register("bluefors")
class BlueforsDevice(Device):
    summary_channels = SUMMARY_CHANNELS
    def __init__(
        self,
        name: str,
        host: str,
        api_key: str,
        channels: dict[str, ChannelSpec],
        port: int = DEFAULT_PORT,
        verify_tls: Any = False,
        timeout: float = 10.0,
        poll_interval: float = 30.0,
        unreachable_after: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(name, channels, poll_interval, unreachable_after)
        self.host = host
        self.port = port
        self._key = api_key
        self._client = httpx.AsyncClient(
            base_url=f"https://{host}:{port}",
            verify=_make_verify(verify_tls),
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": "labwatch"},
        )
        self._missing_logged: set[str] = set()

    @classmethod
    def from_config(cls, name: str, cfg: dict[str, Any]) -> BlueforsDevice:
        host = cfg.get("host")
        key = cfg.get("api_key")
        if not host:
            raise DeviceConfigError(f"device {name!r}: 'host' is required")
        if not key:
            raise DeviceConfigError(
                f"device {name!r}: 'api_key' is required (create a read-only key in the "
                "Bluefors control software and put it in .env)"
            )
        return cls(
            name=name,
            host=str(host),
            api_key=str(key),
            channels=parse_channels(name, cfg, DEFAULT_CHANNELS),
            port=int(cfg.get("port", DEFAULT_PORT)),
            verify_tls=cfg.get("verify_tls", False),
            timeout=parse_duration(cfg.get("timeout", 10.0)),
            poll_interval=parse_duration(cfg.get("poll_interval", 30.0)),
            unreachable_after=parse_duration(cfg.get("unreachable_after", 300.0)),
        )

    # -- HTTP -------------------------------------------------------------

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        """GET an API path, returning ``data``. Raises DeviceError/DeviceUnreachable.

        Exception messages never contain the URL (which carries the API key).
        """
        try:
            resp = await self._client.get(path, params={**params, "key": self._key})
        except httpx.TimeoutException:
            raise DeviceUnreachable(f"{self.name}: timeout talking to {self.host}:{self.port}") from None
        except httpx.TransportError as e:
            raise DeviceUnreachable(
                f"{self.name}: cannot connect to {self.host}:{self.port} ({type(e).__name__})"
            ) from None
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code == 403:
            raise DeviceError(f"{self.name}: API key rejected ({_api_error(body) or 'HTTP 403'})")
        if resp.status_code >= 400 or not isinstance(body, dict) or "data" not in body:
            raise DeviceError(f"{self.name}: {path}: {_api_error(body) or f'HTTP {resp.status_code}'}")
        return body["data"] or {}

    async def describe(self) -> dict[str, Any]:
        return await self._get("/system/")

    async def _read_channel(self, spec: ChannelSpec, sem: asyncio.Semaphore) -> Reading:
        async with sem:
            try:
                data = await self._get("/values/" + spec.source.replace(".", "/"), recursion=0)
            except DeviceUnreachable:
                raise
            except DeviceError as e:
                if "API key" in str(e):
                    raise
                if spec.source not in self._missing_logged:
                    self._missing_logged.add(spec.source)
                    log.warning("%s: channel %s (%s) unavailable: %s", self.name, spec.name, spec.source, e)
                return Reading(self.name, spec.name, spec.source, None, None, spec.unit, status="NOT_FOUND")
        return node_to_reading(self.name, spec, data.get(spec.source))

    async def poll(self) -> list[Reading]:
        sem = asyncio.Semaphore(8)
        results = await asyncio.gather(
            *(self._read_channel(spec, sem) for spec in self.channels.values()),
            return_exceptions=True,
        )
        readings: list[Reading] = []
        for r in results:
            if isinstance(r, DeviceError):
                raise r  # unreachable or bad key: the whole poll failed
            if isinstance(r, BaseException):
                raise DeviceError(f"{self.name}: unexpected error: {type(r).__name__}") from r
            readings.append(r)
        return readings

    async def discover(self, prefix: str = "") -> list[DiscoveredValue]:
        path = "/values/" + prefix.strip(".").replace(".", "/")
        data = await self._get(path)
        now = utcnow()
        out = []
        for source, node in sorted(data.items()):
            content = node.get("content") if isinstance(node, dict) else None
            if not isinstance(content, dict) or node.get("type") == "Method":
                continue
            latest = content.get("latest_value") or {}
            date = _parse_date(latest.get("date"))
            out.append(
                DiscoveredValue(
                    source=source,
                    type=node.get("type"),
                    raw=None if latest.get("value") is None else str(latest.get("value")),
                    status=latest.get("status"),
                    age=(now - date).total_seconds() if date else None,
                )
            )
        return out

    async def aclose(self) -> None:
        await self._client.aclose()

    def __repr__(self) -> str:  # never include the key
        return f"<BlueforsDevice {self.name!r} {self.host}:{self.port}>"
