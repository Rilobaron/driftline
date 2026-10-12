"""Donchian channel breakout strategy (long-only, spot-style).

Entry: the most recently completed candle closes above the highest high of
the previous ``entry_lookback`` completed candles.
Exit: a completed candle closes below the lowest low of the previous
``exit_lookback`` completed candles.

Anti-lookahead rule: the channel for a decision at candle ``i`` is computed
from candles ``[i - lookback, i)`` — the evaluated candle is NEVER part of
its own channel. Including it would guarantee ``close <= channel high`` for
the high (contamination that suppresses entries) and leak intrabar range
information into the decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from driftline.domain.models import Candle, Signal, SignalType
from driftline.strategies.specs import ParameterSpec


@dataclass(frozen=True)
class DonchianBreakoutParams:
    entry_lookback: int = 20
    exit_lookback: int = 10

    def __post_init__(self) -> None:
        if self.entry_lookback < 2:
            raise ValueError("entry_lookback must be at least 2")
        if self.exit_lookback < 2:
            raise ValueError("exit_lookback must be at least 2")


class DonchianBreakoutStrategy:
    """Long-only Donchian breakout baseline."""

    strategy_id = "donchian_breakout"
    name = "donchian_breakout"
    display_name = "Donchian Breakout"
    description = (
        "Long-only Donchian breakout: enter when a completed candle closes above "
        "the prior 20-candle highest high; exit when it closes below the prior "
        "10-candle lowest low. Research baseline, not a profitability claim."
    )
    version = "1.0.0"
    required_indicators = ("donchian",)

    def __init__(self, params: DonchianBreakoutParams | None = None) -> None:
        self.params = params or DonchianBreakoutParams()

    @classmethod
    def parameter_schema(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec(
                name="entry_lookback",
                kind="int",
                default=20,
                min_value=2,
                max_value=200,
                description="Completed candles in entry channel",
            ),
            ParameterSpec(
                name="exit_lookback",
                kind="int",
                default=10,
                min_value=2,
                max_value=200,
                description="Completed candles in exit channel",
            ),
        ]

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"entry_lookback": 20, "exit_lookback": 10}

    @classmethod
    def build_params(cls, params: dict[str, Any]) -> DonchianBreakoutParams:
        return DonchianBreakoutParams(
            entry_lookback=int(params.get("entry_lookback", 20)),
            exit_lookback=int(params.get("exit_lookback", 10)),
        )

    @property
    def warmup_bars(self) -> int:
        return max(self.params.entry_lookback, self.params.exit_lookback)

    def generate(self, candles: list[Candle]) -> list[Signal]:
        if len(candles) < self.warmup_bars + 1:
            return []
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        entry_lb = self.params.entry_lookback
        exit_lb = self.params.exit_lookback
        signals: list[Signal] = []
        in_long = False
        for i in range(1, len(candles)):
            if i < self.warmup_bars:
                continue
            if not in_long:
                # Channel strictly excludes candle i (uses [i-lb, i)).
                channel_high = max(highs[i - entry_lb : i])
                if candles[i].close > channel_high:
                    signals.append(
                        Signal(
                            index=i,
                            timestamp=candles[i].ts,
                            signal=SignalType.ENTRY,
                            close=candles[i].close,
                            context={
                                "strategy": self.strategy_id,
                                "strategy_version": self.version,
                                "channel_high": channel_high,
                                "entry_lookback": entry_lb,
                            },
                        )
                    )
                    in_long = True
            else:
                channel_low = min(lows[i - exit_lb : i])
                if candles[i].close < channel_low:
                    signals.append(
                        Signal(
                            index=i,
                            timestamp=candles[i].ts,
                            signal=SignalType.EXIT,
                            close=candles[i].close,
                            context={
                                "strategy": self.strategy_id,
                                "strategy_version": self.version,
                                "channel_low": channel_low,
                                "exit_lookback": exit_lb,
                            },
                        )
                    )
                    in_long = False
        return signals
