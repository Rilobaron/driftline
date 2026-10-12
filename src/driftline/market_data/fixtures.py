"""Deterministic synthetic fixtures — for automated tests ONLY.

Never present fixture-derived numbers as market performance. The backtest
engine labels fixture runs explicitly via the ``data_source=synthetic_fixture``
assumption.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from driftline.domain.models import Candle


def make_trend_candles(
    n: int = 120,
    *,
    start_price: float = 100.0,
    drift: float = 0.5,
    amplitude: float = 1.0,
    start: datetime | None = None,
) -> list[Candle]:
    """Gently rising series with a sinusoidal wiggle (deterministic)."""
    t0 = start or datetime(2024, 1, 1, tzinfo=UTC)
    candles: list[Candle] = []
    price = start_price
    for i in range(n):
        price = max(1.0, price + drift + math.sin(i / 5.0) * amplitude * 0.2)
        o = price - 0.1
        c = price
        candles.append(
            Candle(
                ts=t0 + timedelta(hours=i),
                open=o,
                high=max(o, c) + 0.3,
                low=min(o, c) - 0.3,
                close=c,
                volume=100.0 + i,
            )
        )
    return candles


def make_oscillating_candles(n: int = 160) -> list[Candle]:
    """Sine-wave market that forces EMA crossovers both ways."""
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    candles: list[Candle] = []
    for i in range(n):
        c = 100.0 + 10.0 * math.sin(i / 8.0) + 0.05 * i
        o = c - 0.5 * math.sin(i / 3.0)
        candles.append(
            Candle(
                ts=t0 + timedelta(hours=i),
                open=o,
                high=max(o, c) + 0.4,
                low=min(o, c) - 0.4,
                close=c,
                volume=50.0,
            )
        )
    return candles
