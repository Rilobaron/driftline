"""Market-data normalization, duplicates, gaps, incomplete candles."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from driftline.core.exceptions import ValidationError
from driftline.domain.models import Candle
from driftline.market_data.fixtures import make_trend_candles
from driftline.market_data.validation import (
    analyze_gaps,
    deduplicate,
    drop_incomplete_last_candle,
    normalize_raw_ohlcv,
)


def test_normalize_converts_ms_timestamps_to_utc() -> None:
    rows = [[1704067200000.0, 100.0, 101.0, 99.0, 100.5, 10.0]]
    (candle,) = normalize_raw_ohlcv(rows)
    assert candle.ts == datetime(2024, 1, 1, tzinfo=UTC)
    assert candle.close == 100.5


def test_normalize_rejects_bad_range() -> None:
    with pytest.raises(ValidationError):
        normalize_raw_ohlcv([[1704067200000.0, 100.0, 99.0, 99.0, 100.0, 1.0]])


def test_normalize_rejects_non_positive_price() -> None:
    with pytest.raises(ValidationError):
        normalize_raw_ohlcv([[1704067200000.0, 0.0, 1.0, 0.0, 1.0, 1.0]])


def test_duplicate_and_gap_detection() -> None:
    candles = make_trend_candles(n=10)
    doubled = candles + [candles[3]]
    report = analyze_gaps(doubled, "1h")
    assert candles[3].ts in report.duplicates
    assert report.missing == []

    gapped = [c for i, c in enumerate(candles) if i != 5]
    report2 = analyze_gaps(gapped, "1h")
    assert report2.duplicates == []
    assert len(report2.missing) == 1
    assert report2.missing[0] == candles[5].ts


def test_deduplicate_keeps_first() -> None:
    candles = make_trend_candles(n=5)
    out = deduplicate(candles + [candles[0]])
    assert len(out) == 5


def test_drop_incomplete_last_candle() -> None:
    candles = make_trend_candles(n=5)
    last_ts = candles[-1].ts
    # "now" inside the last candle's hour -> last candle is forming.
    kept = drop_incomplete_last_candle(candles, "1h", now=last_ts + timedelta(minutes=10))
    assert len(kept) == 4
    # "now" after the hour -> complete.
    kept2 = drop_incomplete_last_candle(candles, "1h", now=last_ts + timedelta(hours=2))
    assert len(kept2) == 5


def test_candle_model_normalizes_naive_ts() -> None:
    c = Candle(ts=datetime(2024, 1, 1, 12, 0, 0), open=1.0, high=1.0, low=1.0, close=1.0)
    assert c.ts.tzinfo is not None
