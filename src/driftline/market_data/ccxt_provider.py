"""CCXT-backed historical OHLCV provider (public endpoints, no auth)."""

from __future__ import annotations

import time
from typing import Any

from driftline.core.exceptions import InaccessibleExchangeError, MarketDataError, ValidationError
from driftline.core.logging import get_logger
from driftline.domain.models import Candle
from driftline.market_data import resilience as resilience_mod
from driftline.market_data.resilience import RetryConfig
from driftline.market_data.validation import normalize_raw_ohlcv, timeframe_to_seconds

log = get_logger("market_data.ccxt")

_GEO_HINTS = ("restricted", "451", "geographic", "geo-block", "unavailable for", "forbidden")


class CcxtMarketDataProvider:
    """Paginated CCXT OHLCV fetcher with retries and rate-limit respect."""

    name: str

    def __init__(
        self,
        exchange_id: str = "binance",
        *,
        rate_limit: bool = True,
        timeout_seconds: float = 30.0,
        max_retries: int = 5,
    ) -> None:
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
        timeout_ms = int(max(1.0, timeout_seconds) * 1000.0)
        self._exchange: Any = getattr(ccxt, exchange_id)(
            {"enableRateLimit": rate_limit, "timeout": timeout_ms}
        )
        self._retry = RetryConfig(max_retries=max(0, max_retries))
        self._available: bool | None = None

    @property
    def provider_available(self) -> bool | None:
        """Last known availability (None = unknown, never fabricate)."""
        return self._available

    def validate_market(self, symbol: str, timeframe: str) -> None:
        """Fail fast on malformed/derivative markets without network calls."""
        from driftline.market_data.streams import validate_symbol, validate_timeframe

        try:
            validate_symbol(symbol)
            validate_timeframe(timeframe)
        except ValidationError as exc:
            raise MarketDataError(str(exc)) from exc
        # Static CCXT capability check (no network): timeframe advertised?
        try:
            timeframes = getattr(self._exchange, "timeframes", None)
            # Not all exchanges publish this map; only reject when the map
            # exists, is non-empty, and lacks the timeframe.
            if isinstance(timeframes, dict) and timeframes and timeframe not in timeframes:
                raise MarketDataError(
                    f"exchange {self.name!r} does not advertise timeframe {timeframe!r}"
                )
            has = getattr(self._exchange, "has", {})
            if isinstance(has, dict) and has.get("fetchOHLCV") is False:
                raise MarketDataError(f"exchange {self.name!r} does not support fetchOHLCV")
        except MarketDataError:
            raise
        except Exception:
            pass

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        *,
        since_ms: int | None = None,
        limit: int = 1000,
    ) -> list[Candle]:
        self.validate_market(symbol, timeframe)
        # Normalize identity once: "btc/usdt"+"1H" must hit the same exchange
        # market and DB rows as the canonical "BTC/USDT"+"1h".
        from driftline.market_data.streams import validate_symbol, validate_timeframe

        symbol = validate_symbol(symbol)
        timeframe = validate_timeframe(timeframe)
        try:
            raw = self._exchange.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=limit)
        except Exception as exc:  # ccxt raises rich typed errors; map to domain errors
            message = str(exc).lower()
            self._available = False
            if any(h in message for h in _GEO_HINTS):
                raise InaccessibleExchangeError(
                    f"exchange {self.name!r} is inaccessible ({exc}). "
                    "Configure another provider, e.g. DRIFTLINE_EXCHANGE=kraken.",
                ) from exc
            raise MarketDataError(f"failed to fetch OHLCV from {self.name}: {exc}") from exc
        self._available = True
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
        self.validate_market(symbol, timeframe)
        from driftline.market_data.streams import validate_symbol, validate_timeframe

        symbol = validate_symbol(symbol)
        timeframe = validate_timeframe(timeframe)
        retry = RetryConfig(max_retries=max(0, max_retries))
        interval_ms = timeframe_to_seconds(timeframe) * 1000
        collected: list[Candle] = []
        cursor = since_ms
        page_size = max(1, min(page_limit, 1000))
        label = f"{self.name}:{symbol}:{timeframe}"
        while len(collected) < total_limit:

            def _fetch_page(page_cursor: int | None = cursor) -> list[Candle]:
                return self.fetch_ohlcv(symbol, timeframe, since_ms=page_cursor, limit=page_size)

            batch = resilience_mod.run_with_retry(
                _fetch_page,
                retry,
                stream_label=label,
            )
            if not batch:
                break
            # Malformed-row protection: normalize_raw_ohlcv raises ValidationError
            # (a permanent error) instead of silently interpolating.
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
