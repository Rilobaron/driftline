"""Market automation schema: exchange provenance, stream state, sync history."""

from __future__ import annotations

import contextlib
from collections.abc import Sequence

revision: str = "0002_market_automation"
down_revision: str | None = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    import sqlalchemy as sa

    from alembic import op

    # 1. Exchange provenance on candles. Existing rows belong to the
    #    previously single configured exchange (default binance).
    bind = op.get_bind()
    if bind is not None and bind.dialect.name == "sqlite":
        # SQLite cannot drop/create constraints in place; fresh SQLite DBs
        # are created via Base.metadata (tests/dev) which already carries the
        # exchange-scoped constraint, so only backfill the column here.
        with op.batch_alter_table("candles") as batch:
            batch.add_column(sa.Column("exchange", sa.String(32), server_default="binance"))
        op.create_index("ix_candles_exchange", "candles", ["exchange"])
    else:
        op.add_column("candles", sa.Column("exchange", sa.String(32), server_default="binance"))
        op.create_index("ix_candles_exchange", "candles", ["exchange"])
        # Replace the (symbol, timeframe, ts) uniqueness with the
        # exchange-scoped variant so two exchanges never silently merge.
        with contextlib.suppress(Exception):
            op.drop_constraint("uq_candle", "candles", type_="unique")
        op.create_unique_constraint(
            "uq_candle_exchange", "candles", ["exchange", "symbol", "timeframe", "ts"]
        )

    op.create_table(
        "market_streams",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="BOOTSTRAPPING"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_completed_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bootstrap_done", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("bootstrap_target", sa.Integer(), nullable=False, server_default="8760"),
        sa.Column("unresolved_gaps", sa.JSON(), nullable=True),
        sa.Column("locked_by", sa.String(length=64), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("exchange", "symbol", "timeframe", name="uq_market_stream"),
    )
    op.create_index("ix_market_streams_exchange", "market_streams", ["exchange"])
    op.create_index("ix_market_streams_symbol", "market_streams", ["symbol"])
    op.create_index("ix_market_streams_timeframe", "market_streams", ["timeframe"])

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="incremental"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stored_new", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicates", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("invalid", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("missing", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(length=512), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("data_source", sa.String(length=128), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", name="uq_sync_run"),
    )
    op.create_index("ix_sync_runs_exchange", "sync_runs", ["exchange"])
    op.create_index("ix_sync_runs_symbol", "sync_runs", ["symbol"])
    op.create_index("ix_sync_runs_timeframe", "sync_runs", ["timeframe"])


def downgrade() -> None:
    from alembic import op

    op.drop_index("ix_sync_runs_timeframe", table_name="sync_runs")
    op.drop_index("ix_sync_runs_symbol", table_name="sync_runs")
    op.drop_index("ix_sync_runs_exchange", table_name="sync_runs")
    op.drop_table("sync_runs")
    op.drop_index("ix_market_streams_timeframe", table_name="market_streams")
    op.drop_index("ix_market_streams_symbol", table_name="market_streams")
    op.drop_index("ix_market_streams_exchange", table_name="market_streams")
    op.drop_table("market_streams")
    op.drop_constraint("uq_candle_exchange", "candles", type_="unique")
    op.drop_index("ix_candles_exchange", table_name="candles")
    op.drop_column("candles", "exchange")
    op.create_unique_constraint("uq_candle", "candles", ["symbol", "timeframe", "ts"])
