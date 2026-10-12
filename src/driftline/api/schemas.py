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
    version: str = "1.0.0"


class ParameterSchemaOut(BaseModel):
    name: str
    kind: str = "int"
    default: float | int
    min_value: float | None = None
    max_value: float | None = None
    description: str = ""


class StrategyDetailOut(BaseModel):
    strategy_id: str
    name: str
    description: str
    version: str = "1.0.0"
    warmup_bars: int
    required_indicators: list[str] = Field(default_factory=list)
    default_params: dict[str, Any] = Field(default_factory=dict)
    parameter_schema: list[ParameterSchemaOut] = Field(default_factory=list)


class BacktestRequest(BaseModel):
    symbol: str = Field(default="BTC/USDT", max_length=32)
    timeframe: str = Field(default="1h", max_length=16)
    exchange: str = Field(default="binance", max_length=32)
    strategy: str = Field(default="ema_crossover", max_length=64)
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    initial_capital: float = Field(default=10_000.0, gt=0, le=100_000_000)
    fee_rate: float = Field(default=0.001, ge=0, lt=0.1)
    slippage_rate: float = Field(default=0.0005, ge=0, lt=0.1)
    allocation: float = Field(default=1.0, gt=0, le=1.0)
    max_candles: int = Field(default=1000, ge=100, le=5000)
    min_candles: int | None = Field(default=None, ge=2, le=5000)
    use_fixture: bool = Field(default=True)
    fast_period: int | None = Field(default=None, ge=2, le=200)
    slow_period: int | None = Field(default=None, ge=3, le=500)
    atr_period: int | None = Field(default=None, ge=2, le=200)


class BacktestSummary(BaseModel):
    id: str
    symbol: str
    timeframe: str
    exchange: str = "binance"
    strategy: str
    data_source: str
    created_at: datetime
    metrics: dict[str, Any]
    num_trades: int
    num_signals: int
    num_candles: int
    dataset_fingerprint: str = ""
    data_warnings: list[str] = Field(default_factory=list)


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


class StrategyCandidateIn(BaseModel):
    strategy_id: str = Field(max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)


class SharedBacktestFields(BaseModel):
    initial_capital: float = Field(default=10_000.0, gt=0, le=100_000_000)
    fee_rate: float = Field(default=0.001, ge=0, lt=0.1)
    slippage_rate: float = Field(default=0.0005, ge=0, lt=0.1)
    allocation: float = Field(default=1.0, gt=0, le=1.0)
    risk: dict[str, float] | None = Field(default=None)


class CompareRequest(SharedBacktestFields):
    symbol: str = Field(default="BTC/USDT", max_length=32)
    timeframe: str = Field(default="1h", max_length=16)
    strategies: list[StrategyCandidateIn] = Field(min_length=1, max_length=5)
    max_candles: int = Field(default=1000, ge=100, le=5000)
    use_fixture: bool = Field(default=True)
    sort_by: str | None = Field(default=None, max_length=32)
    sort_descending: bool = Field(default=True)


class SplitTestRequest(SharedBacktestFields):
    symbol: str = Field(default="BTC/USDT", max_length=32)
    timeframe: str = Field(default="1h", max_length=16)
    strategy_id: str = Field(default="ema_crossover", max_length=64)
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    train_ratio: float = Field(default=0.7, ge=0.5, le=0.9)
    max_candles: int = Field(default=1000, ge=200, le=5000)
    use_fixture: bool = Field(default=True)


class WalkForwardRequest(SharedBacktestFields):
    symbol: str = Field(default="BTC/USDT", max_length=32)
    timeframe: str = Field(default="1h", max_length=16)
    strategy_id: str = Field(default="ema_crossover", max_length=64)
    param_grid: dict[str, list[int | float]] = Field(min_length=1, max_length=4)
    train_window: int = Field(default=1000, ge=200, le=5000)
    test_window: int = Field(default=250, ge=50, le=2000)
    step: int = Field(default=250, ge=50, le=2000)
    expanding: bool = Field(default=False)
    objective: str = Field(default="total_return_pct", max_length=32)
    min_trades: int = Field(default=1, ge=0, le=20)
    max_candles: int = Field(default=2000, ge=500, le=9000)
    use_fixture: bool = Field(default=True)


class ExperimentSummaryOut(BaseModel):
    experiment_id: str
    created_at: datetime
    methodology: str
    strategy_id: str
    strategy_version: str = "1.0.0"
    symbol: str = ""
    timeframe: str = "1h"
    fingerprint: str = ""


class ExperimentDetailOut(ExperimentSummaryOut):
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    evaluation: dict[str, Any] = Field(default_factory=dict)
    data_provider: str = ""
    data_start: datetime | None = None
    data_end: datetime | None = None
    num_candles: int = 0
    backtest_config: dict[str, Any] = Field(default_factory=dict)
    risk_config: dict[str, Any] | None = None
    seed: int | None = None
    code_version: str = ""
    git_commit: str | None = None
    results: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


# --- v0.2b market automation ---


class StreamOut(BaseModel):
    id: int
    exchange: str
    symbol: str
    timeframe: str
    enabled: bool
    status: str
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_completed_ts: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    bootstrap_done: bool = False
    unresolved_gaps: list[str] = Field(default_factory=list)
    last_duration_ms: int = 0


class StreamHealthOut(BaseModel):
    exchange: str
    symbol: str
    timeframe: str
    status: str
    last_completed_ts: datetime | None = None
    last_success_at: datetime | None = None
    expected_last_completed: datetime | None = None
    freshness_seconds: float | None = None
    total_stored: int = 0
    missing_count: int = 0
    unresolved_gaps: list[str] = Field(default_factory=list)
    last_duration_ms: int = 0
    last_error: str | None = None
    provider_available: bool | None = None


class MarketHealthOut(BaseModel):
    overall: str
    generated_at: datetime
    streams: list[StreamHealthOut] = Field(default_factory=list)


class SyncRequest(BaseModel):
    exchange: str = Field(default="binance", max_length=32)
    symbol: str = Field(default="BTC/USDT", max_length=32)
    timeframe: str = Field(default="1h", max_length=16)
    kind: str = Field(default="incremental", max_length=16)
    total_limit: int = Field(default=2000, ge=1, le=8760)


class RepairRequest(BaseModel):
    exchange: str = Field(default="binance", max_length=32)
    symbol: str = Field(default="BTC/USDT", max_length=32)
    timeframe: str = Field(default="1h", max_length=16)
    max_intervals: int = Field(default=10, ge=1, le=50)


class SyncReportOut(BaseModel):
    exchange: str
    symbol: str
    timeframe: str
    kind: str
    success: bool
    fetched: int
    stored_new: int
    total_stored: int
    duplicates: int
    invalid: int
    missing: int
    error: str | None = None
    duration_ms: int = 0
    data_source: str = ""


class SyncRunOut(BaseModel):
    id: int
    exchange: str
    symbol: str
    timeframe: str
    kind: str
    started_at: datetime
    finished_at: datetime
    success: bool
    fetched: int
    stored_new: int
    duplicates: int
    invalid: int
    missing: int
    error: str | None = None
    duration_ms: int = 0
    data_source: str = ""


class SyncHistoryOut(BaseModel):
    total: int
    limit: int
    offset: int
    runs: list[SyncRunOut] = Field(default_factory=list)
