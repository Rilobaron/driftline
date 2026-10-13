"""Exchange-scoped candle uniqueness on SQLite.

Revision 0002 swaps the ``(symbol, timeframe, ts)`` uniqueness for the
exchange-scoped variant, but SQLite cannot alter constraints in place, so
SQLite databases kept the legacy constraint. This revision completes the swap
via batch copy-and-move. PostgreSQL already carries the new constraint from
0002 and is left untouched.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence

revision: str = "0005_candles_exchange_unique"
down_revision: str | Sequence[str] | None = "0004_paper_trading"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    from alembic import op

    # Model parity: PaperOrderRow.idempotency_key carries index=True, which
    # revision 0004 did not create. Harmless on databases that already have it
    # (none do — 0004 never created it), required for metadata parity.
    with contextlib.suppress(Exception):
        op.create_index("ix_paper_orders_idempotency_key", "paper_orders", ["idempotency_key"])

    bind = op.get_bind()
    if bind is None or bind.dialect.name != "sqlite":
        return
    with op.batch_alter_table("candles", recreate="always") as batch:
        batch.drop_constraint("uq_candle", type_="unique")
        batch.create_unique_constraint(
            "uq_candle_exchange", ["exchange", "symbol", "timeframe", "ts"]
        )


def downgrade() -> None:
    from alembic import op

    with contextlib.suppress(Exception):
        op.drop_index("ix_paper_orders_idempotency_key", table_name="paper_orders")

    bind = op.get_bind()
    if bind is None or bind.dialect.name != "sqlite":
        return
    with op.batch_alter_table("candles", recreate="always") as batch:
        batch.drop_constraint("uq_candle_exchange", type_="unique")
        batch.create_unique_constraint("uq_candle", ["symbol", "timeframe", "ts"])
