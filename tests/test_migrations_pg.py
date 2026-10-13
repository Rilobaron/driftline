"""Migration regression tests (require real PostgreSQL; skipped otherwise).

These tests apply the actual Alembic migrations — they never mark success
without applying. Set DRIFTLINE_TEST_PG_URL, e.g.::

    postgresql+psycopg://driftline:driftline@localhost:5433/driftline_audit
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic.config import Config

from alembic import command

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


def _alembic_config(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _fresh_db(url: str, db_name: str) -> str:
    admin_url = url.rsplit("/", 1)[0] + "/postgres"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        conn.execute(sa.text(f'CREATE DATABASE "{db_name}"'))
    admin.dispose()
    return url.rsplit("/", 1)[0] + f"/{db_name}"


@requires_pg
def test_single_head() -> None:
    from alembic.script import ScriptDirectory

    cfg = _alembic_config(PG_URL)
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert heads == ["0003_merge_v02"], f"expected exactly one head, got {heads}"


@requires_pg
def test_fresh_upgrade_head_and_no_drift() -> None:
    url = _fresh_db(PG_URL, "driftline_audit_fresh")
    command.upgrade(_alembic_config(url), "head")
    # alembic check must report no drift between models metadata and migrated DB.
    command.check(_alembic_config(url))
    engine = sa.create_engine(url)
    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                sa.text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            )
        }
        assert {"candles", "backtests", "market_streams", "sync_runs", "experiments"} <= tables
        constraints = {
            row[0]
            for row in conn.execute(
                sa.text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid IN (SELECT oid FROM pg_class WHERE relname IN "
                    "('candles','market_streams','sync_runs'))"
                )
            )
        }
        assert "uq_candle_exchange" in constraints
        assert "uq_market_stream" in constraints
        assert "uq_candle" not in constraints
        assert "uq_sync_run" not in constraints
        exchange_nullable = conn.execute(
            sa.text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name='candles' AND column_name='exchange'"
            )
        ).scalar()
        assert exchange_nullable == "NO"
    engine.dispose()


@requires_pg
def test_v01_upgrade_path_preserves_data() -> None:
    url = _fresh_db(PG_URL, "driftline_audit_v01")
    cfg = _alembic_config(url)
    command.upgrade(cfg, "0001_init")
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO candles (symbol, timeframe, ts, open, high, low, close, volume)"
                " VALUES ('BTC/USDT','1h',:ts,100,101,99,100.5,10)"
            ),
            {"ts": datetime(2024, 1, 1, tzinfo=UTC)},
        )
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        row = conn.execute(sa.text("SELECT symbol, close, exchange FROM candles")).one()
        assert (row[0], float(row[1]), row[2]) == ("BTC/USDT", 100.5, "binance")
        # Old uniqueness replaced by the exchange-scoped variant.
        names = {
            r[0]
            for r in conn.execute(
                sa.text("SELECT conname FROM pg_constraint WHERE conrelid = 'candles'::regclass")
            )
        }
        assert "uq_candle_exchange" in names
        assert "uq_candle" not in names
    engine.dispose()
    # Repository layer works against the migrated schema.
    from sqlalchemy.orm import sessionmaker

    import driftline.persistence.database as db_module
    from driftline.market_data.fixtures import make_trend_candles
    from driftline.persistence.database import get_engine
    from driftline.persistence.repository import load_candles, upsert_candles

    db_module.reset_engine()
    eng = get_engine(url)
    session = sessionmaker(bind=eng, expire_on_commit=False)()
    try:
        fresh = [c for c in make_trend_candles(n=5) if c.ts.year == 2024]
        assert len(fresh) == 5
        added = upsert_candles(session, "BTC/USDT", "1h", fresh)
        # The migrated v0.1 row (2024-01-01T00:00) collides with the first
        # fixture candle and must be treated as a duplicate, not an error.
        assert added == 4
        loaded = load_candles(session, "BTC/USDT", "1h", limit=100)
        assert len(loaded) == 5  # migrated row + 4 new
        assert loaded[0].ts < loaded[-1].ts
    finally:
        session.close()
        db_module.reset_engine()
        eng.dispose()
