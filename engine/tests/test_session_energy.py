#!/usr/bin/env python3
"""Charge session energy: the car's ``charge_capacity`` wins over the SoC estimate.

    python3 -m unittest engine.tests.test_session_energy -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xpeng_engine.teslamate_mapping import Telemetry
from xpeng_engine.teslamate_provider import (
    ProviderOptions,
    StaticSource,
    TeslaMateProvider,
)

VIN = "L1NNSGHA3SB800783"
USABLE_KWH = 87.5


def charging_frame(soc: int, capacity: Any = None) -> dict[str, Any]:
    charge: dict[str, Any] = {
        "battery_soc": soc,
        "charging_state": 2,
        "charge_gun_status": 1,
    }
    if capacity is not None:
        charge["charge_capacity"] = capacity
    return {
        "instant": {"charge": charge, "odometer": {"avalible_driving_distance": 300}},
        "interval": {"charge": {"power": 7.0}},
    }


def unplugged_frame(soc: int) -> dict[str, Any]:
    return {
        "instant": {
            "charge": {
                "battery_soc": soc,
                "charging_state": 15,
                "charge_gun_status": 0,
            }
        },
        "interval": {"charge": {"power": 0}},
    }


class SessionEnergyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = StaticSource([])
        self.provider = TeslaMateProvider(
            self.source, ProviderOptions(token="t", port=0)
        )
        self.now_ms = 1_700_000_000_000

    def feed(self, raw: dict[str, Any]) -> float | None:
        """Deliver one frame and return the energy the provider reports."""
        self.now_ms += 10_000
        snapshot = Telemetry(
            vin=VIN, raw=raw, now_ms=self.now_ms, usable_kwh=USABLE_KWH
        )
        self.source.update([snapshot])
        prepared = self.provider._prepared()[0]
        energy, _ = self.provider._session_values(prepared)
        return energy

    def test_car_value_replaces_the_soc_estimate(self) -> None:
        self.feed(charging_frame(soc=40))
        energy = self.feed(charging_frame(soc=50, capacity=26.600000381469727))
        # The SoC delta alone would give 10 % of 87.5 kWh = 8.75.
        self.assertEqual(energy, 26.6)

    def test_zero_reading_keeps_the_last_positive_value(self) -> None:
        self.feed(charging_frame(soc=40))
        self.feed(charging_frame(soc=50, capacity=24.5))
        self.assertEqual(self.feed(charging_frame(soc=50, capacity=0)), 24.5)
        self.assertEqual(self.feed(charging_frame(soc=50)), 24.5)

    def test_missing_value_falls_back_to_the_soc_delta(self) -> None:
        self.feed(charging_frame(soc=40))
        energy = self.feed(charging_frame(soc=50))
        self.assertEqual(energy, 8.75)

    def test_new_session_does_not_inherit_the_previous_value(self) -> None:
        self.feed(charging_frame(soc=40))
        self.assertEqual(self.feed(charging_frame(soc=60, capacity=38.8)), 38.8)
        self.feed(unplugged_frame(soc=60))
        self.feed(charging_frame(soc=60))
        energy = self.feed(charging_frame(soc=62, capacity=1.5))
        self.assertEqual(energy, 1.5)


if __name__ == "__main__":
    unittest.main()
