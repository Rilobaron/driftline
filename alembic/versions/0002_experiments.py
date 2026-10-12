"""Research experiments table (v0.2a Strategy Lab)."""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0002_experiments"
down_revision: str | None = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    import sqlalchemy as sa

    from alembic import op

    op.create_table(
        "experiments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("methodology", sa.String(length=32), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("record", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_experiments_methodology", "experiments", ["methodology"])
    op.create_index("ix_experiments_strategy_id", "experiments", ["strategy_id"])


def downgrade() -> None:
    from alembic import op

    op.drop_index("ix_experiments_strategy_id", table_name="experiments")
    op.drop_index("ix_experiments_methodology", table_name="experiments")
    op.drop_table("experiments")
