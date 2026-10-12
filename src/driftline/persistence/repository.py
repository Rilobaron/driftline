"""Repository helpers for candles and backtest runs."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from driftline.domain.models import Candle
from driftline.market_data.provider import merge_incremental
from driftline.persistence.models import BacktestRow, CandleRow


def _ts_key(ts: datetime) -> datetime:
    """Backend-agnostic timestamp key (SQLite returns naive datetimes)."""
    return ts.replace(tzinfo=None) if ts.tzinfo is not None else ts


def upsert_candles(session: Session, symbol: str, timeframe: str, candles: list[Candle]) -> int:
    """Insert-or-ignore candles (idempotent incremental sync). Returns new rows."""
    existing_ts = {
        _ts_key(t)
        for t in session.scalars(
            select(CandleRow.ts).where(CandleRow.symbol == symbol, CandleRow.timeframe == timeframe)
        ).all()
    }
    fresh = [c for c in candles if _ts_key(c.ts) not in existing_ts]
    for c in fresh:
        existing_ts.add(_ts_key(c.ts))
        session.add(
            CandleRow(
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
) -> list[Candle]:
    stmt = (
        select(CandleRow)
        .where(CandleRow.symbol == symbol, CandleRow.timeframe == timeframe)
        .order_by(CandleRow.ts.asc())
    )
    if since is not None:
        stmt = stmt.where(CandleRow.ts >= since)
    rows = session.scalars(stmt).all()
    rows = rows[-limit:] if len(rows) > limit else rows
    out = [
        Candle(ts=r.ts, open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume)
        for r in rows
    ]
    # Defensive: DB unique constraint should prevent dupes; merge guards anyway.
    return merge_incremental([], out)


def save_backtest(session: Session, row: BacktestRow) -> None:
    session.add(row)
    session.commit()


def get_backtest(session: Session, backtest_id: str) -> BacktestRow | None:
    return session.get(BacktestRow, backtest_id)
