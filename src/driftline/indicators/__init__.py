"""Reusable indicator helpers."""

from __future__ import annotations

from driftline.domain.models import Candle

from .basic import atr, ema, rsi

__all__ = ["ema", "rsi", "atr", "compute_all"]


def compute_all(
    candles: list[Candle],
    *,
    fast: int = 20,
    slow: int = 50,
    rsi_period: int = 14,
    atr_period: int = 14,
) -> dict[str, list[float | None]]:
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    return {
        "fast_ema": ema(closes, fast),
        "slow_ema": ema(closes, slow),
        "rsi": rsi(closes, rsi_period),
        "atr": atr(highs, lows, closes, atr_period),
    }
