"""JSON serialization for research reports (dashboard-ready dicts)."""

from __future__ import annotations

from typing import Any

from driftline.research.comparison import ComparisonReport
from driftline.research.splits import SplitReport
from driftline.research.walk_forward import WalkForwardReport
from driftline.research.windows import WindowEvaluation


def window_to_dict(w: WindowEvaluation, *, include_trades: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "eval_start": w.eval_start,
        "eval_end": w.eval_end,
        "num_bars": w.eval_end - w.eval_start,
        "warmup_prefix": w.warmup_prefix,
        "metrics": w.metrics.model_dump(mode="json"),
        "num_signals": w.num_signals,
        "num_trades": len(w.trades),
        "unfilled_signals": w.unfilled_signals,
        "missing_candles": w.missing_candles,
        "warnings": w.warnings,
    }
    if include_trades:
        out["trades"] = [t.model_dump(mode="json") for t in w.trades]
        out["equity"] = [e.model_dump(mode="json") for e in w.equity_curve]
    return out


def comparison_to_dict(report: ComparisonReport) -> dict[str, Any]:
    return {
        "generated_at": report.generated_at.isoformat(),
        "symbol": report.symbol,
        "timeframe": report.timeframe,
        "data_source": report.data_source,
        "fingerprint": report.fingerprint,
        "num_candles": report.num_candles,
        "data_start": report.data_start.isoformat(),
        "data_end": report.data_end.isoformat(),
        "backtest_config": report.backtest_config,
        "risk_config": report.risk_config,
        "benchmark_buy_and_hold_pct": report.benchmark_buy_and_hold_pct,
        "entries": [
            {
                "strategy_id": e.strategy_id,
                "version": e.version,
                "params": e.params,
                "metrics": e.metrics.model_dump(mode="json"),
                "num_signals": e.num_signals,
                "num_trades": e.num_trades,
                "warnings": e.warnings,
            }
            for e in report.entries
        ],
        "warnings": report.warnings,
    }


def split_to_dict(report: SplitReport) -> dict[str, Any]:
    assert report.in_sample is not None and report.out_sample is not None
    return {
        "generated_at": report.generated_at.isoformat(),
        "strategy_id": report.strategy_id,
        "version": report.version,
        "params": report.params,
        "symbol": report.symbol,
        "timeframe": report.timeframe,
        "data_source": report.data_source,
        "fingerprint": report.fingerprint,
        "train_ratio": report.train_ratio,
        "split_index": report.split_index,
        "capital_reset_between_windows": report.capital_reset_between_windows,
        "in_sample": window_to_dict(report.in_sample),
        "out_sample": window_to_dict(report.out_sample),
        "warnings": report.warnings,
    }


def walk_forward_to_dict(report: WalkForwardReport) -> dict[str, Any]:
    return {
        "generated_at": report.generated_at.isoformat(),
        "strategy_id": report.strategy_id,
        "version": report.version,
        "symbol": report.symbol,
        "timeframe": report.timeframe,
        "data_source": report.data_source,
        "fingerprint": report.fingerprint,
        "config": {
            "train_window": report.config.train_window,
            "test_window": report.config.test_window,
            "step": report.config.step,
            "expanding": report.config.expanding,
            "objective": report.config.objective,
            "min_trades": report.config.min_trades,
            "max_folds": report.config.max_folds,
        },
        "capital_convention": report.capital_convention,
        "folds": [
            {
                "fold": f.fold,
                "train_start": f.train_start,
                "train_end": f.train_end,
                "test_start": f.test_start,
                "test_end": f.test_end,
                "status": f.status,
                "selected_params": f.selected_params,
                "train_objective_value": f.train_objective_value,
                "num_train_trades": f.num_train_trades,
                "num_test_trades": f.num_test_trades,
                "num_candidates": f.num_candidates,
                "test_metrics": f.test_metrics.model_dump(mode="json")
                if f.test_metrics is not None
                else None,
                "warnings": f.warnings,
            }
            for f in report.folds
        ],
        "aggregate_metrics": report.aggregate_metrics.model_dump(mode="json")
        if report.aggregate_metrics is not None
        else None,
        "aggregate_trades": [t.model_dump(mode="json") for t in report.aggregate_trades],
        "aggregate_equity": [e.model_dump(mode="json") for e in report.aggregate_equity],
        "warnings": report.warnings,
    }
