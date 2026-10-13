"""PostgreSQL concurrency tests: leases, unique constraints, rollback.

Requires real PostgreSQL (SQLite cannot prove locking correctness).
Set DRIFTLINE_TEST_PG_URL, e.g.::

    postgresql+psycopg://driftline:driftline@localhost:5433/driftline_audit
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

PG_URL = os.environ.get("DRIFTLINE_TEST_PG_URL", "")


def _pg_available() -> bool:
    if not PG_URL:
        return False
    try:
        engine = sa.create_engine(PG_URL)
        with engine.connect():
            pass
        engine.dispose()
        return True
    except Exception:
        return False


requires_pg = pytest.mark.skipif(not _pg_available(), reason="PostgreSQL unavailable")


def _fresh_db(db_name: str) -> str:
    admin_url = PG_URL.rsplit("/", 1)[0] + "/postgres"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        conn.execute(sa.text(f'CREATE DATABASE "{db_name}"'))
    admin.dispose()
    return PG_URL.rsplit("/", 1)[0] + f"/{db_name}"


def _engine(url: str) -> sa.Engine:
    from driftline.persistence.models import Base

    engine = sa.create_engine(url, pool_size=10, max_overflow=10)
    Base.metadata.create_all(engine)
    return engine


@requires_pg
def test_competing_workers_exactly_one_owner() -> None:
    from driftline.persistence.repository import acquire_stream_lock, get_or_create_stream

    url = _fresh_db("driftline_audit_lease_race")
    engine = _engine(url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    setup = factory()
    get_or_create_stream(setup, exchange="binance", symbol="BTC/USDT", timeframe="1h")
    setup.close()

    barrier = threading.Barrier(8)
    results: list[bool] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        from driftline.persistence.repository import find_stream

        session = factory()
        try:
            barrier.wait(timeout=30)
            row = find_stream(session, exchange="binance", symbol="BTC/USDT", timeframe="1h")
            assert row is not None
            won = acquire_stream_lock(session, row, owner=f"worker-{i}", lease_seconds=600)
            with lock:
                results.append(won)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(results) == 8
    assert sum(results) == 1, f"expected exactly one lease owner, got {sum(results)}"
    engine.dispose()


@requires_pg
def test_concurrent_stream_creation_single_row() -> None:
    from driftline.persistence.repository import get_or_create_stream

    url = _fresh_db("driftline_audit_stream_race")
    engine = _engine(url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    barrier = threading.Barrier(6)
    ids: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        session = factory()
        try:
            barrier.wait(timeout=30)
            row = get_or_create_stream(
                session, exchange="binance", symbol="ETH/USDT", timeframe="1h"
            )
            with lock:
                ids.append(row.id)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(ids) == 6
    assert len(set(ids)) == 1, "concurrent creation must converge on one row"
    engine.dispose()


@requires_pg
def test_concurrent_upserts_no_duplicates() -> None:
    from driftline.market_data.fixtures import make_trend_candles
    from driftline.persistence.repository import load_candles, upsert_candles

    url = _fresh_db("driftline_audit_upsert_race")
    engine = _engine(url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    candles = make_trend_candles(n=50)
    barrier = threading.Barrier(4)

    def worker(offset: int) -> None:
        session = factory()
        try:
            barrier.wait(timeout=30)
            upsert_candles(session, "BTC/USDT", "1h", candles[offset : offset + 30])
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(o,)) for o in (0, 10, 20, 5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    session = factory()
    try:
        loaded = load_candles(session, "BTC/USDT", "1h", limit=100)
        assert len(loaded) == 50
        assert len({c.ts for c in loaded}) == 50
    finally:
        session.close()
        engine.dispose()


@requires_pg
def test_unique_candle_constraint_enforced_at_db_level() -> None:
    from driftline.persistence.models import CandleRow

    url = _fresh_db("driftline_audit_unique")
    engine = _engine(url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        ts = datetime(2024, 1, 1, tzinfo=UTC)
        session.add(
            CandleRow(
                exchange="binance",
                symbol="BTC/USDT",
                timeframe="1h",
                ts=ts,
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=1.0,
            )
        )
        session.commit()
        session.add(
            CandleRow(
                exchange="binance",
                symbol="BTC/USDT",
                timeframe="1h",
                ts=ts,
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=1.0,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        # Same ts on a DIFFERENT exchange must succeed (exchange-scoped identity).
        session.add(
            CandleRow(
                exchange="kraken",
                symbol="BTC/USDT",
                timeframe="1h",
                ts=ts,
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=1.0,
            )
        )
        session.commit()
        count = session.scalar(sa.text("SELECT COUNT(*) FROM candles"))
        assert count == 2
    finally:
        session.close()
        engine.dispose()


@requires_pg
def test_rollback_after_failure_keeps_session_usable() -> None:
    from driftline.persistence.models import CandleRow

    url = _fresh_db("driftline_audit_rollback")
    engine = _engine(url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        ts = datetime(2024, 1, 1, tzinfo=UTC)
        session.add(
            CandleRow(
                exchange="binance",
                symbol="BTC/USDT",
                timeframe="1h",
                ts=ts,
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=1.0,
            )
        )
        session.commit()
        session.add(
            CandleRow(
                exchange="binance",
                symbol="BTC/USDT",
                timeframe="1h",
                ts=ts,
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=1.0,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        session.add(
            CandleRow(
                exchange="binance",
                symbol="BTC/USDT",
                timeframe="1h",
                ts=datetime(2024, 1, 1, 1, tzinfo=UTC),
                open=2.0,
                high=2.0,
                low=2.0,
                close=2.0,
                volume=1.0,
            )
        )
        session.commit()
        assert session.scalar(sa.text("SELECT COUNT(*) FROM candles")) == 2
    finally:
        session.close()
        engine.dispose()
