"""Strategy Lab: registry, parameter validation, new-strategy behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from driftline.backtesting.engine import BacktestConfig, run_backtest
from driftline.domain.models import Candle, SignalType
from driftline.market_data.fixtures import make_oscillating_candles, make_trend_candles
from driftline.strategies.donchian_breakout import (
    DonchianBreakoutParams,
    DonchianBreakoutStrategy,
)
from driftline.strategies.ema_crossover import EmaCrossoverStrategy
from driftline.strategies.registry import StrategyRegistry, create_strategy, list_strategies
from driftline.strategies.rsi_mean_reversion import (
    RsiMeanReversionParams,
    RsiMeanReversionStrategy,
)


def test_registry_lists_all_three_strategies() -> None:
    names = [s["name"] for s in list_strategies()]
    assert names == ["donchian_breakout", "ema_crossover", "rsi_mean_reversion"]


def test_registry_rejects_unknown_strategy() -> None:
    with pytest.raises(ValueError, match="unknown strategy"):
        create_strategy("nope")


def test_registry_rejects_unknown_params() -> None:
    with pytest.raises(ValueError, match="unknown parameters"):
        create_strategy("ema_crossover", {"fast_period": 10, "bogus": 1})


def test_registry_rejects_out_of_range_params() -> None:
    with pytest.raises(ValueError):
        create_strategy("ema_crossover", {"fast_period": 1, "slow_period": 50})


def test_registry_fills_defaults() -> None:
    from driftline.strategies.registry import default_registry

    normalized = default_registry().validate_params("rsi_mean_reversion", {})
    assert normalized == {"rsi_period": 14, "oversold": 30.0, "exit_threshold": 50.0}


def test_registry_duplicate_registration_rejected() -> None:
    reg = StrategyRegistry()
    reg.register(EmaCrossoverStrategy)
    with pytest.raises(ValueError, match="already registered"):
        reg.register(EmaCrossoverStrategy)


def test_ema_baseline_defaults_preserved() -> None:
    strat = create_strategy("ema_crossover", None)
    assert isinstance(strat, EmaCrossoverStrategy)
    assert strat.params.fast_period == 20
    assert strat.params.slow_period == 50
    assert strat.warmup_bars == 50
    assert strat.version == "1.0.0"


def test_ema_param_validation() -> None:
    with pytest.raises(ValueError):
        create_strategy("ema_crossover", {"fast_period": 50, "slow_period": 20})
    with pytest.raises(ValueError):
        create_strategy("ema_crossover", {"fast_period": -5, "slow_period": 20})


def test_rsi_param_validation() -> None:
    with pytest.raises(ValueError, match="below exit_threshold"):
        RsiMeanReversionParams(rsi_period=14, oversold=60.0, exit_threshold=50.0)
    with pytest.raises(ValueError, match="within"):
        RsiMeanReversionParams(rsi_period=14, oversold=0.0, exit_threshold=50.0)
    with pytest.raises(ValueError):
        RsiMeanReversionParams(rsi_period=1)
    assert RsiMeanReversionStrategy().warmup_bars == 15


def test_donchian_param_validation() -> None:
    with pytest.raises(ValueError):
        DonchianBreakoutParams(entry_lookback=1, exit_lookback=10)
    assert DonchianBreakoutStrategy().warmup_bars == 20


def _rsi_scenario() -> list[Candle]:
    """Rally (RSI high) -> fall (cross oversold once) -> flat -> rally (cross exit)."""
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    closes: list[float] = []
    price = 100.0
    for _ in range(15):  # opening rally lifts RSI before the decline
        price *= 1.015
        closes.append(price)
    for _ in range(25):  # steady decline through the oversold threshold
        price *= 0.985
        closes.append(price)
    for _ in range(30):  # flat while oversold
        closes.append(price)
    for _ in range(25):  # rally back through the exit threshold
        price *= 1.02
        closes.append(price)
    candles = []
    for i, c in enumerate(closes):
        candles.append(
            Candle(
                ts=t0 + timedelta(hours=i),
                open=c,
                high=c + 0.2,
                low=c - 0.2,
                close=c,
                volume=10.0,
            )
        )
    return candles


def test_rsi_crossing_semantics_single_entry_while_oversold() -> None:
    candles = _rsi_scenario()
    signals = RsiMeanReversionStrategy().generate(candles)
    assert signals, "expected at least an entry in a falling market"
    assert signals[0].signal is SignalType.ENTRY
    entries = [s for s in signals if s.signal is SignalType.ENTRY]
    assert len(entries) == 1, "must not re-enter while RSI remains oversold"
    kinds = [s.signal for s in signals]
    assert kinds.count(SignalType.EXIT) <= 1
    # Alternation: entry always precedes its exit.
    if SignalType.EXIT in kinds:
        assert kinds.index(SignalType.ENTRY) < kinds.index(SignalType.EXIT)


def test_rsi_warmup_gates_signals() -> None:
    candles = _rsi_scenario()[:10]
    assert RsiMeanReversionStrategy().generate(candles) == []


def test_donchian_excludes_current_candle_from_channel() -> None:
    """A close above the *prior* channel must fire even though the candle's own
    high would raise a contaminated channel and suppress the signal."""
    t0 = datetime(2024, 1, 1, tzinfo=UTC)

    def bar(i: int, close: float, high: float, low: float) -> Candle:
        return Candle(
            ts=t0 + timedelta(hours=i),
            open=close,
            high=high,
            low=low,
            close=close,
            volume=10.0,
        )

    candles = [bar(i, 100.0, 100.0, 100.0) for i in range(10)]
    # Bar 10 closes at 105: prior 3-bar channel max is 100 -> ENTRY.
    # A contaminated channel including bar 10 would read 105 and suppress it.
    candles.append(bar(10, 105.0, 105.0, 104.0))
    # Bars 11..13 hold high; bar 14 breaks down through the prior low channel.
    candles += [
        bar(11, 106.0, 106.0, 105.0),
        bar(12, 106.0, 106.0, 105.0),
        bar(13, 106.0, 106.0, 105.0),
    ]
    candles.append(bar(14, 104.0, 105.0, 104.0))  # prior 10-low min is 105 -> EXIT
    strat = DonchianBreakoutStrategy(DonchianBreakoutParams(entry_lookback=3, exit_lookback=3))
    signals = strat.generate(candles)
    assert [(s.index, s.signal) for s in signals] == [
        (10, SignalType.ENTRY),
        (14, SignalType.EXIT),
    ]
    assert signals[0].context["channel_high"] == 100.0
    assert signals[1].context["channel_low"] == 105.0


def test_donchian_warmup_and_short_series() -> None:
    assert DonchianBreakoutStrategy().generate(make_trend_candles(n=10)) == []


def test_all_strategies_engine_compatible() -> None:
    candles = make_oscillating_candles(n=300)
    config = BacktestConfig()
    for strategy_id in ("ema_crossover", "rsi_mean_reversion", "donchian_breakout"):
        strategy = create_strategy(strategy_id, None)
        signals = strategy.generate(candles)
        result = run_backtest(
            candles, signals, config, strategy_name=strategy_id, data_source="synthetic_fixture"
        )
        assert result.metrics.num_trades == len(result.trades)
        assert all(s.timestamp == candles[s.index].ts for s in signals)
