"""Chronological in-sample / out-of-sample evaluation.

Rules:
  * Candles are NEVER shuffled; the split point is chronological.
  * Out-of-sample data must not inform parameter selection — this module only
    evaluates fixed parameters on each partition. Selection happens elsewhere
    (walk-forward) or beforehand on in-sample data only.
  * Portfolio state is RESET between windows: each partition starts flat with
    the configured initial capital. (Walk-forward documents its own
    carry-forward convention separately.)
  * Each partition gets pre-period warmup history for indicator seeding, but
    no trades may occur before the partition start.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from driftline.backtesting.engine import BacktestConfig
from driftline.domain.models import Candle
from driftline.research.fingerprints import dataset_fingerprint
from driftline.research.warnings import OVERFITTING_CAVEAT, trade_count_warnings
from driftline.research.windows import WindowEvaluation, evaluate_window
from driftline.risk.manager import RiskConfig
from driftline.strategies.registry import StrategyRegistry


@dataclass
class SplitReport:
    generated_at: datetime
    strategy_id: str
    version: str
    params: dict[str, Any]
    symbol: str
    timeframe: str
    data_source: str
    fingerprint: str
    train_ratio: float
    split_index: int
    capital_reset_between_windows: bool = True
    in_sample: WindowEvaluation | None = None
    out_sample: WindowEvaluation | None = None
    warnings: list[str] = field(default_factory=list)


def run_split_test(
    candles: list[Candle],
    strategy_id: str,
    params: dict[str, Any] | None,
    config: BacktestConfig,
    registry: StrategyRegistry,
    *,
    train_ratio: float = 0.7,
    symbol: str = "",
    timeframe: str = "1h",
    data_source: str = "exchange",
    risk_config: RiskConfig | None = None,
    min_window_bars: int = 100,
) -> SplitReport:
    """Evaluate fixed parameters on chronological 70/30 (default) partitions."""
    if not 0.5 <= train_ratio <= 0.9:
        raise ValueError(f"train_ratio must be in [0.5, 0.9], got {train_ratio}")
    n = len(candles)
    split = int(n * train_ratio)
    if split < min_window_bars or n - split < min_window_bars:
        raise ValueError(
            f"insufficient history for split: {n} candles with ratio {train_ratio} "
            f"gives windows ({split}, {n - split}); need >={min_window_bars} each"
        )
    strategy = registry.create(strategy_id, params)
    normalized = registry.validate_params(strategy_id, params)
    version = str(getattr(strategy, "version", "1.0.0"))

    report = SplitReport(
        generated_at=datetime.now(UTC),
        strategy_id=strategy_id,
        version=version,
        params=normalized,
        symbol=symbol,
        timeframe=timeframe,
        data_source=data_source,
        fingerprint=dataset_fingerprint(candles, symbol, timeframe),
        train_ratio=train_ratio,
        split_index=split,
    )
    report.warnings.append(
        "Portfolio state is reset between windows: each partition starts flat with "
        "initial capital. In-sample and out-of-sample results are reported separately; "
        "do not re-select parameters on out-of-sample results and call them unbiased."
    )
    report.warnings.append(OVERFITTING_CAVEAT)

    in_eval = evaluate_window(
        candles,
        strategy,
        config,
        eval_start=0,
        eval_end=split,
        strategy_id=strategy_id,
        timeframe=timeframe,
        data_source=data_source,
        risk_config=risk_config,
    )
    out_eval = evaluate_window(
        candles,
        strategy,
        config,
        eval_start=split,
        eval_end=n,
        strategy_id=strategy_id,
        timeframe=timeframe,
        data_source=data_source,
        risk_config=risk_config,
    )
    in_eval.warnings += trade_count_warnings(in_eval.metrics.num_trades, label="in-sample")
    out_eval.warnings += trade_count_warnings(out_eval.metrics.num_trades, label="out-of-sample")
    report.in_sample = in_eval
    report.out_sample = out_eval
    return report
