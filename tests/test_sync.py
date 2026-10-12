"""Sync service tests (provider mocked — no network)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from driftline.market_data.ccxt_provider import CcxtMarketDataProvider
from driftline.market_data.fixtures import make_trend_candles
from driftline.market_data.service import load_research_candles, sync_history


def test_sync_history_is_incremental(db_session: Session, monkeypatch: object) -> None:
    candles = make_trend_candles(n=30)
    monkeypatch.setattr(  # type: ignore[attr-defined]
        CcxtMarketDataProvider,
        "fetch_history",
        lambda self, *args, **kwargs: list(candles),
    )
    first = sync_history(db_session, symbol="BTC/USDT", timeframe="1h", lookback_months=1)
    assert first.stored_new == 30
    assert first.total_stored == 30
    assert first.missing == 0
    assert first.data_source == "exchange:binance"

    second = sync_history(db_session, symbol="BTC/USDT", timeframe="1h", lookback_months=1)
    assert second.stored_new == 0
    assert second.total_stored == 30

    research = load_research_candles(db_session, "BTC/USDT", "1h", limit=100)
    assert len(research) == 30
    assert research[0].ts < research[-1].ts
