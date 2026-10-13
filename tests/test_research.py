"""Research layer: comparison, splits, walk-forward, fingerprints, experiments."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from driftline.backtesting.engine import BacktestConfig
from driftline.market_data.fixtures import make_oscillating_candles
from driftline.persistence.models import ExperimentRow
from driftline.persistence.repository import (
    get_experiment,
    list_experiments,
    save_experiment,
)
from driftline.research.comparison import compare_strategies, sort_entries
from driftline.research.experiments import build_experiment
from driftline.research.fingerprints import dataset_fingerprint
from driftline.research.splits import run_split_test
from driftline.research.walk_forward import (
    WalkForwardConfig,
    expand_grid,
    run_walk_forward,
)
from driftline.research.windows import evaluate_window
from driftline.strategies.registry import default_registry

CONFIG = BacktestConfig(initial_capital=10_000.0, fee_rate=0.001, slippage_rate=0.0005)


def test_dataset_fingerprint_stable_and_sensitive() -> None:
    candles = make_oscillating_candles(n=100)
    a = dataset_fingerprint(candles, "BTC/USDT", "1h")
    b = dataset_fingerprint(candles, "BTC/USDT", "1h")
    assert a == b and len(a) == 64
    altered = list(candles)
    altered[10] = altered[10].model_copy(update={"close": altered[10].close + 1.0})
    assert dataset_fingerprint(altered, "BTC/USDT", "1h") != a
    assert dataset_fingerprint(candles, "ETH/USDT", "1h") != a


def test_comparison_reproducible_and_fair() -> None:
    candles = make_oscillating_candles(n=400)
    candidates = [
        {"strategy_id": "ema_crossover", "params": {}},
        {"strategy_id": "rsi_mean_reversion", "params": {}},
        {"strategy_id": "donchian_breakout", "params": {}},
    ]
    reg = default_registry()
    r1 = compare_strategies(candles, candidates, CONFIG, reg, symbol="BTC/USDT")
    r2 = compare_strategies(candles, candidates, CONFIG, reg, symbol="BTC/USDT")
    assert r1.fingerprint == r2.fingerprint
    assert [e.strategy_id for e in r1.entries] == [c["strategy_id"] for c in candidates]
    for e1, e2 in zip(r1.entries, r2.entries, strict=True):
        assert e1.metrics == e2.metrics
        assert e1.params == e2.params
    # Identical shared assumptions echoed in the report.
    assert r1.backtest_config == {
        "initial_capital": 10_000.0,
        "fee_rate": 0.001,
        "slippage_rate": 0.0005,
        "allocation": 1.0,
    }
    assert any("selection bias" in w for w in r1.warnings)


def test_comparison_zero_trade_metrics_not_misleading() -> None:
    from datetime import UTC, datetime, timedelta

    from driftline.domain.models import Candle

    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    flat = [
        Candle(
            ts=t0 + timedelta(hours=i),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=10.0,
        )
        for i in range(200)
    ]
    report = compare_strategies(
        flat, [{"strategy_id": "ema_crossover", "params": {}}], CONFIG, default_registry()
    )
    entry = report.entries[0]
    assert entry.metrics.num_trades == 0
    assert entry.metrics.profit_factor is None
    assert entry.metrics.sharpe_ratio is None
    assert entry.metrics.sortino_ratio is None
    assert entry.metrics.win_rate_pct is None
    assert entry.metrics.expectancy is None
    assert any("zero completed trades" in w for w in entry.warnings)


def test_comparison_sort_entries() -> None:
    candles = make_oscillating_candles(n=400)
    report = compare_strategies(
        candles,
        [
            {"strategy_id": "ema_crossover", "params": {}},
            {"strategy_id": "donchian_breakout", "params": {}},
        ],
        CONFIG,
        default_registry(),
    )
    ranked = sort_entries(report.entries, metric="total_return_pct")
    returns = [e.metrics.total_return_pct for e in ranked]
    assert returns == sorted(returns, reverse=True)
    with pytest.raises(ValueError, match="unsortable metric"):
        sort_entries(report.entries, metric="vibes")


def test_sort_entries_none_metrics_always_last() -> None:
    from driftline.domain.models import BacktestMetrics
    from driftline.research.comparison import ComparisonEntry

    def entry(sharpe: float | None, total: float) -> ComparisonEntry:
        return ComparisonEntry(
            strategy_id="s",
            version="1.0.0",
            params={},
            metrics=BacktestMetrics(
                initial_capital=10000.0,
                final_equity=10000.0 + total * 100,
                total_return_pct=total,
                buy_and_hold_return_pct=0.0,
                max_drawdown_pct=0.0,
                num_trades=1,
                sharpe_ratio=sharpe,
                total_fees=0.0,
            ),
            num_signals=0,
            num_trades=1,
        )

    entries = [entry(None, 5.0), entry(2.0, 1.0), entry(1.0, 2.0)]
    desc = sort_entries(entries, metric="sharpe_ratio", descending=True)
    assert [e.metrics.total_return_pct for e in desc] == [1.0, 2.0, 5.0]
    asc = sort_entries(entries, metric="sharpe_ratio", descending=False)
    assert [e.metrics.total_return_pct for e in asc] == [2.0, 1.0, 5.0]


def test_comparison_rejects_unknown_strategy() -> None:
    with pytest.raises(ValueError, match="unknown strategy"):
        compare_strategies(
            make_oscillating_candles(n=100),
            [{"strategy_id": "ghost", "params": {}}],
            CONFIG,
            default_registry(),
        )


def test_split_chronology_and_capital_reset() -> None:
    candles = make_oscillating_candles(n=500)
    report = run_split_test(
        candles, "ema_crossover", None, CONFIG, default_registry(), train_ratio=0.7
    )
    assert report.split_index == 350
    assert report.capital_reset_between_windows is True
    assert report.in_sample is not None and report.out_sample is not None
    assert (report.in_sample.eval_start, report.in_sample.eval_end) == (0, 350)
    assert (report.out_sample.eval_start, report.out_sample.eval_end) == (350, 500)
    assert report.in_sample.metrics.initial_capital == report.out_sample.metrics.initial_capital


def test_split_out_of_sample_isolation() -> None:
    """No out-of-sample trade may reference pre-split bars; IS trades stay inside."""
    candles = make_oscillating_candles(n=500)
    report = run_split_test(
        candles, "donchian_breakout", None, CONFIG, default_registry(), train_ratio=0.7
    )
    assert report.in_sample is not None and report.out_sample is not None
    for t in report.in_sample.trades:
        assert 0 <= t.entry_index < t.exit_index <= 350
    for t in report.out_sample.trades:
        assert 350 <= t.entry_index < t.exit_index <= 500
    # Buy-and-hold measured on each window, not the full span.
    assert report.in_sample.metrics.buy_and_hold_return_pct != pytest.approx(
        report.out_sample.metrics.buy_and_hold_return_pct
    )


def test_split_insufficient_history_rejected() -> None:
    with pytest.raises(ValueError, match="insufficient history"):
        run_split_test(
            make_oscillating_candles(n=150),
            "ema_crossover",
            None,
            CONFIG,
            default_registry(),
            train_ratio=0.7,
        )
    with pytest.raises(ValueError, match="train_ratio"):
        run_split_test(
            make_oscillating_candles(n=500),
            "ema_crossover",
            None,
            CONFIG,
            default_registry(),
            train_ratio=0.95,
        )


def test_split_gap_warning() -> None:
    candles = make_oscillating_candles(n=500)
    gapped = [c for i, c in enumerate(candles) if i != 400]  # hole in OOS half
    report = run_split_test(
        gapped, "ema_crossover", None, CONFIG, default_registry(), train_ratio=0.7
    )
    assert report.out_sample is not None
    assert report.out_sample.missing_candles == 1
    assert any("missing" in w for w in report.out_sample.warnings)


def test_walk_forward_fold_boundaries_no_overlap() -> None:
    candles = make_oscillating_candles(n=1200)
    wf = WalkForwardConfig(train_window=500, test_window=200, step=200, min_trades=0)
    report = run_walk_forward(
        candles,
        "ema_crossover",
        {"fast_period": [10, 20], "slow_period": [30, 50]},
        CONFIG,
        wf,
        default_registry(),
    )
    tests = [(f.test_start, f.test_end) for f in report.folds if f.status == "ok"]
    assert len(tests) >= 2
    for (_s1, e1), (s2, _e2) in zip(tests, tests[1:], strict=False):
        assert e1 <= s2, "test windows must not overlap"
    assert all(f.train_end == f.test_start for f in report.folds)


def test_walk_forward_selection_uses_training_only() -> None:
    """Frozen test params must equal the training-window winner, whatever it is."""
    candles = make_oscillating_candles(n=1200)
    grid = {"fast_period": [5, 20], "slow_period": [30, 60]}
    wf = WalkForwardConfig(train_window=500, test_window=200, step=200, min_trades=0)
    report = run_walk_forward(candles, "ema_crossover", grid, CONFIG, wf, default_registry())
    reg = default_registry()
    for fold in report.folds:
        if fold.status != "ok":
            continue
        # Recompute every candidate's training objective independently.
        scored: dict[tuple[float, float], float] = {}
        for combo in (
            {"fast_period": f, "slow_period": s}
            for f in grid["fast_period"]
            for s in grid["slow_period"]
        ):
            strat = reg.create("ema_crossover", combo)
            ev = evaluate_window(
                candles, strat, CONFIG, eval_start=fold.train_start, eval_end=fold.train_end
            )
            scored[(combo["fast_period"], combo["slow_period"])] = ev.metrics.total_return_pct
        best = max(scored.items(), key=lambda kv: kv[1])[0]
        assert (fold.selected_params["fast_period"], fold.selected_params["slow_period"]) == best


def test_walk_forward_deterministic_and_bounded() -> None:
    candles = make_oscillating_candles(n=1200)
    grid = {"fast_period": [10, 20], "slow_period": [40, 60]}
    wf = WalkForwardConfig(train_window=500, test_window=200, step=200, min_trades=0)
    r1 = run_walk_forward(candles, "ema_crossover", grid, CONFIG, wf, default_registry())
    r2 = run_walk_forward(candles, "ema_crossover", grid, CONFIG, wf, default_registry())
    assert r1.fingerprint == r2.fingerprint
    assert r1.aggregate_metrics == r2.aggregate_metrics
    with pytest.raises(ValueError, match="step"):
        WalkForwardConfig(train_window=500, test_window=200, step=100)
    with pytest.raises(ValueError, match="limit 64"):
        expand_grid({"a": [1, 2], "b": list(range(40))})
    with pytest.raises(ValueError, match="at least one parameter"):
        expand_grid({})


def test_walk_forward_min_trades_skip_and_aggregate_ledger() -> None:
    candles = make_oscillating_candles(n=1200)
    grid = {"fast_period": [10], "slow_period": [40]}
    wf = WalkForwardConfig(train_window=500, test_window=200, step=200, min_trades=10_000)
    report = run_walk_forward(candles, "ema_crossover", grid, CONFIG, wf, default_registry())
    assert all(f.status == "skipped" for f in report.folds)
    assert report.aggregate_metrics is None

    wf2 = WalkForwardConfig(train_window=500, test_window=200, step=200, min_trades=0)
    ok_report = run_walk_forward(candles, "ema_crossover", grid, CONFIG, wf2, default_registry())
    assert ok_report.aggregate_metrics is not None
    # Aggregate built from the chronological ledger, not averaged returns.
    assert len(ok_report.aggregate_trades) == sum(
        len(f.test_trades) for f in ok_report.folds if f.status == "ok"
    )
    stamps = [p.timestamp for p in ok_report.aggregate_equity]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)


def test_walk_forward_expanding_windows() -> None:
    candles = make_oscillating_candles(n=1200)
    wf = WalkForwardConfig(
        train_window=400, test_window=200, step=200, expanding=True, min_trades=0
    )
    report = run_walk_forward(
        candles,
        "donchian_breakout",
        {"entry_lookback": [10, 20], "exit_lookback": [5, 10]},
        CONFIG,
        wf,
        default_registry(),
    )
    starts = [f.train_start for f in report.folds]
    assert all(s == 0 for s in starts)
    ends = [f.train_end for f in report.folds]
    assert ends == sorted(ends)


def test_experiment_record_and_persistence(db_session: Session) -> None:
    candles = make_oscillating_candles(n=200)
    record = build_experiment(
        methodology="split",
        strategy_id="ema_crossover",
        strategy_params={"fast_period": 20, "slow_period": 50},
        symbol="BTC/USDT",
        timeframe="1h",
        data_provider="synthetic_fixture",
        num_candles=len(candles),
        fingerprint=dataset_fingerprint(candles, "BTC/USDT", "1h"),
        backtest_config={"initial_capital": 10_000.0},
        results={"num_trades": 3},
        warnings=["demo"],
    )
    assert record.experiment_id and record.created_at and record.git_commit is not None
    save_experiment(
        db_session,
        ExperimentRow(
            id=record.experiment_id,
            created_at=record.created_at,
            methodology=record.methodology,
            strategy_id=record.strategy_id,
            record=record.model_dump(mode="json"),
        ),
    )
    fetched = get_experiment(db_session, record.experiment_id)
    assert fetched is not None
    assert fetched.record["fingerprint"] == record.fingerprint  # type: ignore[index]
    assert get_experiment(db_session, "missing") is None
    listed = list_experiments(db_session, limit=10)
    assert [r.id for r in listed] == [record.experiment_id]


def test_evaluate_window_rejects_bad_ranges() -> None:
    from driftline.strategies.registry import create_strategy

    candles = make_oscillating_candles(n=200)
    strat = create_strategy("ema_crossover", None)
    with pytest.raises(ValueError, match="invalid evaluation window"):
        evaluate_window(candles, strat, CONFIG, eval_start=100, eval_end=50)
    with pytest.raises(ValueError, match="insufficient pre-window history"):
        evaluate_window(candles, strat, CONFIG, eval_start=10, eval_end=150)
