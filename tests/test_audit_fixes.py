"""Audit regression tests for v0.2 integration fixes.

Covers: bootstrap completion evidence rule, walk-forward all-undefined
objective fallback + warmup feasibility, open-position window warnings, and
market-identity case normalization.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from driftline.backtesting.engine import BacktestConfig
from driftline.domain.models import Candle
from driftline.market_data.fixtures import make_oscillating_candles
from driftline.research.splits import run_split_test
from driftline.research.walk_forward import (
    WalkForwardConfig,
    run_walk_forward,
)
from driftline.research.windows import evaluate_window
from driftline.strategies.donchian_breakout import DonchianBreakoutStrategy
from driftline.strategies.registry import default_registry

CONFIG = BacktestConfig()


def _hourly(n: int, end: datetime, *, start_price: float = 100.0) -> list[Candle]:
    t0 = end - timedelta(hours=n - 1)
    return [
        Candle(
            ts=t0 + timedelta(hours=i),
            open=start_price,
            high=start_price + 1,
            low=start_price - 1,
            close=start_price,
            volume=10.0,
        )
        for i in range(n)
    ]


class _ScriptedProvider:
    """Deterministic stand-in for CcxtMarketDataProvider (no network)."""

    name = "binance"

    def __init__(self, candles: list[Candle]) -> None:
        self._candles = candles

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
        out = list(self._candles)
        if since_ms is not None:
            out = [c for c in out if c.ts.timestamp() * 1000 >= since_ms]
        return out[:total_limit]

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        *,
        since_ms: int | None = None,
        limit: int = 1000,
    ) -> list[Candle]:
        return self.fetch_history(symbol, timeframe, total_limit=limit, since_ms=since_ms)


def test_bootstrap_not_done_after_bounded_partial_run(db_session: Session) -> None:
    from driftline.market_data.sync import sync_stream

    now = datetime(2026, 10, 12, 20, 0, tzinfo=UTC)
    candles = _hourly(300, datetime(2026, 10, 12, 19, 0, tzinfo=UTC))
    report = sync_stream(
        db_session,
        exchange="binance",
        symbol="BTC/USDT",
        timeframe="1h",
        provider=_ScriptedProvider(candles),
        now=now,
        kind="bootstrap",
        total_limit=300,
        bootstrap_target=8760,
    )
    assert report.success is True
    assert report.total_stored == 300
    # Bounded partial pass must NOT complete the bootstrap.
    from driftline.persistence.repository import find_stream

    row = find_stream(db_session, exchange="binance", symbol="BTC/USDT", timeframe="1h")
    assert row is not None
    assert row.bootstrap_done is False


def test_bootstrap_done_when_provider_exhausted(db_session: Session) -> None:
    from driftline.market_data.sync import sync_stream
    from driftline.persistence.repository import find_stream

    now = datetime(2026, 10, 12, 20, 0, tzinfo=UTC)
    # Provider holds only 50 candles although 300 were requested: exhausted.
    candles = _hourly(50, datetime(2026, 10, 12, 19, 0, tzinfo=UTC))
    report = sync_stream(
        db_session,
        exchange="binance",
        symbol="BTC/USDT",
        timeframe="1h",
        provider=_ScriptedProvider(candles),
        now=now,
        kind="bootstrap",
        total_limit=300,
        bootstrap_target=8760,
    )
    assert report.success is True
    row = find_stream(db_session, exchange="binance", symbol="BTC/USDT", timeframe="1h")
    assert row is not None
    assert row.bootstrap_done is True


def test_walk_forward_undefined_objective_freezes_first_candidate() -> None:
    flat = _hourly(900, datetime(2024, 5, 1, tzinfo=UTC))
    wf = WalkForwardConfig(
        train_window=500,
        test_window=100,
        step=100,
        objective="sharpe_ratio",
        min_trades=0,
    )
    report = run_walk_forward(
        flat,
        "ema_crossover",
        {"fast_period": [10, 20], "slow_period": [40, 60]},
        CONFIG,
        wf,
        default_registry(),
    )
    ok_folds = [f for f in report.folds if f.status == "ok"]
    assert ok_folds, "flat market must still evaluate (no misleading skips)"
    for fold in ok_folds:
        assert fold.selected_params == {"fast_period": 10, "slow_period": 40, "atr_period": 14}
        assert any("undefined" in w for w in fold.warnings)


def test_walk_forward_rejects_infeasible_warmup() -> None:
    candles = make_oscillating_candles(n=1200)
    wf = WalkForwardConfig(train_window=200, test_window=100, step=100, min_trades=0)
    with pytest.raises(ValueError, match="warmup"):
        run_walk_forward(
            candles,
            "ema_crossover",
            {"fast_period": [10], "slow_period": [500]},
            CONFIG,
            wf,
            default_registry(),
        )


def _rising(n: int) -> list[Candle]:
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    out = []
    for i in range(n):
        close = 100.0 + i * 0.5
        out.append(
            Candle(
                ts=t0 + timedelta(hours=i),
                open=close,
                high=close + 0.1,
                low=close - 0.1,
                close=close,
                volume=10.0,
            )
        )
    return out


def test_open_position_window_warning() -> None:
    candles = _rising(120)
    evaluation = evaluate_window(
        candles, DonchianBreakoutStrategy(), CONFIG, eval_start=0, eval_end=120
    )
    assert evaluation.equity_curve[-1].position_qty > 0
    assert any("position open" in w for w in evaluation.warnings)
    # Closed/flat evaluations carry no such warning.
    flat_eval = evaluate_window(
        _hourly(120, datetime(2024, 5, 1, tzinfo=UTC)),
        DonchianBreakoutStrategy(),
        CONFIG,
        eval_start=0,
        eval_end=120,
    )
    assert not any("position open" in w for w in flat_eval.warnings)


def test_split_surfaces_open_position_warning() -> None:
    report = run_split_test(_rising(400), "donchian_breakout", None, CONFIG, default_registry())
    assert report.in_sample is not None and report.out_sample is not None
    assert any("position open" in w for w in report.in_sample.warnings)


def test_market_identity_case_normalization(db_session: Session) -> None:
    from fastapi.testclient import TestClient

    from driftline.api import main as api_main
    from driftline.persistence.repository import upsert_candles

    upsert_candles(db_session, "BTC/USDT", "1h", _hourly(60, datetime(2024, 3, 1, tzinfo=UTC)))

    def _override():  # type: ignore[no-untyped-def]
        yield db_session

    api_main.app.dependency_overrides[api_main.get_db] = _override
    try:
        with TestClient(api_main.app) as client:
            canonical = client.get("/api/v1/market/candles", params={"limit": 60})
            lowered = client.get(
                "/api/v1/market/candles",
                params={"symbol": "btc/usdt", "timeframe": "1H", "limit": 60},
            )
            assert canonical.status_code == 200 and lowered.status_code == 200
            # Lowercase request must hit the SAME stored rows, not fixtures.
            assert lowered.headers["x-driftline-data-source"] == "exchange:binance"
            assert [r["ts"] for r in lowered.json()] == [r["ts"] for r in canonical.json()]
    finally:
        api_main.app.dependency_overrides.clear()


def test_timeframe_normalization() -> None:
    from driftline.market_data.streams import validate_symbol, validate_timeframe

    assert validate_symbol("btc/usdt") == "BTC/USDT"
    assert validate_timeframe("1H") == "1h"
