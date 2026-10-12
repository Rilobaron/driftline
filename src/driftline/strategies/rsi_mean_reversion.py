"""RSI mean-reversion strategy (long-only, spot-style).

Entry: RSI crosses below the oversold threshold (completed candle ``i``).
Exit: RSI crosses above the exit threshold.

Crossing semantics (not level-holding): a signal fires only on the bar where
the threshold is crossed, so no repeated entries are emitted while RSI stays
oversold. A position-state machine additionally ensures entries only fire
when flat and exits only when long.

Note: mean-reversion strategies can perform poorly — including sustained
losses — during persistent trends. This strategy is a research baseline, not
a profitability claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from driftline.domain.models import Candle, Signal, SignalType
from driftline.indicators.basic import rsi as rsi_fn
from driftline.strategies.specs import ParameterSpec


@dataclass(frozen=True)
class RsiMeanReversionParams:
    rsi_period: int = 14
    oversold: float = 30.0
    exit_threshold: float = 50.0

    def __post_init__(self) -> None:
        if self.rsi_period < 2:
            raise ValueError("rsi_period must be at least 2")
        for label, value in (("oversold", self.oversold), ("exit_threshold", self.exit_threshold)):
            if not 0 < value < 100:
                raise ValueError(f"{label} must be within (0, 100), got {value}")
        if not self.oversold < self.exit_threshold:
            raise ValueError(
                f"oversold ({self.oversold}) must be below exit_threshold ({self.exit_threshold})"
            )


class RsiMeanReversionStrategy:
    """Long-only RSI mean reversion baseline."""

    strategy_id = "rsi_mean_reversion"
    name = "rsi_mean_reversion"
    display_name = "RSI Mean Reversion"
    description = (
        "Long-only RSI mean reversion: enter when RSI crosses below the oversold "
        "threshold, exit when it crosses back above the exit threshold. "
        "Research baseline — mean reversion can fail in persistent trends."
    )
    version = "1.0.0"
    required_indicators = ("rsi",)

    def __init__(self, params: RsiMeanReversionParams | None = None) -> None:
        self.params = params or RsiMeanReversionParams()

    @classmethod
    def parameter_schema(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec(
                name="rsi_period",
                kind="int",
                default=14,
                min_value=2,
                max_value=100,
                description="RSI lookback period",
            ),
            ParameterSpec(
                name="oversold",
                kind="float",
                default=30.0,
                min_value=1.0,
                max_value=49.0,
                description="Entry trigger: RSI crosses below this",
            ),
            ParameterSpec(
                name="exit_threshold",
                kind="float",
                default=50.0,
                min_value=31.0,
                max_value=99.0,
                description="Exit trigger: RSI crosses above this",
            ),
        ]

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"rsi_period": 14, "oversold": 30.0, "exit_threshold": 50.0}

    @classmethod
    def build_params(cls, params: dict[str, Any]) -> RsiMeanReversionParams:
        return RsiMeanReversionParams(
            rsi_period=int(params.get("rsi_period", 14)),
            oversold=float(params.get("oversold", 30.0)),
            exit_threshold=float(params.get("exit_threshold", 50.0)),
        )

    @property
    def warmup_bars(self) -> int:
        # First RSI value at index `period`; a crossing needs two consecutive values.
        return self.params.rsi_period + 1

    def generate(self, candles: list[Candle]) -> list[Signal]:
        if len(candles) < self.warmup_bars + 1:
            return []
        closes = [c.close for c in candles]
        rsi_vals = rsi_fn(closes, self.params.rsi_period)
        signals: list[Signal] = []
        in_long = False
        for i in range(1, len(candles)):
            if i < self.warmup_bars:
                continue
            prev, cur = rsi_vals[i - 1], rsi_vals[i]
            if prev is None or cur is None:
                continue
            if not in_long and prev >= self.params.oversold > cur:
                signals.append(
                    Signal(
                        index=i,
                        timestamp=candles[i].ts,
                        signal=SignalType.ENTRY,
                        close=candles[i].close,
                        context={
                            "strategy": self.strategy_id,
                            "strategy_version": self.version,
                            "rsi": cur,
                            "rsi_period": self.params.rsi_period,
                            "oversold": self.params.oversold,
                        },
                    )
                )
                in_long = True
            elif in_long and prev <= self.params.exit_threshold < cur:
                signals.append(
                    Signal(
                        index=i,
                        timestamp=candles[i].ts,
                        signal=SignalType.EXIT,
                        close=candles[i].close,
                        context={
                            "strategy": self.strategy_id,
                            "strategy_version": self.version,
                            "rsi": cur,
                            "rsi_period": self.params.rsi_period,
                            "exit_threshold": self.params.exit_threshold,
                        },
                    )
                )
                in_long = False
        return signals
