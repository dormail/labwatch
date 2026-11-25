"""A fake dilution fridge for trying out configs and notification channels.

Config::

    - name: Simfridge
      type: simulated
      poll_interval: 5
      scenario: power_outage      # or "stable" / "unreachable"
      outage_after: 60            # seconds after start when the "power fails"
      warmup_rate: 2.0            # simulated time speed-up for the warm-up (K/min on 4K)

At base temperature it reports the same channel names as the Bluefors driver
(T50K, T4K, Tstill, TMXC, pulse_tube, compressor, ...), so the same rules apply.
"""

from __future__ import annotations

import math
import random
import time
from typing import Any

from labwatch.devices.base import ChannelSpec, Device, DeviceUnreachable, register
from labwatch.devices.bluefors import (
    BLUEFORS_STEADY_STATE,
    DEFAULT_CHANNELS,
    STATUS_SECTIONS,
    SUMMARY_CHANNELS,
    bluefors_category,
)
from labwatch.models import Reading, parse_duration, utcnow

BASE = {
    "T50K": 45.0,
    "T4K": 3.2,
    "Tstill": 0.85,
    "TMXC": 12.0,  # mK
    "P1": 3.0e-7,
    "P2": 1.0e-4,
    "P3": 0.25,
    "P4": 2.0,
    "P5": 180.0,
    "P6": 0.5,
    "flow": 0.6,
    "pulse_tube": 1.0,
    "compressor": 1.0,
    "cpa_alarm": 0.0,
    "cpa_warning": 0.0,
    "cooling_water_in": 290.0,
    "cooling_water_out": 297.0,
    "cpa_oil": 305.0,
    "cpa_helium": 310.0,
    "cpa_high_pressure": 19.0,
    "cpa_low_pressure": 6.0,
}


@register("simulated")
class SimulatedDevice(Device):
    summary_channels = SUMMARY_CHANNELS
    overview_channels = ("T4K", "TMXC", "pulse_tube")
    default_steady_state = BLUEFORS_STEADY_STATE

    def category_for(self, channel: str) -> str:
        return bluefors_category(channel)

    @property
    def status_sections(self) -> list[tuple[str, tuple[str, ...]]]:
        return [(title, tuple(c for c in chans if c in self.channels)) for title, chans in STATUS_SECTIONS]

    def __init__(
        self,
        name: str,
        channels: dict[str, ChannelSpec],
        scenario: str = "stable",
        outage_after: float = 60.0,
        warmup_rate: float = 2.0,
        poll_interval: float = 5.0,
        unreachable_after: float = 60.0,
        clock=time.monotonic,
    ) -> None:
        super().__init__(name, channels, poll_interval, unreachable_after)
        self.scenario = scenario
        self.outage_after = outage_after
        self.warmup_rate = warmup_rate
        self._clock = clock
        self._t0 = clock()

    @classmethod
    def from_config(cls, name: str, cfg: dict[str, Any]) -> SimulatedDevice:
        channels = {k: v for k, v in DEFAULT_CHANNELS.items() if k in BASE}
        return cls(
            name=name,
            channels=channels,
            scenario=str(cfg.get("scenario", "stable")),
            outage_after=parse_duration(cfg.get("outage_after", 60.0)),
            warmup_rate=float(cfg.get("warmup_rate", 2.0)),
            poll_interval=parse_duration(cfg.get("poll_interval", 5.0)),
            unreachable_after=parse_duration(cfg.get("unreachable_after", 60.0)),
        )

    def _values(self, elapsed: float) -> dict[str, float]:
        v = dict(BASE)
        if self.scenario == "power_outage" and elapsed > self.outage_after:
            minutes = (elapsed - self.outage_after) / 60.0
            v["pulse_tube"] = 0.0
            v["compressor"] = 0.0
            v["flow"] = max(0.0, 0.6 - 0.3 * minutes)
            # Plates warm up roughly exponentially towards room temperature.
            k = self.warmup_rate / 100.0
            v["T4K"] = 295 - (295 - 3.2) * math.exp(-k * minutes)
            v["T50K"] = 295 - (295 - 45.0) * math.exp(-k * 0.5 * minutes)
            v["Tstill"] = 0.85 + 0.6 * self.warmup_rate * minutes
            v["TMXC"] = 12.0 + 400.0 * self.warmup_rate * minutes
        return v

    async def poll(self) -> list[Reading]:
        elapsed = self._clock() - self._t0
        if self.scenario == "unreachable" or (
            self.scenario == "power_outage_pc" and elapsed > self.outage_after
        ):
            raise DeviceUnreachable(f"{self.name}: simulated control PC is down")
        now = utcnow()
        values = self._values(elapsed)
        out = []
        for spec in self.channels.values():
            base = values.get(spec.name)
            if base is None:
                continue
            noisy = base if spec.name in {"pulse_tube", "compressor", "cpa_alarm", "cpa_warning"} else (
                base * (1 + random.gauss(0, 0.002))
            )
            out.append(
                Reading(
                    self.name, spec.name, spec.source, noisy, now, spec.unit,
                    raw=str(noisy), status="SIMULATED", label=spec.label_for(noisy),
                )
            )
        return out
