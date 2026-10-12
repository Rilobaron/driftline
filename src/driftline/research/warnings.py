"""Shared research warnings (sample size, costs, selection bias)."""

from __future__ import annotations

MIN_TRADES_WARNING = 10
MIN_TRADES_SMALL_SAMPLE = 30


def trade_count_warnings(num_trades: int, *, label: str = "evaluation") -> list[str]:
    warnings: list[str] = []
    if num_trades == 0:
        warnings.append(
            f"{label}: zero completed trades — return/risk statistics are "
            "descriptive of inactivity, not evidence of edge."
        )
    elif num_trades < MIN_TRADES_WARNING:
        warnings.append(
            f"{label}: only {num_trades} trades (<{MIN_TRADES_WARNING}) — "
            "statistics are unstable; treat as exploratory only."
        )
    elif num_trades < MIN_TRADES_SMALL_SAMPLE:
        warnings.append(
            f"{label}: {num_trades} trades is a small sample (<{MIN_TRADES_SMALL_SAMPLE}); "
            "apparent performance may not generalize."
        )
    return warnings


def multiple_comparison_warning(n_strategies: int) -> str | None:
    if n_strategies > 1:
        return (
            f"{n_strategies} strategies compared on one dataset: the best historical "
            "performer benefits from selection bias. Do not mistake the maximum for "
            "expected out-of-sample performance (multiple-comparison effect)."
        )
    return None


OVERFITTING_CAVEAT = (
    "Parameters chosen by historical optimization describe the past, not the future. "
    "In-sample superiority does not imply predictive power; prefer out-of-sample "
    "and walk-forward evidence, and expect degradation."
)
