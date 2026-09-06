"""Latency model: d ~ max(0, N(mean, sd)) or a fixed value for determinism."""

from __future__ import annotations

import random

from rehearsal.config import LatencyConfig


class LatencyModel:
    def __init__(self, cfg: LatencyConfig, seed: int | None = None):
        self.cfg = cfg
        self.rng = random.Random(seed)

    def draw_ms(self) -> int:
        if self.cfg.fixed_ms is not None:
            return int(self.cfg.fixed_ms)
        d = self.rng.gauss(self.cfg.mean_ms, self.cfg.sd_ms)
        return int(max(0.0, d))

    def describe(self) -> str:
        if self.cfg.fixed_ms is not None:
            return f"fixed {int(self.cfg.fixed_ms)} ms"
        return f"N({self.cfg.mean_ms:.0f}, {self.cfg.sd_ms:.0f}) ms"
