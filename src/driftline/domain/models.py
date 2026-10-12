"""Core domain models: candles, signals, trades, portfolio snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class Candle(BaseModel):
    """Normalized OHLCV candle with UTC timestamp (start of interval)."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @field_validator("ts")
    @classmethod
    def _normalize_ts(cls, v: datetime) -> datetime:
        return _utc(v)

    @field_validator("open", "high", "low", "close")
    @classmethod
    def _positive_price(cls, v: float) -> float:
        if v <= 0 or v != v:  # NaN check via self-inequality
            raise ValueError("price must be a positive finite number")
        return float(v)


class SignalType(StrEnum):
    ENTRY = "entry"
    EXIT = "exit"


class Signal(BaseModel):
    """Structured strategy output tied to a completed candle index."""

    index: int = Field(ge=0)
    timestamp: datetime
    signal: SignalType
    fast_ema: float | None = None
    slow_ema: float | None = None
    atr: float | None = None
    close: float
    context: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def _normalize_ts(cls, v: datetime) -> datetime:
        return _utc(v)


class TradeSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class Trade(BaseModel):
    """Completed round-trip trade (long-only spot in v0.1)."""

    entry_index: int
    exit_index: int
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    quantity: float
    entry_fee: float = 0.0
    exit_fee: float = 0.0
    realized_pnl: float = 0.0
    return_pct: float = 0.0

    @field_validator("entry_time", "exit_time")
    @classmethod
    def _normalize_ts(cls, v: datetime) -> datetime:
        return _utc(v)


class EquityPoint(BaseModel):
    timestamp: datetime
    equity: float
    cash: float
    position_qty: float
    close: float

    @field_validator("timestamp")
    @classmethod
    def _normalize_ts(cls, v: datetime) -> datetime:
        return _utc(v)


class BacktestMetrics(BaseModel):
    initial_capital: float
    final_equity: float
    total_return_pct: float
    annualized_return_pct: float | None = None
    buy_and_hold_return_pct: float
    max_drawdown_pct: float
    num_trades: int
    win_rate_pct: float | None = None
    profit_factor: float | None = None
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    expectancy: float | None = None
    total_fees: float
    exposure_pct: float = 0.0
    assumptions: list[str] = Field(default_factory=list)
