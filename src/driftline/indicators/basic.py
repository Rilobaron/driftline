"""Deterministic technical indicators (pure functions, no lookahead)."""

from __future__ import annotations


def ema(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average.

    Convention: seed with the simple moving average of the first ``period``
    values, so ``result[period - 1]`` is the first defined value and all
    earlier entries are ``None``. Deterministic; uses only past data.
    """
    if period <= 0:
        raise ValueError("EMA period must be positive")
    n = len(values)
    out: list[float | None] = [None] * n
    if n < period or period == 0:
        return out
    if any(v != v for v in values):
        raise ValueError("EMA inputs must not contain NaN")
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1.0)
    prev = seed
    for i in range(period, n):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    """Wilder's RSI. First defined value at index ``period``."""
    if period <= 0:
        raise ValueError("RSI period must be positive")
    n = len(closes)
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period + 1, n):
        delta = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> list[float | None]:
    """Wilder's ATR. First defined value at index ``period``."""
    if period <= 0:
        raise ValueError("ATR period must be positive")
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("ATR inputs must have equal length")
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    trs: list[float] = []
    for i in range(n):
        if i == 0:
            trs.append(highs[0] - lows[0])
        else:
            trs.append(
                max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                )
            )
    atr_val = sum(trs[1 : period + 1]) / period
    out[period] = atr_val
    for i in range(period + 1, n):
        atr_val = (atr_val * (period - 1) + trs[i]) / period
        out[i] = atr_val
    return out
