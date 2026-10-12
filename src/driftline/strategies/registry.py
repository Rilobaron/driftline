"""Strategy registry (extension point for future strategies/ensembles)."""

from __future__ import annotations

from typing import Any

from driftline.strategies.ema_crossover import EmaCrossoverParams, EmaCrossoverStrategy

_STRATEGIES: dict[str, dict[str, object]] = {
    "ema_crossover": {
        "name": "ema_crossover",
        "description": (
            "Baseline long-only EMA crossover (fast crosses slow). "
            "Validation baseline, not a profitability claim."
        ),
        "default_params": {"fast_period": 20, "slow_period": 50, "atr_period": 14},
    },
}


def list_strategies() -> list[dict[str, object]]:
    return [dict(v) for v in _STRATEGIES.values()]


def create_strategy(name: str, params: dict[str, Any] | None = None) -> EmaCrossoverStrategy:
    if name != "ema_crossover":
        raise ValueError(f"unknown strategy: {name}")
    p = params or {}
    return EmaCrossoverStrategy(
        EmaCrossoverParams(
            fast_period=int(p.get("fast_period", 20)),
            slow_period=int(p.get("slow_period", 50)),
            atr_period=int(p.get("atr_period", 14)),
        )
    )
