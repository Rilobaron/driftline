"""Deterministic UTC candle-boundary clock helpers.

All scheduling decisions derive from UTC candle boundaries so behaviour is
independent of application startup time and fully testable with controlled
clocks (pass explicit ``now`` in tests).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from driftline.market_data.validation import timeframe_to_seconds


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def floor_to_candle(ts: datetime, timeframe: str) -> datetime:
    """Round ``ts`` down to the start of its candle for ``timeframe``."""
    interval = timeframe_to_seconds(timeframe)
    ref = ensure_utc(ts)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = int((ref - epoch).total_seconds())
    floored = (elapsed // interval) * interval
    return epoch + timedelta(seconds=floored)


def expected_last_completed(now: datetime, timeframe: str) -> datetime:
    """Most recent candle start that is fully complete at ``now``.

    A candle starting at ``ts`` with interval ``dt`` completes at ``ts + dt``.
    """
    interval = timeframe_to_seconds(timeframe)
    ref = ensure_utc(now)
    floored = floor_to_candle(ref, timeframe)
    if ref >= floored + timedelta(seconds=interval):
        return floored
    return floored - timedelta(seconds=interval)


def next_candle_close(now: datetime, timeframe: str) -> datetime:
    """Next candle close strictly after ``now`` (the forming candle's end)."""
    interval = timeframe_to_seconds(timeframe)
    return floor_to_candle(ensure_utc(now), timeframe) + timedelta(seconds=interval)


def next_run_at(now: datetime, timeframe: str, *, delay_seconds: int) -> datetime:
    """Next scheduled sync: shortly after the forming candle closes + delay."""
    return next_candle_close(now, timeframe) + timedelta(seconds=max(0, delay_seconds))


def expected_timestamps(start: datetime, end: datetime, timeframe: str) -> list[datetime]:
    """All expected candle starts in ``[start, end]`` for ``timeframe``."""
    interval = timeframe_to_seconds(timeframe)
    s, e = ensure_utc(start), ensure_utc(end)
    if e < s:
        return []
    out: list[datetime] = []
    cursor = floor_to_candle(s, timeframe)
    # Align up to first expected >= start.
    while cursor < s:
        cursor += timedelta(seconds=interval)
    while cursor <= e:
        out.append(cursor)
        cursor += timedelta(seconds=interval)
    return out
