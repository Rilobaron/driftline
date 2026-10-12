"""Deterministic walk-forward validation.

Protocol per fold:
  1. Slice the TRAINING window (chronological, past only).
  2. Evaluate each candidate parameter set on training data only.
  3. Select by the predeclared objective (strictly-greater wins; grid order
     breaks ties deterministically).
  4. Freeze the winner; evaluate once on the subsequent unseen TEST window.
  5. Advance by `step` (test windows never overlap: `step >= test_window`
     is enforced, so aggregate ledgers never double-count a bar).

Capital convention (documented): test windows CHAIN capital — each test
window starts with the previous window's final equity. Open positions are
marked to close, not liquidated, so carried value assumes frictionless
flattening at fold boundaries. Per-fold ledgers are preserved for audit.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from itertools import product
from typing import Any

from driftline.backtesting.engine import BacktestConfig
from driftline.backtesting.metrics import compute_metrics
from driftline.domain.models import BacktestMetrics, Candle, EquityPoint, Trade
from driftline.research.fingerprints import dataset_fingerprint
from driftline.research.warnings import OVERFITTING_CAVEAT, trade_count_warnings
from driftline.research.windows import evaluate_window
from driftline.risk.manager import RiskConfig
from driftline.strategies.registry import StrategyRegistry

OBJECTIVES = (
    "total_return_pct",
    "sharpe_ratio",
    "sortino_ratio",
    "profit_factor",
    "expectancy",
    "win_rate_pct",
)

MAX_CANDIDATES = 64


@dataclass
class WalkForwardConfig:
    train_window: int = 3000
    test_window: int = 750
    step: int = 750
    expanding: bool = False
    objective: str = "total_return_pct"
    min_trades: int = 3
    max_folds: int = 12

    def __post_init__(self) -> None:
        if self.train_window <= 0 or self.test_window <= 0 or self.step <= 0:
            raise ValueError("windows and step must be positive")
        if self.step < self.test_window:
            raise ValueError(
                f"step ({self.step}) must be >= test_window ({self.test_window}) "
                "to prevent overlapping test windows and double counting"
            )
        if self.objective not in OBJECTIVES:
            raise ValueError(f"unknown objective {self.objective!r} (choose from {OBJECTIVES})")
        if self.min_trades < 0:
            raise ValueError("min_trades must be >= 0")
        if self.max_folds <= 0:
            raise ValueError("max_folds must be positive")


@dataclass
class WalkForwardFold:
    fold: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    status: str = "ok"  # "ok" | "skipped"
    selected_params: dict[str, Any] = field(default_factory=dict)
    train_objective_value: float | None = None
    num_train_trades: int = 0
    num_test_trades: int = 0
    test_metrics: BacktestMetrics | None = None
    test_trades: list[Trade] = field(default_factory=list)
    test_equity: list[EquityPoint] = field(default_factory=list)
    num_candidates: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class WalkForwardReport:
    generated_at: datetime
    strategy_id: str
    version: str
    symbol: str
    timeframe: str
    data_source: str
    fingerprint: str
    config: WalkForwardConfig
    capital_convention: str = (
        "carry-forward: each test window starts with the previous window's final "
        "equity (open positions marked to close); per-fold ledgers preserved."
    )
    folds: list[WalkForwardFold] = field(default_factory=list)
    aggregate_metrics: BacktestMetrics | None = None
    aggregate_trades: list[Trade] = field(default_factory=list)
    aggregate_equity: list[EquityPoint] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def expand_grid(param_grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Deterministic grid expansion (insertion order preserved)."""
    if not param_grid:
        raise ValueError("param_grid must contain at least one parameter")
    for key, values in param_grid.items():
        if not isinstance(values, list) or not values:
            raise ValueError(f"param_grid[{key!r}] must be a non-empty list")
    combos = [
        dict(zip(param_grid.keys(), combo, strict=True)) for combo in product(*param_grid.values())
    ]
    if len(combos) > MAX_CANDIDATES:
        raise ValueError(
            f"param_grid expands to {len(combos)} candidates (limit {MAX_CANDIDATES}); "
            "narrow the search space"
        )
    return combos


def _objective_value(metrics: BacktestMetrics, objective: str) -> float:
    value = getattr(metrics, objective)
    if value is None:
        return float("-inf")
    return float(value)


