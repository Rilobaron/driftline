"""Multi-market stream configuration and validation.

Default streams (spot only, no leverage/futures):
  BTC/USDT 1h, ETH/USDT 1h, SOL/USDT 1h on the configured exchange.

Streams are configured via ``DRIFTLINE_MARKET_STREAMS`` as a comma-separated
list of ``exchange:SYMBOL:timeframe`` entries, e.g.::

    binance:BTC/USDT:1h,binance:ETH/USDT:1h,kraken:BTC/USD:1h

A leading ``!`` (or ``-``) disables a stream without removing it::

    binance:BTC/USDT:1h,!binance:SOL/USDT:1h
"""

from __future__ import annotations

from dataclasses import dataclass

from driftline.core.exceptions import ValidationError
from driftline.market_data.validation import timeframe_to_seconds


@dataclass(frozen=True)
class MarketStream:
    exchange: str
    symbol: str
    timeframe: str
    enabled: bool = True

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.symbol}:{self.timeframe}"

    @property
    def stream_id(self) -> str:
        return self.key


def validate_symbol(symbol: str) -> str:
    """Validate a spot symbol like ``BTC/USDT`` (no futures/leverage markers)."""
    cleaned = symbol.strip().upper()
    if not cleaned or "/" not in cleaned:
        raise ValidationError(f"unsupported market symbol: {symbol!r} (expected BASE/QUOTE)")
    base, _, quote = cleaned.partition("/")
    if not base or not quote or len(base) > 16 or len(quote) > 16:
        raise ValidationError(f"unsupported market symbol: {symbol!r}")
    for marker in (":", "PERP", "SWAP"):
        if marker in cleaned:
            raise ValidationError(f"derivatives not supported (spot only): {symbol!r}")
    if any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789./" for ch in cleaned):
        raise ValidationError(f"unsupported market symbol: {symbol!r}")
    return cleaned


def validate_timeframe(timeframe: str) -> str:
    cleaned = timeframe.strip()
    timeframe_to_seconds(cleaned)  # raises ValidationError on unsupported values
    return cleaned


def validate_exchange(exchange: str) -> str:
    cleaned = exchange.strip().lower()
    if not cleaned or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in cleaned):
        raise ValidationError(f"unsupported exchange: {exchange!r}")
    return cleaned


def parse_stream_entry(entry: str) -> MarketStream:
    text = entry.strip()
    if not text:
        raise ValidationError("empty market stream entry")
    enabled = True
    if text.startswith(("!", "-")):
        enabled = False
        text = text[1:].strip()
    parts = text.split(":")
    if len(parts) != 3:
        raise ValidationError(
            f"invalid market stream {entry!r} (expected exchange:SYMBOL:timeframe)"
        )
    exchange = validate_exchange(parts[0])
    symbol = validate_symbol(parts[1])
    timeframe = validate_timeframe(parts[2])
    return MarketStream(exchange=exchange, symbol=symbol, timeframe=timeframe, enabled=enabled)


def parse_market_streams(raw: str) -> list[MarketStream]:
    """Parse the full streams setting; dedupe by key (last entry wins)."""
    if not raw.strip():
        return []
    by_key: dict[str, MarketStream] = {}
    for entry in raw.split(","):
        if not entry.strip():
            continue
        stream = parse_stream_entry(entry)
        by_key[stream.key] = stream
    return list(by_key.values())


def default_streams() -> list[MarketStream]:
    from driftline.core.config import DEFAULT_MARKET_STREAMS

    return parse_market_streams(DEFAULT_MARKET_STREAMS)


def ensure_legacy_stream(exchange: str, symbol: str, timeframe: str) -> MarketStream:
    """Preserve v0.1 single-market behaviour as one enabled stream."""
    return MarketStream(
        exchange=validate_exchange(exchange),
        symbol=validate_symbol(symbol),
        timeframe=validate_timeframe(timeframe),
        enabled=True,
    )
