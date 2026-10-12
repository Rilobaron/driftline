"""Pydantic request/response schemas for the v1 API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class CandleOut(BaseModel):
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class IndicatorPoint(BaseModel):
    ts: datetime
    close: float
    fast_ema: float | None = None
    slow_ema: float | None = None
    rsi: float | None = None
    atr: float | None = None


class StrategyOut(BaseModel):
    name: str
    description: str
    default_params: dict[str, Any]


class BacktestRequest(BaseModel):
    symbol: str = Field(default="BTC/USDT", max_length=32)
    timeframe: str = Field(default="1h", max_length=16)
    strategy: str = Field(default="ema_crossover", max_length=64)
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    initial_capital: float = Field(default=10_000.0, gt=0, le=100_000_000)
    fee_rate: float = Field(default=0.001, ge=0, lt=0.1)
    slippage_rate: float = Field(default=0.0005, ge=0, lt=0.1)
    allocation: float = Field(default=1.0, gt=0, le=1.0)
    max_candles: int = Field(default=1000, ge=100, le=5000)
    use_fixture: bool = Field(default=True)
    fast_period: int | None = Field(default=None, ge=2, le=200)
    slow_period: int | None = Field(default=None, ge=3, le=500)
    atr_period: int | None = Field(default=None, ge=2, le=200)


class BacktestSummary(BaseModel):
    id: str
    symbol: str
    timeframe: str
    strategy: str
    data_source: str
    created_at: datetime
    metrics: dict[str, Any]
    num_trades: int
    num_signals: int
    num_candles: int


class TradeOut(BaseModel):
    entry_index: int
    exit_index: int
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    quantity: float
    realized_pnl: float
    return_pct: float


class EquityPointOut(BaseModel):
    timestamp: datetime
    equity: float
    cash: float
    position_qty: float
    close: float


class ErrorOut(BaseModel):
    detail: str
    code: Literal["validation_error", "market_data_error", "backtest_error", "not_found"] = (
        "validation_error"
    )
