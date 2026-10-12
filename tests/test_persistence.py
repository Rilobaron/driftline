"""Persistence integration tests (SQLite; PostgreSQL compatible schema)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from driftline.market_data.fixtures import make_trend_candles
from driftline.persistence.repository import load_candles, upsert_candles


def test_upsert_is_idempotent(db_session: Session) -> None:
    candles = make_trend_candles(n=20)
    first = upsert_candles(db_session, "BTC/USDT", "1h", candles)
    assert first == 20
    second = upsert_candles(db_session, "BTC/USDT", "1h", candles)
    assert second == 0
    loaded = load_candles(db_session, "BTC/USDT", "1h")
    assert len(loaded) == 20
    assert loaded[0].ts < loaded[-1].ts


def test_incremental_sync_appends_only_new(db_session: Session) -> None:
    candles = make_trend_candles(n=30)
    upsert_candles(db_session, "BTC/USDT", "1h", candles[:20])
    added = upsert_candles(db_session, "BTC/USDT", "1h", candles[10:])
    assert added == 10
    assert len(load_candles(db_session, "BTC/USDT", "1h")) == 30
