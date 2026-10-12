"""EMA crossover baseline strategy (long-only, spot-style).

Signals are emitted on completed candle ``i`` when the fast EMA crosses the
slow EMA between ``i - 1`` and ``i``. Execution timing (e.g. next-open) is the
backtest engine's responsibility, keeping signal generation free of lookahead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from driftline.domain.models import Candle, Signal, SignalType
from driftline.indicators.basic import atr as atr_fn
from driftline.indicators.basic import ema as ema_fn
from driftline.strategies.specs import ParameterSpec


@dataclass(frozen=True)
class EmaCrossoverParams:
    fast_period: int = 20
    slow_period: int = 50
    atr_period: int = 14

    def __post_init__(self) -> None:
        if self.fast_period <= 0 or self.slow_period <= 0 or self.atr_period <= 0:
            raise ValueError("periods must be positive")
        if self.fast_period >= self.slow_period:
            raise ValueError("fast_period must be smaller than slow_period")


class EmaCrossoverStrategy:
    """Baseline EMA-crossover strategy for platform validation."""

    strategy_id = "ema_crossover"
    name = "ema_crossover"
    display_name = "EMA Crossover"
    description = (
        "Baseline long-only EMA crossover (fast crosses slow). "
        "Validation baseline, not a profitability claim."
    )
    version = "1.0.0"
    required_indicators = ("ema", "atr")

    def __init__(self, params: EmaCrossoverParams | None = None) -> None:
        self.params = params or EmaCrossoverParams()

    @classmethod
    def parameter_schema(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec(
                name="fast_period",
                kind="int",
                default=20,
                min_value=2,
                max_value=500,
                description="Fast EMA period",
            ),
            ParameterSpec(
                name="slow_period",
                kind="int",
                default=50,
                min_value=3,
                max_value=500,
                description="Slow EMA period",
            ),
            ParameterSpec(
                name="atr_period",
                kind="int",
                default=14,
                min_value=2,
                max_value=200,
                description="ATR context period",
            ),
        ]

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"fast_period": 20, "slow_period": 50, "atr_period": 14}

    @classmethod
    def build_params(cls, params: dict[str, Any]) -> EmaCrossoverParams:
        return EmaCrossoverParams(
            fast_period=int(params.get("fast_period", 20)),
            slow_period=int(params.get("slow_period", 50)),
            atr_period=int(params.get("atr_period", 14)),
        )

    @property
    def warmup_bars(self) -> int:
        # slow EMA seeds at slow-1; a crossover needs two consecutive diffs.
        return self.params.slow_period

    def generate(self, candles: list[Candle]) -> list[Signal]:
        if len(candles) < self.warmup_bars + 1:
            return []
        closes = [c.close for c in candles]
        fast = ema_fn(closes, self.params.fast_period)
        slow = ema_fn(closes, self.params.slow_period)
        atr_vals = atr_fn(
            [c.high for c in candles],
            [c.low for c in candles],
            closes,
            self.params.atr_period,
        )
        signals: list[Signal] = []
        for i in range(1, len(candles)):
            if i < self.warmup_bars:
                continue
            f_prev, f_now = fast[i - 1], fast[i]
            s_prev, s_now = slow[i - 1], slow[i]
            if f_prev is None or f_now is None or s_prev is None or s_now is None:
                continue
            prev_diff = f_prev - s_prev
            now_diff = f_now - s_now
            if prev_diff <= 0 < now_diff:
                signals.append(
                    Signal(
                        index=i,
                        timestamp=candles[i].ts,
                        signal=SignalType.ENTRY,
                        fast_ema=f_now,
                        slow_ema=s_now,
                        atr=atr_vals[i],
                        close=candles[i].close,
                        context={
                            "strategy": self.name,
                            "fast_period": self.params.fast_period,
                            "slow_period": self.params.slow_period,
                            "prev_diff": prev_diff,
                            "diff": now_diff,
                        },
                    )
                )
            elif prev_diff >= 0 > now_diff:
                signals.append(
                    Signal(
                        index=i,
                        timestamp=candles[i].ts,
                        signal=SignalType.EXIT,
                        fast_ema=f_now,
                        slow_ema=s_now,
                        atr=atr_vals[i],
                        close=candles[i].close,
                        context={
                            "strategy": self.name,
                            "fast_period": self.params.fast_period,
                            "slow_period": self.params.slow_period,
                            "prev_diff": prev_diff,
                            "diff": now_diff,
                        },
                    )
                )
        return signals
