"""CCXT-backed historical OHLCV provider (public endpoints, no auth)."""

from __future__ import annotations

import time

from driftline.core.exceptions import InaccessibleExchangeError, MarketDataError
from driftline.core.logging import get_logger
from driftline.domain.models import Candle
from driftline.market_data.validation import normalize_raw_ohlcv, timeframe_to_seconds

log = get_logger("market_data.ccxt")

_GEO_HINTS = ("restricted", "451", "geographic", "geo-block", "unavailable for", "forbidden")


class CcxtMarketDataProvider:
    """Paginated CCXT OHLCV fetcher with retries and rate-limit respect."""

    name: str

    def __init__(self, exchange_id: str = "binance", *, rate_limit: bool = True) -> None:
        try:
            import ccxt
        except ImportError as exc:
            raise MarketDataError(
                "ccxt is not installed; install driftline with market-data support"
            ) from exc
        if not hasattr(ccxt, exchange_id):
            supported = [e for e in ccxt.exchanges if isinstance(e, str)]
            raise MarketDataError(
                f"unsupported exchange {exchange_id!r}; examples: {supported[:8]}"
            )
        self.name = exchange_id
        self._exchange = getattr(ccxt, exchange_id)({"enableRateLimit": rate_limit})

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        *,
        since_ms: int | None = None,
        limit: int = 1000,
    ) -> list[Candle]:
        try:
            raw = self._exchange.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=limit)
        except Exception as exc:  # ccxt raises rich typed errors; map to domain errors
            message = str(exc).lower()
            if any(h in message for h in _GEO_HINTS):
                raise InaccessibleExchangeError(
                    f"exchange {self.name!r} is inaccessible ({exc}). "
                    "Configure another provider, e.g. DRIFTLINE_EXCHANGE=kraken.",
                ) from exc
            raise MarketDataError(f"failed to fetch OHLCV from {self.name}: {exc}") from exc
        return normalize_raw_ohlcv([[float(x) for x in row] for row in raw])

    def fetch_history(
        self,
        symbol: str,
        timeframe: str,
        *,
        total_limit: int = 8760,
        since_ms: int | None = None,
        page_limit: int = 1000,
        max_retries: int = 5,
    ) -> list[Candle]:
        """Paginate backwards-safe ascending history up to ``total_limit`` candles."""
        interval_ms = timeframe_to_seconds(timeframe) * 1000
        collected: list[Candle] = []
        cursor = since_ms
        page_size = max(1, min(page_limit, 1000))
        while len(collected) < total_limit:
            batch: list[Candle] = []
            for attempt in range(max_retries + 1):
                try:
                    batch = self.fetch_ohlcv(symbol, timeframe, since_ms=cursor, limit=page_size)
                    break
                except InaccessibleExchangeError:
                    raise
                except MarketDataError as exc:
                    if attempt >= max_retries:
                        raise
                    backoff = min(2.0**attempt, 15.0)
                    log.warning(
                        "fetch failed (attempt %d): %s; retrying in %.1fs",
                        attempt + 1,
                        exc,
                        backoff,
                    )
                    time.sleep(backoff)
            if not batch:
                break
            if collected and batch[0].ts <= collected[-1].ts:
                # Advance cursor past overlap to guarantee progress.
                batch = [c for c in batch if c.ts > collected[-1].ts]
                if not batch:
                    cursor = int(collected[-1].ts.timestamp() * 1000) + interval_ms
                    continue
            collected.extend(batch)
            if len(batch) < page_size:
                break  # exhausted available history
            cursor = int(batch[-1].ts.timestamp() * 1000) + interval_ms
            rate_limit_s = getattr(self._exchange, "rateLimit", 0) / 1000.0
            if rate_limit_s > 0:
                time.sleep(rate_limit_s)
        return collected[:total_limit]
