"""Repository helpers for candles, backtest runs, and market automation state."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from driftline.domain.models import Candle
from driftline.market_data.provider import merge_incremental
from driftline.persistence.models import (
    BacktestRow,
    CandleRow,
    ExperimentRow,
    MarketStreamRow,
    StreamStatus,
    SyncRunRow,
)


def _ts_key(ts: datetime) -> datetime:
    """Backend-agnostic timestamp key (SQLite returns naive datetimes)."""
    return ts.replace(tzinfo=None) if ts.tzinfo is not None else ts


def _as_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def upsert_candles(
    session: Session,
    symbol: str,
    timeframe: str,
    candles: list[Candle],
    *,
    exchange: str = "binance",
) -> int:
    """Insert-or-ignore candles (idempotent incremental sync). Returns new rows."""
    existing_ts = {
        _ts_key(t)
        for t in session.scalars(
            select(CandleRow.ts).where(
                CandleRow.exchange == exchange,
                CandleRow.symbol == symbol,
                CandleRow.timeframe == timeframe,
            )
        ).all()
    }
    fresh = [c for c in candles if _ts_key(c.ts) not in existing_ts]
    for c in fresh:
        existing_ts.add(_ts_key(c.ts))
        session.add(
            CandleRow(
                exchange=exchange,
                symbol=symbol,
                timeframe=timeframe,
                ts=c.ts,
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                volume=c.volume,
            )
        )
    session.commit()
    return len(fresh)


def load_candles(
    session: Session,
    symbol: str,
    timeframe: str,
    *,
    limit: int = 5000,
    since: datetime | None = None,
    exchange: str = "binance",
) -> list[Candle]:
    stmt = (
        select(CandleRow)
        .where(
            CandleRow.exchange == exchange,
            CandleRow.symbol == symbol,
            CandleRow.timeframe == timeframe,
        )
        .order_by(CandleRow.ts.asc())
    )
    if since is not None:
        stmt = stmt.where(CandleRow.ts >= since)
    rows = session.scalars(stmt).all()
    rows = rows[-limit:] if len(rows) > limit else rows
    out = [
        Candle(
            ts=_as_utc(r.ts),
            open=r.open,
            high=r.high,
            low=r.low,
            close=r.close,
            volume=r.volume,
        )
        for r in rows
    ]
    # Defensive: DB unique constraint should prevent dupes; merge guards anyway.
    return merge_incremental([], out)


def count_candles(session: Session, symbol: str, timeframe: str, *, exchange: str) -> int:
    stmt = (
        select(func.count())
        .select_from(CandleRow)
        .where(
            CandleRow.exchange == exchange,
            CandleRow.symbol == symbol,
            CandleRow.timeframe == timeframe,
        )
    )
    return int(session.scalar(stmt) or 0)


def latest_candle_ts(
    session: Session, symbol: str, timeframe: str, *, exchange: str
) -> datetime | None:
    stmt = (
        select(CandleRow.ts)
        .where(
            CandleRow.exchange == exchange,
            CandleRow.symbol == symbol,
            CandleRow.timeframe == timeframe,
        )
        .order_by(CandleRow.ts.desc())
        .limit(1)
    )
    ts = session.scalar(stmt)
    return _as_utc(ts) if ts is not None else None


def save_backtest(session: Session, row: BacktestRow) -> None:
    session.add(row)
    session.commit()


def get_backtest(session: Session, backtest_id: str) -> BacktestRow | None:
    return session.get(BacktestRow, backtest_id)


# --- market stream state -----------------------------------------------------


def get_or_create_stream(
    session: Session,
    *,
    exchange: str,
    symbol: str,
    timeframe: str,
    enabled: bool = True,
    bootstrap_target: int = 8760,
) -> MarketStreamRow:
    row = session.scalars(
        select(MarketStreamRow).where(
            MarketStreamRow.exchange == exchange,
            MarketStreamRow.symbol == symbol,
            MarketStreamRow.timeframe == timeframe,
        )
    ).first()
    if row is None:
        row = MarketStreamRow(
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            enabled=enabled,
            status=StreamStatus.DISABLED if not enabled else StreamStatus.BOOTSTRAPPING,
            bootstrap_target=bootstrap_target,
            unresolved_gaps=[],
        )
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def list_streams(session: Session) -> list[MarketStreamRow]:
    return list(session.scalars(select(MarketStreamRow).order_by(MarketStreamRow.id.asc())).all())


def get_stream(session: Session, stream_id: int) -> MarketStreamRow | None:
    return session.get(MarketStreamRow, stream_id)


def find_stream(
    session: Session, *, exchange: str, symbol: str, timeframe: str
) -> MarketStreamRow | None:
    return session.scalars(
        select(MarketStreamRow).where(
            MarketStreamRow.exchange == exchange,
            MarketStreamRow.symbol == symbol,
            MarketStreamRow.timeframe == timeframe,
        )
    ).first()


def acquire_stream_lock(
    session: Session,
    row: MarketStreamRow,
    *,
    owner: str,
    lease_seconds: int = 600,
    now: datetime | None = None,
) -> bool:
    """DB-backed lease so two workers never sync the same stream concurrently.

    Returns True when this owner holds the lock. Expired leases are stolen.
    """
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
    # Lease arithmetic in UTC; SQLite stores naive — compare defensively.
    row.locked_until = ref.replace(tzinfo=None) if _is_sqlite(session) else ref
    session.commit()
    return True


def release_stream_lock(session: Session, row: MarketStreamRow, *, owner: str) -> None:
    if row.locked_by == owner:
        row.locked_by = None
        row.locked_until = None
        session.commit()


def _is_sqlite(session: Session) -> bool:
    try:
        return session.bind is not None and session.bind.dialect.name == "sqlite"
    except Exception:
        return False


# --- sync run history --------------------------------------------------------


def record_sync_run(
    session: Session,
    *,
    exchange: str,
    symbol: str,
    timeframe: str,
    kind: str,
    started_at: datetime,
    finished_at: datetime,
    success: bool,
    fetched: int = 0,
    stored_new: int = 0,
    duplicates: int = 0,
    invalid: int = 0,
    missing: int = 0,
    error: str | None = None,
    duration_ms: int = 0,
    data_source: str = "",
) -> SyncRunRow:
    row = SyncRunRow(
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        kind=kind,
        started_at=started_at,
        finished_at=finished_at,
        success=success,
        fetched=fetched,
        stored_new=stored_new,
        duplicates=duplicates,
        invalid=invalid,
        missing=missing,
        error=(error or "")[:512] if error else None,
        duration_ms=duration_ms,
        data_source=data_source,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def list_sync_runs(
    session: Session,
    *,
    limit: int = 50,
    offset: int = 0,
    exchange: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> tuple[list[SyncRunRow], int]:
    base = select(SyncRunRow)
    if exchange is not None:
        base = base.where(SyncRunRow.exchange == exchange)
    if symbol is not None:
        base = base.where(SyncRunRow.symbol == symbol)
    if timeframe is not None:
        base = base.where(SyncRunRow.timeframe == timeframe)
    count_stmt = select(func.count()).select_from(base.subquery())
    total = int(session.scalar(count_stmt) or 0)
    rows = list(
        session.scalars(base.order_by(SyncRunRow.id.desc()).limit(limit).offset(offset)).all()
    )
    return rows, total


def save_experiment(session: Session, row: ExperimentRow) -> None:
    session.add(row)
    session.commit()


def get_experiment(session: Session, experiment_id: str) -> ExperimentRow | None:
    return session.get(ExperimentRow, experiment_id)


def list_experiments(session: Session, *, limit: int = 50) -> list[ExperimentRow]:
    stmt = select(ExperimentRow).order_by(ExperimentRow.created_at.desc()).limit(limit)
    return list(session.scalars(stmt).all())
