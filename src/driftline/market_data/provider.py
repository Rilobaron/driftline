"""MarketDataProvider interface and shared ingestion helpers."""

from __future__ import annotations

from typing import Protocol

from driftline.domain.models import Candle


class MarketDataProvider(Protocol):
    """Interface for historical OHLCV providers (CCXT, CSV, fixtures)."""

    name: str

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        *,
        since_ms: int | None = None,
        limit: int = 1000,
    ) -> list[Candle]:
        """Fetch one page of ascending, normalized candles."""
        ...


def merge_incremental(existing: list[Candle], incoming: list[Candle]) -> list[Candle]:
    """Merge incremental sync results without duplicating timestamps."""
    by_ts = {c.ts: c for c in existing}
    for c in incoming:
        by_ts[c.ts] = c
    return sorted(by_ts.values(), key=lambda c: c.ts)
