"""Merge v0.2a experiments and v0.2b market automation branches."""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0003_merge_v02"
down_revision: str | Sequence[str] | None = ("0002_experiments", "0002_market_automation")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
