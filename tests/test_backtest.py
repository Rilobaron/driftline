"""Backtest engine: timing, fees/slippage, reproducibility, no overspend."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from driftline.backtesting.engine import BacktestConfig, run_backtest
from driftline.domain.models import Candle, Signal, SignalType
from driftline.market_data.fixtures import make_oscillating_candles, make_trend_candles
from driftline.strategies.ema_crossover import EmaCrossoverStrategy


def _flat_candles(n: int = 10, price: float = 100.0) -> list[Candle]:
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        Candle(
            ts=t0 + timedelta(hours=i),
            open=price,
            high=price + 1,
            low=price - 1,
            close=price,
            volume=10.0,
        )
        for i in range(n)
    ]


def test_next_open_execution_timing() -> None:
    """Entry signal on candle i fills at candle i+1 open, not at signal close."""
    candles = _flat_candles(5, 100.0)
    # Make next-open distinctive: candle 2 opens far from candle 1 close.
    candles[2] = candles[2].model_copy(update={"open": 120.0})
    sig = Signal(index=1, timestamp=candles[1].ts, signal=SignalType.ENTRY, close=100.0)
    res = run_backtest(
        candles,
        [sig],
        BacktestConfig(initial_capital=10_000.0, fee_rate=0.0, slippage_rate=0.0, allocation=1.0),
    )
    assert res.trades == []  # no exit yet
    assert res.equity_curve[2].position_qty == pytest.approx(10_000.0 / 120.0)


def test_fee_and_slippage_applied() -> None:
    import pytest

    candles = _flat_candles(4, 100.0)
    sig = Signal(index=0, timestamp=candles[0].ts, signal=SignalType.ENTRY, close=100.0)
    res = run_backtest(
        candles,
        [sig],
        BacktestConfig(initial_capital=10_000.0, fee_rate=0.01, slippage_rate=0.01, allocation=1.0),
    )
    eff = 100.0 * 1.01
    qty = 10_000.0 / (eff * 1.01)
    assert res.equity_curve[1].position_qty == pytest.approx(qty)
    assert res.metrics.total_fees == pytest.approx(qty * eff * 0.01)


def test_no_overspend_and_single_position() -> None:
    candles = _flat_candles(6, 100.0)
    sigs = [
        Signal(index=0, timestamp=candles[0].ts, signal=SignalType.ENTRY, close=100.0),
        Signal(index=1, timestamp=candles[1].ts, signal=SignalType.ENTRY, close=100.0),
    ]
    res = run_backtest(
        candles,
        sigs,
        BacktestConfig(initial_capital=1_000.0, fee_rate=0.0, slippage_rate=0.0, allocation=1.0),
    )
    assert all(p.cash >= -1e-6 for p in res.equity_curve)
    # Second entry ignored while invested: position equals single fill.
    import pytest as _pt

    assert res.equity_curve[2].position_qty == _pt.approx(10.0)


def test_exit_realizes_pnl_and_trade_ledger() -> None:
    import pytest

    candles = _flat_candles(5, 100.0)
    candles[3] = candles[3].model_copy(update={"open": 110.0})
    sigs = [
        Signal(index=0, timestamp=candles[0].ts, signal=SignalType.ENTRY, close=100.0),
        Signal(index=2, timestamp=candles[2].ts, signal=SignalType.EXIT, close=100.0),
    ]
    res = run_backtest(
        candles,
        sigs,
        BacktestConfig(initial_capital=10_000.0, fee_rate=0.0, slippage_rate=0.0, allocation=1.0),
    )
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.entry_price == pytest.approx(100.0)
    assert t.exit_price == pytest.approx(110.0)
    assert t.realized_pnl == pytest.approx((110.0 - 100.0) * 100.0)


def test_reproducibility_and_metrics() -> None:
    candles = make_oscillating_candles(n=200)
    signals = EmaCrossoverStrategy().generate(candles)
    cfg = BacktestConfig(initial_capital=10_000.0, fee_rate=0.001, slippage_rate=0.0005)
    r1 = run_backtest(candles, signals, cfg, data_source="synthetic_fixture")
    r2 = run_backtest(candles, signals, cfg, data_source="synthetic_fixture")
    assert r1.metrics == r2.metrics
    assert [t.model_dump() for t in r1.trades] == [t.model_dump() for t in r2.trades]
    assert r1.metrics.num_trades >= 1
    assert r1.metrics.max_drawdown_pct >= 0
    assert any("next candle open" in a for a in r1.metrics.assumptions)
    assert any("synthetic fixture" in a for a in r1.metrics.assumptions)


def test_trend_run_reports_buy_and_hold() -> None:
    candles = make_trend_candles(n=120)
    signals = EmaCrossoverStrategy().generate(candles)
    res = run_backtest(candles, signals, BacktestConfig(), data_source="synthetic_fixture")
    assert res.metrics.buy_and_hold_return_pct > 0
    assert res.metrics.final_equity > 0
