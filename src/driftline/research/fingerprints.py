"""Dataset fingerprinting for reproducible research."""

from __future__ import annotations

import hashlib

from driftline.domain.models import Candle


def dataset_fingerprint(
    candles: list[Candle], symbol: str, timeframe: str, *, exchange: str = "binance"
) -> str:
    """Stable SHA-256 over exchange, symbol, timeframe, and full OHLCV content.

    Any change in composition or values changes the fingerprint, so an
    experiment record pins the exact dataset it ran on. Provenance includes
    the exchange: candles from different exchanges never share a fingerprint.
    """
    digest = hashlib.sha256()
    digest.update(f"{exchange}|{symbol}|{timeframe}|{len(candles)}|".encode())
    for c in candles:
        digest.update(
            f"{c.ts.isoformat()}|{c.open!r}|{c.high!r}|{c.low!r}|{c.close!r}|{c.volume!r};".encode()
        )
    return digest.hexdigest()
