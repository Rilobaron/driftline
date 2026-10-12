"""Demonstration runner: deterministic backtest on fixture or live data."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta

from driftline.backtesting.engine import BacktestConfig, run_backtest
from driftline.core.config import get_settings
from driftline.core.logging import configure_logging
from driftline.domain.models import Candle
from driftline.market_data.fixtures import make_oscillating_candles
from driftline.strategies.ema_crossover import EmaCrossoverStrategy


def run_demo(*, use_fixture: bool = True, limit: int = 500) -> dict[str, object]:
    settings = get_settings()
    data_source = "synthetic_fixture"
    candles: list[Candle]
    if use_fixture:
        candles = make_oscillating_candles(n=min(limit, 2000))
    else:  # pragma: no cover - requires network
        from driftline.market_data.ccxt_provider import CcxtMarketDataProvider
        from driftline.market_data.validation import drop_incomplete_last_candle

        lookback_months = max(1, settings.lookback_months)
        total = min(max(limit, lookback_months * 744 + 48), 9000)
        since_ms = int(
            (datetime.now(UTC) - timedelta(days=30 * lookback_months)).timestamp() * 1000
        )
        provider = CcxtMarketDataProvider(settings.exchange)
        fetched = provider.fetch_history(
            settings.symbol, settings.timeframe, total_limit=total, since_ms=since_ms
        )
        candles = drop_incomplete_last_candle(fetched, settings.timeframe)
        data_source = f"exchange:{settings.exchange}"

    strategy = EmaCrossoverStrategy()
    signals = strategy.generate(candles)
    config = BacktestConfig(
        initial_capital=settings.initial_capital,
        fee_rate=settings.fee_rate,
        slippage_rate=settings.slippage_rate,
        allocation=settings.allocation,
    )
    result = run_backtest(
        candles,
        signals,
        config,
        strategy_name=strategy.name,
        timeframe=settings.timeframe,
        data_source=data_source,
    )
    return {
        "data_source": data_source,
        "candles": len(candles),
        "signals": len(signals),
        "trades": len(result.trades),
        "metrics": result.metrics.model_dump(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a deterministic Driftline demo backtest")
    parser.add_argument("--live", action="store_true", help="fetch real exchange data")
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    configure_logging()
    print(json.dumps(run_demo(use_fixture=not args.live, limit=args.limit), indent=2))


if __name__ == "__main__":
    main()
