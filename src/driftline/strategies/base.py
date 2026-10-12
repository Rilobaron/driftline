"""Strategy interface."""

from __future__ import annotations

from typing import Protocol

from driftline.domain.models import Candle, Signal


class Strategy(Protocol):
    """Deterministic strategy contract: completed candles in, signals out."""

    name: str
    warmup_bars: int

    def generate(self, candles: list[Candle]) -> list[Signal]:
        """Generate signals using only data available at each candle.

        Must never use future candles when emitting the signal for index i.
        """
        ...
