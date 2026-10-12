"""Historical gap detection and bounded repair (never fabricate candles)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from driftline.market_data.clock import ensure_utc, expected_timestamps
from driftline.market_data.validation import timeframe_to_seconds


@dataclass(frozen=True)
class GapInterval:
    """One bounded re-request window: [start, end] inclusive candle starts."""

    start: datetime
    end: datetime

    def sized(self, timeframe: str) -> int:
        interval = timeframe_to_seconds(timeframe)
        delta = (ensure_utc(self.end) - ensure_utc(self.start)).total_seconds()
        return int(delta // interval) + 1


@dataclass(frozen=True)
class RepairReport:
    repaired: int
    still_missing: list[datetime] = field(default_factory=list)
    attempts: int = 0
    errors: list[str] = field(default_factory=list)


def find_missing(
    persisted: list[datetime],
    *,
    start: datetime,
    end: datetime,
    timeframe: str,
) -> list[datetime]:
    """Missing expected candle starts in ``[start, end]`` (ascending)."""
    have = {ensure_utc(t) for t in persisted}
    return [t for t in expected_timestamps(start, end, timeframe) if t not in have]


def group_intervals(
    missing: list[datetime],
    timeframe: str,
    *,
    max_candles_per_request: int = 1000,
) -> list[GapInterval]:
    """Group adjacent missing timestamps into bounded re-request intervals."""
    if not missing:
        return []
    interval = timeframe_to_seconds(timeframe)
    ordered = sorted(ensure_utc(t) for t in missing)
    groups: list[list[datetime]] = [[ordered[0]]]
    for ts in ordered[1:]:
        if (ts - groups[-1][-1]).total_seconds() == interval:
            groups[-1].append(ts)
        else:
            groups.append([ts])
    out: list[GapInterval] = []
    cap = max(1, max_candles_per_request)
    for group in groups:
        for i in range(0, len(group), cap):
            chunk = group[i : i + cap]
            out.append(GapInterval(start=chunk[0], end=chunk[-1]))
    return out


def interval_since_ms(start: datetime) -> int:
    return int(ensure_utc(start).timestamp() * 1000)


def interval_limit(interval: GapInterval, timeframe: str) -> int:
    seconds = timeframe_to_seconds(timeframe)
    span = (ensure_utc(interval.end) - ensure_utc(interval.start)).total_seconds()
    return int(span // seconds) + 1


def next_expected_after(last_ts: datetime | None, timeframe: str) -> datetime | None:
    if last_ts is None:
        return None
    return ensure_utc(last_ts) + timedelta(seconds=timeframe_to_seconds(timeframe))
