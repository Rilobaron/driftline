"""Indicator correctness: EMA/RSI/ATR against hand-computed values."""

from __future__ import annotations

from driftline.indicators.basic import atr, ema, rsi


def test_ema_seed_and_recursion() -> None:
    values = [10.0, 11.0, 12.0, 13.0, 14.0]
    out = ema(values, 3)
    assert out[0] is None and out[1] is None
    assert out[2] == 11.0  # SMA(10,11,12)
    k = 2 / (3 + 1)
    assert out[3] == 13.0 * k + 11.0 * (1 - k)
    assert out[4] == 14.0 * k + out[3] * (1 - k)  # type: ignore[operator]


def test_ema_short_series_all_none() -> None:
    assert ema([1.0, 2.0], 5) == [None, None]


def test_rsi_all_gains_is_100() -> None:
    closes = [float(100 + i) for i in range(20)]
    out = rsi(closes, 14)
    assert out[:14] == [None] * 14
    assert out[14] == 100.0
    assert out[15] == 100.0


def test_rsi_flat_market_midpoint() -> None:
    closes = [100.0] * 20
    out = rsi(closes, 14)
    assert out[14] == 100.0  # no losses -> 100 by Wilder convention


def test_atr_constant_range() -> None:
    highs = [101.0] * 20
    lows = [99.0] * 20
    closes = [100.0] * 20
    out = atr(highs, lows, closes, 14)
    assert out[:14] == [None] * 14
    assert out[14] == 2.0
    assert out[15] == 2.0
