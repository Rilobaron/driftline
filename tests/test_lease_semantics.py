"""Lease truth-table regression tests (deterministic via injected clock)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from driftline.persistence.repository import (
    acquire_stream_lock,
    get_or_create_stream,
    release_stream_lock,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _stream(session: Session):  # type: ignore[no-untyped-def]
    return get_or_create_stream(session, exchange="binance", symbol="BTC/USDT", timeframe="1h")


def test_acquire_deny_steal_renew_release(db_session: Session) -> None:
    row = _stream(db_session)
    assert acquire_stream_lock(db_session, row, owner="A", lease_seconds=60, now=T0) is True
    # Held, unexpired, other owner -> deny.
    assert acquire_stream_lock(db_session, row, owner="B", lease_seconds=60, now=T0) is False
    assert (
        acquire_stream_lock(
            db_session, row, owner="B", lease_seconds=60, now=T0 + timedelta(seconds=30)
        )
        is False
    )
    # Owner renews its own lease (extends to T0+90).
    assert (
        acquire_stream_lock(
            db_session, row, owner="A", lease_seconds=60, now=T0 + timedelta(seconds=30)
        )
        is True
    )
    # Still valid after renewal -> deny; expired lease is stolen.
    assert (
        acquire_stream_lock(
            db_session, row, owner="B", lease_seconds=60, now=T0 + timedelta(seconds=61)
        )
        is False
    )
    assert (
        acquire_stream_lock(
            db_session, row, owner="B", lease_seconds=60, now=T0 + timedelta(seconds=91)
        )
        is True
    )
    # Non-owner release is a no-op; owner release clears.
    release_stream_lock(db_session, row, owner="A")
    assert (
        acquire_stream_lock(
            db_session, row, owner="B", lease_seconds=60, now=T0 + timedelta(seconds=62)
        )
        is True
    )
    release_stream_lock(db_session, row, owner="B")
    assert (
        acquire_stream_lock(
            db_session, row, owner="A", lease_seconds=60, now=T0 + timedelta(seconds=63)
        )
        is True
    )


def test_lease_actually_lasts_lease_seconds(db_session: Session) -> None:
    """Regression: lease_seconds must extend the lock (previously ignored)."""
    row = _stream(db_session)
    assert acquire_stream_lock(db_session, row, owner="A", lease_seconds=600, now=T0) is True
    # 599s later the lease still holds against another owner.
    assert (
        acquire_stream_lock(
            db_session, row, owner="B", lease_seconds=600, now=T0 + timedelta(seconds=599)
        )
        is False
    )
    assert (
        acquire_stream_lock(
            db_session, row, owner="B", lease_seconds=600, now=T0 + timedelta(seconds=601)
        )
        is True
    )
