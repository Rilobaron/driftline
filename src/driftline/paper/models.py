"""SQLAlchemy ORM models for persistent paper trading (simulated funds only)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, DateTime, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from driftline.persistence.models import Base


class PaperSessionStatus:
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"

    ALL = (CREATED, RUNNING, PAUSED, STOPPED, ERROR)
    ACTIVE = (RUNNING, PAUSED)


class PaperOrderStatus:
    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"

    ALL = (PENDING, FILLED, REJECTED, CANCELLED, EXPIRED)


class PaperEventKind:
    SESSION_CREATED = "SESSION_CREATED"
    SESSION_STARTED = "SESSION_STARTED"
    SESSION_PAUSED = "SESSION_PAUSED"
    SESSION_RESUMED = "SESSION_RESUMED"
    SESSION_STOPPED = "SESSION_STOPPED"
    SESSION_ERROR = "SESSION_ERROR"
    SIGNAL = "SIGNAL"
    ORDER_PLACED = "ORDER_PLACED"
    ORDER_REJECTED = "ORDER_REJECTED"
    FILL = "FILL"
    TRADE_CLOSED = "TRADE_CLOSED"
    RISK_REJECT = "RISK_REJECT"
    KILL_SWITCH_ON = "KILL_SWITCH_ON"
    KILL_SWITCH_OFF = "KILL_SWITCH_OFF"
    DATA_SKIP = "DATA_SKIP"
    RECOVERY = "RECOVERY"


class PaperSessionRow(Base):
    """Durable paper-trading session: portfolio state + execution cursor."""

    __tablename__ = "paper_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default=PaperSessionStatus.CREATED, index=True)

    exchange: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(16), index=True)
    strategy_id: Mapped[str] = mapped_column(String(64))
    strategy_version: Mapped[str] = mapped_column(String(16), default="1.0.0")
    strategy_params: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)

    # Server-side execution/risk snapshot (never client-mutable after create).
    exec_config: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    risk_config: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)

    # Portfolio state (Decimal-precise; reserved_cash stays 0 for market fills).
    initial_capital: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    cash: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    reserved_cash: Mapped[Decimal] = mapped_column(Numeric(36, 8), default=Decimal("0"))
    position_qty: Mapped[Decimal] = mapped_column(Numeric(36, 8), default=Decimal("0"))
    avg_entry_price: Mapped[Decimal] = mapped_column(Numeric(36, 8), default=Decimal("0"))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(36, 8), default=Decimal("0"))
    total_fees: Mapped[Decimal] = mapped_column(Numeric(36, 8), default=Decimal("0"))
    peak_equity: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    # Open-trade legs for the single long position (null/0 when flat).
    open_qty: Mapped[Decimal] = mapped_column(Numeric(36, 8), default=Decimal("0"))
    open_price: Mapped[Decimal] = mapped_column(Numeric(36, 8), default=Decimal("0"))
    open_fee: Mapped[Decimal] = mapped_column(Numeric(36, 8), default=Decimal("0"))
    open_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # UTC-day loss tracking.
    equity_day: Mapped[str] = mapped_column(String(10), default="")
    day_start_equity: Mapped[Decimal] = mapped_column(Numeric(36, 8))

    # Execution cursor + health.
    last_evaluated_ts: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_execution_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)

    # Observability counters.
    num_orders: Mapped[int] = mapped_column(Integer, default=0)
    num_fills: Mapped[int] = mapped_column(Integer, default=0)
    num_trades: Mapped[int] = mapped_column(Integer, default=0)
    risk_rejections: Mapped[int] = mapped_column(Integer, default=0)

    # Single-executor lease (mirrors market-stream locking).
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PaperOrderRow(Base):
    """Simulated MARKET order. One fill at most; idempotency key per session."""

    __tablename__ = "paper_orders"
    __table_args__ = (UniqueConstraint("session_id", "idempotency_key", name="uq_paper_order_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), index=True)
    side: Mapped[str] = mapped_column(String(8))  # buy | sell
    signal_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default=PaperOrderStatus.PENDING, index=True)
    qty: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)


class PaperFillRow(Base):
    """Simulated fill: full quantity at next completed candle open + costs."""

    __tablename__ = "paper_fills"
    __table_args__ = (UniqueConstraint("order_id", name="uq_paper_fill_order"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    price: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    qty: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    fee: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    slippage_rate: Mapped[Decimal] = mapped_column(Numeric(12, 8))
    assumptions: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)


class PaperTradeRow(Base):
    """Closed round-trip (long-only spot). Open leg lives on the session row."""

    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exit_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    entry_price: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    exit_price: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    qty: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    entry_fee: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    exit_fee: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    return_pct: Mapped[Decimal] = mapped_column(Numeric(24, 8))


class PaperSnapshotRow(Base):
    """Per-candle portfolio equity mark (idempotent per session+ts)."""

    __tablename__ = "paper_snapshots"
    __table_args__ = (UniqueConstraint("session_id", "ts", name="uq_paper_snapshot_ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    equity: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    cash: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    position_qty: Mapped[Decimal] = mapped_column(Numeric(36, 8))
    close: Mapped[Decimal] = mapped_column(Numeric(36, 8))


class PaperEventRow(Base):
    """Append-only structured lifecycle/trade-decision log."""

    __tablename__ = "paper_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    detail: Mapped[str] = mapped_column(String(1024), default="")
