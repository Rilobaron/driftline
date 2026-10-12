"""Repeatable Strategy Lab demonstration on locally stored market data.

Reads candles persisted by market-data sync (no network), runs a three-way
comparison, 70/30 chronological splits, and an EMA walk-forward experiment,
and prints the results. All figures are descriptive history, not predictions.

Usage:
    DATABASE_URL=sqlite:///./driftline.db uv run python scripts/research_demo.py
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./driftline.db")

from driftline.backtesting.engine import BacktestConfig
from driftline.persistence.database import get_session, init_db
from driftline.persistence.repository import load_candles
from driftline.research.comparison import compare_strategies, sort_entries
from driftline.research.splits import run_split_test
from driftline.research.walk_forward import WalkForwardConfig, run_walk_forward
from driftline.strategies.registry import default_registry

SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"
DATA_SOURCE = "exchange:binance"


def _fmt(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def main() -> None:
    init_db()
    session = next(get_session())
    candles = load_candles(session, SYMBOL, TIMEFRAME, limit=9000)
    print(f"dataset: {len(candles)} candles {candles[0].ts} -> {candles[-1].ts}")
    config = BacktestConfig()
    registry = default_registry()

    print("\n=== 1. comparison (identical snapshot, costs, benchmark) ===")
    report = compare_strategies(
        candles,
        [
            {"strategy_id": sid, "params": {}}
            for sid in ("ema_crossover", "rsi_mean_reversion", "donchian_breakout")
        ],
        config,
        registry,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        data_source=DATA_SOURCE,
    )
    print(f"fingerprint: {report.fingerprint}")
    print(f"benchmark buy-and-hold: {report.benchmark_buy_and_hold_pct:.2f}%")
    for entry in sort_entries(report.entries, metric="total_return_pct"):
        m = entry.metrics
        print(
            f"- {entry.strategy_id} v{entry.version} {entry.params}: "
            f"trades={m.num_trades} signals={entry.num_signals} "
            f"ret={m.total_return_pct:.2f}% ann={_fmt(m.annualized_return_pct)}% "
            f"dd={m.max_drawdown_pct:.2f}% sharpe={_fmt(m.sharpe_ratio)} "
            f"sortino={_fmt(m.sortino_ratio)} pf={_fmt(m.profit_factor)} "
            f"win={_fmt(m.win_rate_pct)}% expectancy={_fmt(m.expectancy)} "
            f"fees={m.total_fees:.0f} exposure={m.exposure_pct:.1f}%"
        )
        for warning in entry.warnings:
            print(f"    ! {warning}")
    for warning in report.warnings:
        print(f"! {warning}")

    print("\n=== 2. chronological 70/30 splits ===")
    for strategy_id in ("ema_crossover", "rsi_mean_reversion", "donchian_breakout"):
        split = run_split_test(
            candles,
            strategy_id,
            None,
            config,
            registry,
            symbol=SYMBOL,
            timeframe=TIMEFRAME,
            data_source=DATA_SOURCE,
        )
        assert split.in_sample is not None and split.out_sample is not None
        in_m, out_m = split.in_sample.metrics, split.out_sample.metrics
        print(
            f"- {strategy_id}: IS ret={in_m.total_return_pct:.2f}% "
            f"(n={in_m.num_trades}) / OOS ret={out_m.total_return_pct:.2f}% "
            f"(n={out_m.num_trades}); OOS BH={out_m.buy_and_hold_return_pct:.2f}%"
        )

    print("\n=== 3. walk-forward (EMA, rolling 1500/500, objective=total_return_pct) ===")
    wf_config = WalkForwardConfig(
        train_window=1500,
        test_window=500,
        step=500,
        objective="total_return_pct",
        min_trades=2,
    )
    wf = run_walk_forward(
        candles,
        "ema_crossover",
        {"fast_period": [10, 20, 30], "slow_period": [50, 100]},
        config,
        wf_config,
        registry,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        data_source=DATA_SOURCE,
    )
    ok_folds = [f for f in wf.folds if f.status == "ok"]
    print(f"folds ok={len(ok_folds)}/{len(wf.folds)} fingerprint={wf.fingerprint}")
    for fold in wf.folds:
        test_ret = fold.test_metrics.total_return_pct if fold.test_metrics else float("nan")
        print(
            f"- fold {fold.fold} [{fold.status}]: train[{fold.train_start},{fold.train_end}) "
            f"test[{fold.test_start},{fold.test_end}) params={fold.selected_params} "
            f"train_obj={_fmt(fold.train_objective_value)} test_ret={test_ret:.2f}% "
            f"test_trades={fold.num_test_trades}"
        )
    if wf.aggregate_metrics is not None:
        a = wf.aggregate_metrics
        print(
            f"aggregate OOS: ret={a.total_return_pct:.2f}% dd={a.max_drawdown_pct:.2f}% "
            f"sharpe={_fmt(a.sharpe_ratio)} pf={_fmt(a.profit_factor)} "
            f"trades={a.num_trades} bh={a.buy_and_hold_return_pct:.2f}%"
        )
    for warning in wf.warnings:
        print(f"! {warning}")


if __name__ == "__main__":
    main()
