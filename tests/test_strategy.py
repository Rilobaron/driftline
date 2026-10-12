"""EMA crossover signals, warmup, and lookahead prevention."""

from __future__ import annotations

from driftline.domain.models import SignalType
from driftline.market_data.fixtures import make_oscillating_candles, make_trend_candles
from driftline.strategies.ema_crossover import EmaCrossoverParams, EmaCrossoverStrategy


def test_no_signals_before_warmup() -> None:
    candles = make_trend_candles(n=60)
    strategy = EmaCrossoverStrategy(EmaCrossoverParams(fast_period=20, slow_period=50))
    signals = strategy.generate(candles)
    assert all(s.index >= strategy.warmup_bars for s in signals)


def test_wave_market_produces_both_signal_types() -> None:
    candles = make_oscillating_candles(n=200)
    strategy = EmaCrossoverStrategy()
    signals = strategy.generate(candles)
    kinds = {s.signal for s in signals}
    assert SignalType.ENTRY in kinds and SignalType.EXIT in kinds


def test_signal_carries_indicator_context() -> None:
    candles = make_oscillating_candles(n=200)
    signals = EmaCrossoverStrategy().generate(candles)
    assert signals
    s = signals[0]
    assert s.fast_ema is not None and s.slow_ema is not None
    assert s.timestamp == candles[s.index].ts
    assert s.context["strategy"] == "ema_crossover"


def test_no_lookahead_signal_index_matches_candle() -> None:
    """Signal at index i must equal the crossover computed from data <= i."""
    candles = make_oscillating_candles(n=200)
    strategy = EmaCrossoverStrategy(EmaCrossoverParams(5, 10, 14))
    signals = strategy.generate(candles)
    # Recompute crossover manually using only data up to each index.
    from driftline.indicators.basic import ema as ema_fn

    closes = [c.close for c in candles]
    fast = ema_fn(closes, 5)
    slow = ema_fn(closes, 10)
    expected: list[tuple[int, SignalType]] = []
    for i in range(1, len(candles)):
        if i < 10 or fast[i] is None or slow[i] is None:
            continue
        assert fast[i - 1] is not None and slow[i - 1] is not None
        prev_d = fast[i - 1] - slow[i - 1]  # type: ignore[operator]
        now_d = fast[i] - slow[i]  # type: ignore[operator]
        if prev_d <= 0 < now_d:
            expected.append((i, SignalType.ENTRY))
        elif prev_d >= 0 > now_d:
            expected.append((i, SignalType.EXIT))
    assert [(s.index, s.signal) for s in signals] == expected


def test_invalid_params_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        EmaCrossoverParams(fast_period=50, slow_period=20)
