"""Fair multi-strategy comparison on one identical data snapshot.

Fairness contract: identical candles, identical evaluation range, identical
capital/fees/slippage/execution, identical benchmark (buy-and-hold over the
same range), and an explicitly reported risk configuration. No universal
winner score — callers sort by individual metrics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from driftline.backtesting.engine import BacktestConfig, run_backtest
from driftline.domain.models import BacktestMetrics, Candle
from driftline.research.fingerprints import dataset_fingerprint
from driftline.research.warnings import (
    OVERFITTING_CAVEAT,
    multiple_comparison_warning,
    trade_count_warnings,
)
from driftline.risk.manager import RiskConfig
from driftline.strategies.registry import StrategyRegistry


@dataclass
class ComparisonEntry:
    strategy_id: str
    version: str
    params: dict[str, Any]
    metrics: BacktestMetrics
    num_signals: int
    num_trades: int
    warnings: list[str] = field(default_factory=list)


@dataclass
class ComparisonReport:
    generated_at: datetime
    symbol: str
    timeframe: str
    data_source: str
    fingerprint: str
    num_candles: int
    data_start: datetime
    data_end: datetime
    backtest_config: dict[str, Any]
    risk_config: dict[str, Any] | None
    entries: list[ComparisonEntry] = field(default_factory=list)
    benchmark_buy_and_hold_pct: float = 0.0
    warnings: list[str] = field(default_factory=list)


SORTABLE_METRICS = (
    "total_return_pct",
    "annualized_return_pct",
    "buy_and_hold_return_pct",
    "max_drawdown_pct",
    "sharpe_ratio",
    "sortino_ratio",
    "profit_factor",
    "win_rate_pct",
    "num_trades",
    "expectancy",
    "exposure_pct",
    "total_fees",
    "final_equity",
)


def sort_entries(
    entries: list[ComparisonEntry], *, metric: str, descending: bool = True
) -> list[ComparisonEntry]:
    """Sort comparison entries by one metric (None values sort last)."""
    if metric not in SORTABLE_METRICS:
        raise ValueError(f"unsortable metric: {metric!r} (choose from {SORTABLE_METRICS})")

    def key(entry: ComparisonEntry) -> tuple[int, float]:
        value = getattr(entry.metrics, metric)
        if metric == "num_trades":
            return (0, float(value))
        if value is None:
            return (1, 0.0)
        return (0, float(value))

    return sorted(entries, key=key, reverse=descending)


def compare_strategies(
    candles: list[Candle],
    candidates: list[dict[str, Any]],
    config: BacktestConfig,
    registry: StrategyRegistry,
    *,
    symbol: str = "",
    timeframe: str = "1h",
    data_source: str = "exchange",
    risk_config: RiskConfig | None = None,
) -> ComparisonReport:
    """Run every candidate on the identical candle snapshot with identical costs."""
    if not candidates:
        raise ValueError("comparison needs at least one strategy candidate")
    if len(candles) < 2:
        raise ValueError("comparison needs at least 2 candles")
    fingerprint = dataset_fingerprint(candles, symbol, timeframe)
    report = ComparisonReport(
        generated_at=datetime.now(UTC),
        symbol=symbol,
        timeframe=timeframe,
        data_source=data_source,
        fingerprint=fingerprint,
        num_candles=len(candles),
        data_start=candles[0].ts,
        data_end=candles[-1].ts,
        backtest_config={
            "initial_capital": config.initial_capital,
            "fee_rate": config.fee_rate,
            "slippage_rate": config.slippage_rate,
            "allocation": config.allocation,
        },
        risk_config=(
            None
            if risk_config is None
            else {
                "max_position_allocation": risk_config.max_position_allocation,
                "max_risk_per_trade": risk_config.max_risk_per_trade,
                "max_portfolio_drawdown": risk_config.max_portfolio_drawdown,
                "min_available_balance": risk_config.min_available_balance,
            }
        ),
    )
    if risk_config is None:
        report.warnings.append(
            "No pre-trade risk veto in this comparison; sizing by allocation with "
            "cash-sufficiency scaling."
        )
    multi = multiple_comparison_warning(len(candidates))
    if multi:
        report.warnings.append(multi)
    report.warnings.append(OVERFITTING_CAVEAT)

    for candidate in candidates:
        strategy_id = str(candidate.get("strategy_id", ""))
        params = dict(candidate.get("params", {}) or {})
        strategy = registry.create(strategy_id, params)
        normalized = registry.validate_params(strategy_id, params)
        signals = strategy.generate(candles)
        result = run_backtest(
            candles,
            signals,
            config,
            strategy_name=strategy_id,
            timeframe=timeframe,
            data_source=data_source,
            risk_config=risk_config,
        )
        version = getattr(strategy, "version", "1.0.0")
        entry_warnings = trade_count_warnings(result.metrics.num_trades, label=strategy_id)
        if result.equity_curve and result.equity_curve[-1].position_qty > 1e-12:
            entry_warnings.append(
                f"{strategy_id}: position open at end of evaluation — final equity "
                "is marked to last close, not a realized exit."
            )
        report.entries.append(
            ComparisonEntry(
                strategy_id=strategy_id,
                version=str(version),
                params=normalized,
                metrics=result.metrics,
                num_signals=len(signals),
                num_trades=len(result.trades),
                warnings=entry_warnings,
            )
        )
    if candles[0].close > 0:
        report.benchmark_buy_and_hold_pct = (
            (candles[-1].close - candles[0].close) / candles[0].close * 100.0
        )
    return report
