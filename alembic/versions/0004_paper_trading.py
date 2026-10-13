"""Paper-trading tables: sessions, orders, fills, trades, snapshots, events."""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0004_paper_trading"
down_revision: str | Sequence[str] | None = "0003_merge_v02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    import sqlalchemy as sa

    from alembic import op

    op.create_table(
        "paper_sessions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="CREATED"),
        sa.Column("exchange", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(16), nullable=False),
        sa.Column("strategy_id", sa.String(64), nullable=False),
        sa.Column("strategy_version", sa.String(16), nullable=False, server_default="1.0.0"),
        sa.Column("strategy_params", sa.JSON(), nullable=False),
        sa.Column("exec_config", sa.JSON(), nullable=False),
        sa.Column("risk_config", sa.JSON(), nullable=False),
        sa.Column("initial_capital", sa.Numeric(36, 8), nullable=False),
        sa.Column("cash", sa.Numeric(36, 8), nullable=False),
        sa.Column("reserved_cash", sa.Numeric(36, 8), nullable=False, server_default="0"),
        sa.Column("position_qty", sa.Numeric(36, 8), nullable=False, server_default="0"),
        sa.Column("avg_entry_price", sa.Numeric(36, 8), nullable=False, server_default="0"),
        sa.Column("realized_pnl", sa.Numeric(36, 8), nullable=False, server_default="0"),
        sa.Column("total_fees", sa.Numeric(36, 8), nullable=False, server_default="0"),
        sa.Column("peak_equity", sa.Numeric(36, 8), nullable=False),
        sa.Column("open_qty", sa.Numeric(36, 8), nullable=False, server_default="0"),
        sa.Column("open_price", sa.Numeric(36, 8), nullable=False, server_default="0"),
        sa.Column("open_fee", sa.Numeric(36, 8), nullable=False, server_default="0"),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("equity_day", sa.String(10), nullable=False, server_default=""),
        sa.Column("day_start_equity", sa.Numeric(36, 8), nullable=False),
        sa.Column("last_evaluated_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_execution_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(512), nullable=True),
        sa.Column("kill_switch", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("num_orders", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("num_fills", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("num_trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("risk_rejections", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_by", sa.String(64), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_paper_sessions_status", "paper_sessions", ["status"])
    op.create_index("ix_paper_sessions_exchange", "paper_sessions", ["exchange"])
    op.create_index("ix_paper_sessions_symbol", "paper_sessions", ["symbol"])
    op.create_index("ix_paper_sessions_timeframe", "paper_sessions", ["timeframe"])

    op.create_table(
        "paper_orders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("signal_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("qty", sa.Numeric(36, 8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(512), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "idempotency_key", name="uq_paper_order_key"),
    )
    op.create_index("ix_paper_orders_session_id", "paper_orders", ["session_id"])
    op.create_index("ix_paper_orders_status", "paper_orders", ["status"])

    op.create_table(
        "paper_fills",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price", sa.Numeric(36, 8), nullable=False),
        sa.Column("qty", sa.Numeric(36, 8), nullable=False),
        sa.Column("fee", sa.Numeric(36, 8), nullable=False),
        sa.Column("slippage_rate", sa.Numeric(12, 8), nullable=False),
        sa.Column("assumptions", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", name="uq_paper_fill_order"),
    )
    op.create_index("ix_paper_fills_session_id", "paper_fills", ["session_id"])
    op.create_index("ix_paper_fills_order_id", "paper_fills", ["order_id"])

    op.create_table(
        "paper_trades",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_price", sa.Numeric(36, 8), nullable=False),
        sa.Column("exit_price", sa.Numeric(36, 8), nullable=False),
        sa.Column("qty", sa.Numeric(36, 8), nullable=False),
        sa.Column("entry_fee", sa.Numeric(36, 8), nullable=False),
        sa.Column("exit_fee", sa.Numeric(36, 8), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(36, 8), nullable=False),
        sa.Column("return_pct", sa.Numeric(24, 8), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_paper_trades_session_id", "paper_trades", ["session_id"])

    op.create_table(
        "paper_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("equity", sa.Numeric(36, 8), nullable=False),
        sa.Column("cash", sa.Numeric(36, 8), nullable=False),
        sa.Column("position_qty", sa.Numeric(36, 8), nullable=False),
        sa.Column("close", sa.Numeric(36, 8), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "ts", name="uq_paper_snapshot_ts"),
    )
    op.create_index("ix_paper_snapshots_session_id", "paper_snapshots", ["session_id"])
    op.create_index("ix_paper_snapshots_ts", "paper_snapshots", ["ts"])

    op.create_table(
        "paper_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("detail", sa.String(1024), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_paper_events_session_id", "paper_events", ["session_id"])
    op.create_index("ix_paper_events_ts", "paper_events", ["ts"])
    op.create_index("ix_paper_events_kind", "paper_events", ["kind"])


def downgrade() -> None:
    from alembic import op

    op.drop_index("ix_paper_events_kind", table_name="paper_events")
    op.drop_index("ix_paper_events_ts", table_name="paper_events")
    op.drop_index("ix_paper_events_session_id", table_name="paper_events")
    op.drop_table("paper_events")
    op.drop_index("ix_paper_snapshots_ts", table_name="paper_snapshots")
    op.drop_index("ix_paper_snapshots_session_id", table_name="paper_snapshots")
    op.drop_table("paper_snapshots")
    op.drop_index("ix_paper_trades_session_id", table_name="paper_trades")
    op.drop_table("paper_trades")
    op.drop_index("ix_paper_fills_order_id", table_name="paper_fills")
    op.drop_index("ix_paper_fills_session_id", table_name="paper_fills")
    op.drop_table("paper_fills")
    op.drop_index("ix_paper_orders_status", table_name="paper_orders")
    op.drop_index("ix_paper_orders_session_id", table_name="paper_orders")
    op.drop_table("paper_orders")
    op.drop_index("ix_paper_sessions_timeframe", table_name="paper_sessions")
    op.drop_index("ix_paper_sessions_symbol", table_name="paper_sessions")
    op.drop_index("ix_paper_sessions_exchange", table_name="paper_sessions")
    op.drop_index("ix_paper_sessions_status", table_name="paper_sessions")
    op.drop_table("paper_sessions")
