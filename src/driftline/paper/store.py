"""Persistence helpers for paper-trading sessions, orders, fills and ledger."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from driftline.core.exceptions import ValidationError
from driftline.paper.models import (
    PaperEventRow,
    PaperOrderRow,
    PaperOrderStatus,
    PaperSessionRow,
    PaperSessionStatus,
    PaperSnapshotRow,
    PaperTradeRow,
)

_VALID_TRANSITIONS: dict[str, tuple[str, ...]] = {
    PaperSessionStatus.CREATED: (PaperSessionStatus.RUNNING, PaperSessionStatus.STOPPED),
    PaperSessionStatus.RUNNING: (
        PaperSessionStatus.PAUSED,
        PaperSessionStatus.STOPPED,
        PaperSessionStatus.ERROR,
    ),
    PaperSessionStatus.PAUSED: (
        PaperSessionStatus.RUNNING,
        PaperSessionStatus.STOPPED,
        PaperSessionStatus.ERROR,
    ),
    PaperSessionStatus.ERROR: (PaperSessionStatus.RUNNING, PaperSessionStatus.STOPPED),
    PaperSessionStatus.STOPPED: (),
}


def lock_session_row(db: Session, session_id: str) -> PaperSessionRow | None:
    """Load a session row, using SELECT ... FOR UPDATE on PostgreSQL."""
    stmt = select(PaperSessionRow).where(PaperSessionRow.id == session_id)
    try:
        dialect = db.bind.dialect.name if db.bind is not None else ""
    except Exception:
        dialect = ""
    if dialect == "postgresql":
        stmt = stmt.with_for_update()
    return db.scalars(stmt).first()


def get_session_row(db: Session, session_id: str) -> PaperSessionRow | None:
    return db.get(PaperSessionRow, session_id)


def list_sessions(
    db: Session, *, limit: int = 50, offset: int = 0
) -> tuple[list[PaperSessionRow], int]:
    total = int(db.scalar(select(func.count()).select_from(PaperSessionRow)) or 0)
    rows = list(
        db.scalars(
            select(PaperSessionRow)
            .order_by(PaperSessionRow.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
    )
    return rows, total


def find_active_for_market(
    db: Session, *, exchange: str, symbol: str, timeframe: str
) -> PaperSessionRow | None:
    return db.scalars(
        select(PaperSessionRow).where(
            PaperSessionRow.exchange == exchange,
            PaperSessionRow.symbol == symbol,
            PaperSessionRow.timeframe == timeframe,
            PaperSessionRow.status.in_(list(PaperSessionStatus.ACTIVE)),
        )
    ).first()


def transition(db: Session, row: PaperSessionRow, target: str, *, now: datetime) -> None:
    allowed = _VALID_TRANSITIONS.get(row.status, ())
    if target not in allowed:
        raise ValidationError(f"illegal paper session transition {row.status} -> {target}")
    row.status = target
    row.updated_at = now


def acquire_paper_lock(
    db: Session,
    row: PaperSessionRow,
    *,
    owner: str,
    lease_seconds: int = 600,
    now: datetime | None = None,
) -> bool:
    """DB-backed lease: exactly one executor per session. Expired leases stolen."""
    ref = now or datetime.now(UTC)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    locked_until = row.locked_until
    if locked_until is not None:
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=UTC)
        if locked_until >= ref and row.locked_by not in (None, owner):
            return False
    row.locked_by = owner
    try:
        sqlite = db.bind is not None and db.bind.dialect.name == "sqlite"
    except Exception:
        sqlite = False
    row.locked_until = ref.replace(tzinfo=None) if sqlite else ref
    db.commit()
    return True


def release_paper_lock(db: Session, row: PaperSessionRow, *, owner: str) -> None:
    if row.locked_by == owner:
        row.locked_by = None
        row.locked_until = None
        db.commit()


def add_event(db: Session, session_id: str, *, ts: datetime, kind: str, detail: str = "") -> None:
    db.add(PaperEventRow(session_id=session_id, ts=ts, kind=kind, detail=detail[:1024]))


def list_events(
    db: Session,
    session_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
    kind: str | None = None,
) -> tuple[list[PaperEventRow], int]:
    base = select(PaperEventRow).where(PaperEventRow.session_id == session_id)
    if kind is not None:
        base = base.where(PaperEventRow.kind == kind)
    total = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = list(
        db.scalars(base.order_by(PaperEventRow.id.desc()).limit(limit).offset(offset)).all()
    )
    return rows, total


def list_orders(
    db: Session,
    session_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
    status: str | None = None,
) -> tuple[list[PaperOrderRow], int]:
    base = select(PaperOrderRow).where(PaperOrderRow.session_id == session_id)
    if status is not None:
        base = base.where(PaperOrderRow.status == status)
    total = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = list(
        db.scalars(base.order_by(PaperOrderRow.id.desc()).limit(limit).offset(offset)).all()
    )
    return rows, total


def pending_orders_for_session(db: Session, session_id: str) -> list[PaperOrderRow]:
    return list(
        db.scalars(
            select(PaperOrderRow)
            .where(
                PaperOrderRow.session_id == session_id,
                PaperOrderRow.status == PaperOrderStatus.PENDING,
            )
            .order_by(PaperOrderRow.id.asc())
        ).all()
    )


def list_trades(
    db: Session, session_id: str, *, limit: int = 100, offset: int = 0
) -> tuple[list[PaperTradeRow], int]:
    base = select(PaperTradeRow).where(PaperTradeRow.session_id == session_id)
    total = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = list(
        db.scalars(base.order_by(PaperTradeRow.id.desc()).limit(limit).offset(offset)).all()
    )
    return rows, total


def list_snapshots(db: Session, session_id: str, *, limit: int = 5000) -> list[PaperSnapshotRow]:
    rows = list(
        db.scalars(
            select(PaperSnapshotRow)
            .where(PaperSnapshotRow.session_id == session_id)
            .order_by(PaperSnapshotRow.ts.asc())
        ).all()
    )
    return rows[-limit:] if len(rows) > limit else rows


def order_by_key(db: Session, session_id: str, key: str) -> PaperOrderRow | None:
    return db.scalars(
        select(PaperOrderRow).where(
            PaperOrderRow.session_id == session_id,
            PaperOrderRow.idempotency_key == key,
        )
    ).first()


def snapshot_exists(db: Session, session_id: str, ts: datetime) -> bool:
    return (
        db.scalar(
            select(func.count())
            .select_from(PaperSnapshotRow)
            .where(PaperSnapshotRow.session_id == session_id, PaperSnapshotRow.ts == ts)
        )
        or 0
    ) > 0


def event_payload(detail: dict[str, Any]) -> str:
    import json

    return json.dumps(detail, sort_keys=True, default=str)[:1024]
