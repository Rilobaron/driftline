"""Historical sync orchestration: paginated fetch, validate, store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from driftline.core.logging import get_logger
from driftline.domain.models import Candle
from driftline.market_data.ccxt_provider import CcxtMarketDataProvider
from driftline.market_data.validation import (
    analyze_gaps,
    deduplicate,
    drop_incomplete_last_candle,
)
from driftline.persistence.repository import load_candles, upsert_candles

log = get_logger("market_data.sync")


@dataclass(frozen=True)
class SyncResult:
    symbol: str
    timeframe: str
    fetched: int
    stored_new: int
    total_stored: int
    duplicates: int
    missing: int
    data_source: str


def sync_history(
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    exchange: str = "binance",
    lookback_months: int = 12,
    max_candles: int = 9000,
) -> SyncResult:
    """Fetch ~``lookback_months`` of history and incrementally persist it."""
    provider = CcxtMarketDataProvider(exchange)
    since = datetime.now(UTC) - timedelta(days=30 * max(lookback_months, 1))
    since_ms = int(since.timestamp() * 1000)
    fetched = provider.fetch_history(symbol, timeframe, total_limit=max_candles, since_ms=since_ms)
    usable = drop_incomplete_last_candle(fetched, timeframe)
    usable = deduplicate(usable)
    report = analyze_gaps(usable, timeframe)
    if report.missing:
        log.warning(
            "%d missing %s candles for %s (%s..%s)",
            len(report.missing),
            timeframe,
            symbol,
            usable[0].ts if usable else "?",
            usable[-1].ts if usable else "?",
        )
    stored_new = upsert_candles(session, symbol, timeframe, usable)
    total = len(load_candles(session, symbol, timeframe, limit=max_candles))
    return SyncResult(
        symbol=symbol,
        timeframe=timeframe,
        fetched=len(fetched),
        stored_new=stored_new,
        total_stored=total,
        duplicates=len(report.duplicates),
        missing=len(report.missing),
        data_source=f"exchange:{exchange}",
    )


def load_research_candles(
    session: Session, symbol: str, timeframe: str, *, limit: int
) -> list[Candle]:
    """Load locally persisted candles for repeatable research (no network)."""
    return load_candles(session, symbol, timeframe, limit=limit)
