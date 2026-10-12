"""Market-data validation, normalization, and gap analysis."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from driftline.core.exceptions import ValidationError
from driftline.domain.models import Candle

_TIMEFRAME_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def timeframe_to_seconds(timeframe: str) -> int:
    m = _TIMEFRAME_RE.match(timeframe.strip())
    if not m:
        raise ValidationError(f"unsupported timeframe: {timeframe!r}")
    amount, unit = int(m.group(1)), m.group(2).lower()
    if amount <= 0:
        raise ValidationError(f"unsupported timeframe: {timeframe!r}")
    return amount * _UNIT_SECONDS[unit]


def normalize_raw_ohlcv(rows: list[list[float]]) -> list[Candle]:
    """Convert raw CCXT OHLCV rows ``[ts_ms, o, h, l, c, v]`` into Candles.

    Raises ValidationError on malformed records. Does not fabricate data:
    invalid rows raise instead of being interpolated.
    """
    candles: list[Candle] = []
    for row in rows:
        if len(row) < 6:
            raise ValidationError(f"malformed OHLCV row (expected 6 fields): {row!r}")
        ts_ms, o, h, low, c, v = (float(x) for x in row[:6])
        if ts_ms <= 0:
            raise ValidationError(f"invalid candle timestamp: {row!r}")
        for name, val in (("open", o), ("high", h), ("low", low), ("close", c)):
            if not (val > 0):
                raise ValidationError(f"invalid {name}={val} in row {row!r}")
        if v < 0:
            raise ValidationError(f"invalid volume={v} in row {row!r}")
        if not (h >= max(o, c) and low <= min(o, c) and h >= low):
            raise ValidationError(f"inconsistent OHLC range in row {row!r}")
        candles.append(
            Candle(
                ts=datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC),
                open=o,
                high=h,
                low=low,
                close=c,
                volume=v,
            )
        )
    candles.sort(key=lambda c: c.ts)
    return candles


@dataclass(frozen=True)
class GapReport:
    duplicates: list[datetime]
    missing: list[datetime]
    expected_interval_seconds: int


def analyze_gaps(candles: list[Candle], timeframe: str) -> GapReport:
    """Detect duplicate timestamps and missing candles for a fixed timeframe."""
    interval = timeframe_to_seconds(timeframe)
    seen: set[datetime] = set()
    duplicates: list[datetime] = []
    for c in candles:
        if c.ts in seen:
            duplicates.append(c.ts)
        seen.add(c.ts)
    missing: list[datetime] = []
    ordered = sorted(seen)
    for prev, nxt in zip(ordered, ordered[1:], strict=False):
        gap = (nxt - prev).total_seconds()
        steps = round(gap / interval)
        if steps > 1:
            for k in range(1, steps):
                missing.append(prev + k * __import__("datetime").timedelta(seconds=interval))
    return GapReport(duplicates=duplicates, missing=missing, expected_interval_seconds=interval)


def deduplicate(candles: list[Candle]) -> list[Candle]:
    """Keep the first occurrence of each timestamp, preserving order."""
    seen: set[datetime] = set()
    out: list[Candle] = []
    for c in sorted(candles, key=lambda c: c.ts):
        if c.ts in seen:
            continue
        seen.add(c.ts)
        out.append(c)
    return out


def drop_incomplete_last_candle(
    candles: list[Candle], timeframe: str, *, now: datetime | None = None
) -> list[Candle]:
    """Exclude the still-forming candle from strategy calculations.

    A candle starting at ``ts`` with interval ``dt`` is complete only when
    ``now >= ts + dt``. Live exchange queries usually include the forming
    candle; it must not feed indicators or signals.
    """
    if not candles:
        return []
    interval = timeframe_to_seconds(timeframe)
    ref = now or datetime.now(UTC)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    last = candles[-1]
    if ref < last.ts + __import__("datetime").timedelta(seconds=interval):
        return candles[:-1]
    return list(candles)
