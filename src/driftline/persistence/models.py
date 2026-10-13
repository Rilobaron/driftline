"""SQLAlchemy ORM models for candles, backtest runs, and market automation."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CandleRow(Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("exchange", "symbol", "timeframe", "ts", name="uq_candle_exchange"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False, default="binance", index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(16), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)


class BacktestRow(Base):
    __tablename__ = "backtests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(16))
    strategy: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, object]] = mapped_column(JSON)
    config: Mapped[dict[str, object]] = mapped_column(JSON)
    metrics: Mapped[dict[str, object]] = mapped_column(JSON)
    trades: Mapped[list[object]] = mapped_column(JSON)
    equity: Mapped[list[object]] = mapped_column(JSON)
    data_source: Mapped[str] = mapped_column(String(128))


class StreamStatus:
    HEALTHY = "HEALTHY"
    STALE = "STALE"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"
    BOOTSTRAPPING = "BOOTSTRAPPING"
    DISABLED = "DISABLED"

    ALL = (HEALTHY, STALE, DEGRADED, ERROR, BOOTSTRAPPING, DISABLED)


class MarketStreamRow(Base):
    """Operational sync state per (exchange, symbol, timeframe)."""

    __tablename__ = "market_streams"
    __table_args__ = (UniqueConstraint("exchange", "symbol", "timeframe", name="uq_market_stream"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(16), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default=StreamStatus.BOOTSTRAPPING)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_completed_ts: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    bootstrap_done: Mapped[bool] = mapped_column(Boolean, default=False)
    bootstrap_target: Mapped[int] = mapped_column(Integer, default=8760)
    unresolved_gaps: Mapped[list[object]] = mapped_column(JSON, nullable=False, default=list)
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SyncRunRow(Base):
    """Append-only history of sync/bootstrap/repair attempts."""

    __tablename__ = "sync_runs"
    # NOTE: no explicit UNIQUE on id — the primary key already guarantees it.

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(16), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="incremental")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    stored_new: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    invalid: Mapped[int] = mapped_column(Integer, default=0)
    missing: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    data_source: Mapped[str] = mapped_column(String(128), default="")


class ExperimentRow(Base):
    """Lightweight research experiment log (v0.2a Strategy Lab)."""

    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    methodology: Mapped[str] = mapped_column(String(32), index=True)
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    record: Mapped[dict[str, object]] = mapped_column(JSON)