def run_walk_forward(
    candles: list[Candle],
    strategy_id: str,
    param_grid: dict[str, list[Any]],
    config: BacktestConfig,
    wf_config: WalkForwardConfig,
    registry: StrategyRegistry,
    *,
    symbol: str = "",
    timeframe: str = "1h",
    data_source: str = "exchange",
    risk_config: RiskConfig | None = None,
) -> WalkForwardReport:
    n = len(candles)
    combos = expand_grid(param_grid)
    # Validate every candidate up front (fail fast on invalid search spaces).
    for combo in combos:
        registry.validate_params(strategy_id, combo)
    probe = registry.create(strategy_id, None)
    version = str(getattr(probe, "version", "1.0.0"))

    report = WalkForwardReport(
        generated_at=datetime.now(UTC),
        strategy_id=strategy_id,
        version=version,
        symbol=symbol,
        timeframe=timeframe,
        data_source=data_source,
        fingerprint=dataset_fingerprint(candles, symbol, timeframe),
        config=wf_config,
    )
    report.warnings.append(OVERFITTING_CAVEAT)
    report.warnings.append(
        "Walk-forward selects parameters on past training windows only; test windows "
        "are unseen at selection time. Aggregate performance is out-of-sample but "
        "still historical — not a guarantee of future results."
    )

    fold_idx = 0
    test_start = wf_config.train_window
    running_equity = config.initial_capital
    while test_start + wf_config.test_window <= n and fold_idx < wf_config.max_folds:
        train_start = 0 if wf_config.expanding else test_start - wf_config.train_window
        train_end = test_start
        test_end = test_start + wf_config.test_window
        fold = WalkForwardFold(
            fold=fold_idx,
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
            num_candidates=len(combos),
        )
        # --- selection on TRAINING data only ---
        best_params: dict[str, Any] | None = None
        best_value = float("-inf")
        best_trades = 0
        for combo in combos:
            candidate = registry.create(strategy_id, combo)
            train_eval = evaluate_window(
                candles,
                candidate,
                config,
                eval_start=train_start,
                eval_end=train_end,
                strategy_id=strategy_id,
                timeframe=timeframe,
                data_source=data_source,
                risk_config=risk_config,
            )
            if train_eval.metrics.num_trades < wf_config.min_trades:
                continue
            value = _objective_value(train_eval.metrics, wf_config.objective)
            if value > best_value:  # strict: first candidate wins ties
                best_value = value
                best_params = registry.validate_params(strategy_id, combo)
                best_trades = train_eval.metrics.num_trades
        if best_params is None:
            fold.status = "skipped"
            fold.warnings.append(
                f"fold {fold_idx}: no candidate reached min_trades={wf_config.min_trades} "
                "on the training window — test evaluation skipped (no parameters frozen)."
            )
            report.folds.append(fold)
            test_start += wf_config.step
            fold_idx += 1
            continue
        fold.selected_params = best_params
        fold.train_objective_value = best_value
        fold.num_train_trades = best_trades

        # --- frozen evaluation on the unseen TEST window ---
        winner = registry.create(strategy_id, best_params)
        test_config = replace(config, initial_capital=running_equity)
        test_eval = evaluate_window(
            candles,
            winner,
            test_config,
            eval_start=test_start,
            eval_end=test_end,
            strategy_id=strategy_id,
            timeframe=timeframe,
            data_source=data_source,
            risk_config=risk_config,
        )
        fold.num_test_trades = test_eval.metrics.num_trades
        fold.test_metrics = test_eval.metrics
        fold.test_trades = test_eval.trades
        # Aggregate only test-range points (drop the warmup prefix overlap).
        cutoff = candles[test_start].ts
        fold.test_equity = [p for p in test_eval.equity_curve if p.timestamp >= cutoff]
        fold.warnings += test_eval.warnings
        fold.warnings += trade_count_warnings(
            test_eval.metrics.num_trades, label=f"fold {fold_idx} test"
        )
        running_equity = test_eval.metrics.final_equity
        report.folds.append(fold)
        test_start += wf_config.step
        fold_idx += 1

    if not [f for f in report.folds if f.status == "ok"]:
        report.warnings.append("no fold completed successfully — aggregate metrics unavailable.")
        return report

    # --- aggregate from the chronological out-of-sample ledger (not averaged returns) ---
    agg_equity: list[EquityPoint] = []
    agg_trades: list[Trade] = []
    agg_closes: list[float] = []
    for fold in report.folds:
        if fold.status != "ok":
            continue
        agg_equity.extend(fold.test_equity)
        agg_trades.extend(fold.test_trades)
        agg_closes.extend(c.close for c in candles[fold.test_start : fold.test_end])
    bars_in_market = sum(1 for p in agg_equity if p.position_qty > 1e-12)
    aggregate = compute_metrics(
        initial_capital=config.initial_capital,
        equity_curve=agg_equity,
        trades=agg_trades,
        total_fees=sum(t.entry_fee + t.exit_fee for t in agg_trades),
        closes=agg_closes,
        bars_in_market=bars_in_market,
        total_bars=len(agg_equity),
        data_source=data_source,
        strategy_name=f"{strategy_id}:walk_forward",
        timeframe=timeframe,
    )
    aggregate.assumptions.append(
        "Walk-forward aggregate: concatenated non-overlapping out-of-sample test windows "
        f"({sum(1 for f in report.folds if f.status == 'ok')} folds); capital carried "
        "forward between windows with positions marked at boundaries."
    )
    report.aggregate_metrics = aggregate
    report.aggregate_trades = agg_trades
    report.aggregate_equity = agg_equity
    return report
