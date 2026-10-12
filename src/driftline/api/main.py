"""FastAPI application factory and versioned routes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from driftline import __version__
from driftline.api import schemas
from driftline.backtesting.engine import BacktestConfig, run_backtest
from driftline.core.config import get_settings
from driftline.core.exceptions import DriftlineError
from driftline.core.logging import configure_logging
from driftline.indicators import compute_all
from driftline.market_data.fixtures import make_oscillating_candles, make_trend_candles
from driftline.persistence import database
from driftline.persistence.models import BacktestRow
from driftline.persistence.repository import get_backtest as fetch_backtest_row
from driftline.persistence.repository import load_candles, save_backtest
from driftline.strategies import registry

settings = get_settings()
configure_logging(settings.log_level)


def get_db() -> Any:
    yield from database.get_session()


def _resolve_candles(
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    limit: int,
    use_fixture: bool,
    fixture_kind: str = "oscillating",
) -> tuple[list[Any], str]:
    stored = load_candles(session, symbol, timeframe, limit=limit)
    if stored:
        return stored, "database"
    if use_fixture:
        if fixture_kind == "trend":
            candles = make_trend_candles(n=min(limit, 2000))
        else:
            candles = make_oscillating_candles(n=min(limit, 2000))
        return candles, "synthetic_fixture"
    return [], "database(empty)"


def create_app() -> FastAPI:
    app = FastAPI(
        title="Driftline API",
        version=__version__,
        description=(
            "Quantitative trading research API: market data, indicators, "
            "baseline strategies, and deterministic backtests. "
            "Simulated only — no live trading."
        ),
    )

    @app.exception_handler(DriftlineError)
    async def _driftline_errors(_: Any, exc: DriftlineError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc), "code": exc.code})

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/market/candles", response_model=list[schemas.CandleOut], tags=["market"])
    def market_candles(
        response: Response,
        symbol: str = Query(default="BTC/USDT", max_length=32),
        timeframe: str = Query(default="1h", max_length=16),
        limit: int = Query(default=200, ge=1, le=1000),
        use_fixture: bool = Query(default=True),
        session: Session = Depends(get_db),
    ) -> list[dict[str, Any]]:
        candles, source = _resolve_candles(
            session, symbol=symbol, timeframe=timeframe, limit=limit, use_fixture=use_fixture
        )
        response.headers["X-Driftline-Data-Source"] = source
        return [c.model_dump() for c in candles[-limit:]]

    @app.get(
        "/api/v1/market/indicators",
        response_model=list[schemas.IndicatorPoint],
        tags=["market"],
    )
    def market_indicators(
        response: Response,
        symbol: str = Query(default="BTC/USDT", max_length=32),
        timeframe: str = Query(default="1h", max_length=16),
        limit: int = Query(default=200, ge=50, le=1000),
        fast_period: int = Query(default=20, ge=2, le=200),
        slow_period: int = Query(default=50, ge=3, le=500),
        use_fixture: bool = Query(default=True),
        session: Session = Depends(get_db),
    ) -> list[dict[str, Any]]:
        if fast_period >= slow_period:
            raise HTTPException(
                status_code=422, detail="fast_period must be smaller than slow_period"
            )
        candles, source = _resolve_candles(
            session, symbol=symbol, timeframe=timeframe, limit=limit, use_fixture=use_fixture
        )
        response.headers["X-Driftline-Data-Source"] = source
        if len(candles) < slow_period + 1:
            raise HTTPException(
                status_code=422,
                detail=f"not enough candles ({len(candles)}) for slow_period={slow_period}",
            )
        ind = compute_all(candles, fast=fast_period, slow=slow_period)
        start = max(0, len(candles) - limit)
        points: list[dict[str, Any]] = []
        for i in range(start, len(candles)):
            c = candles[i]
            points.append(
                {
                    "ts": c.ts,
                    "close": c.close,
                    "fast_ema": ind["fast_ema"][i],
                    "slow_ema": ind["slow_ema"][i],
                    "rsi": ind["rsi"][i],
                    "atr": ind["atr"][i],
                }
            )
        return points

    @app.get("/api/v1/strategies", response_model=list[schemas.StrategyOut], tags=["strategies"])
    def list_strategies() -> list[dict[str, Any]]:
        return registry.list_strategies()

    @app.post("/api/v1/backtests", response_model=schemas.BacktestSummary, tags=["backtests"])
    def create_backtest(
        body: schemas.BacktestRequest, session: Session = Depends(get_db)
    ) -> dict[str, Any]:
        params: dict[str, Any] = dict(body.strategy_params)
        if body.fast_period is not None:
            params["fast_period"] = body.fast_period
        if body.slow_period is not None:
            params["slow_period"] = body.slow_period
        if body.atr_period is not None:
            params["atr_period"] = body.atr_period
        try:
            strategy = registry.create_strategy(body.strategy, params)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        candles, data_source = _resolve_candles(
            session,
            symbol=body.symbol,
            timeframe=body.timeframe,
            limit=body.max_candles,
            use_fixture=body.use_fixture,
        )
        if len(candles) < 10:
            raise HTTPException(
                status_code=422,
                detail="not enough candle data; enable use_fixture or sync market data",
            )
        try:
            result = run_backtest(
                candles,
                strategy.generate(candles),
                BacktestConfig(
                    initial_capital=body.initial_capital,
                    fee_rate=body.fee_rate,
                    slippage_rate=body.slippage_rate,
                    allocation=body.allocation,
                ),
                strategy_name=body.strategy,
                timeframe=body.timeframe,
                data_source=data_source,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        backtest_id = uuid.uuid4().hex[:12]
        created = datetime.now(UTC)
        signals = strategy.generate(candles)
        save_backtest(
            session,
            BacktestRow(
                id=backtest_id,
                created_at=created,
                symbol=body.symbol,
                timeframe=body.timeframe,
                strategy=body.strategy,
                params=params,
                config={
                    "initial_capital": body.initial_capital,
                    "fee_rate": body.fee_rate,
                    "slippage_rate": body.slippage_rate,
                    "allocation": body.allocation,
                },
                metrics=result.metrics.model_dump(mode="json"),
                trades=[t.model_dump(mode="json") for t in result.trades],
                equity=[e.model_dump(mode="json") for e in result.equity_curve],
                data_source=data_source,
            ),
        )
        return {
            "id": backtest_id,
            "symbol": body.symbol,
            "timeframe": body.timeframe,
            "strategy": body.strategy,
            "data_source": data_source,
            "created_at": created,
            "metrics": result.metrics.model_dump(mode="json"),
            "num_trades": len(result.trades),
            "num_signals": len(signals),
            "num_candles": len(candles),
        }

    def _load_or_404(session: Session, backtest_id: str) -> BacktestRow:
        row = fetch_backtest_row(session, backtest_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"backtest {backtest_id!r} not found")
        return row

    @app.get(
        "/api/v1/backtests/{backtest_id}",
        response_model=schemas.BacktestSummary,
        tags=["backtests"],
    )
    def read_backtest(backtest_id: str, session: Session = Depends(get_db)) -> dict[str, Any]:
        row = _load_or_404(session, backtest_id)
        return {
            "id": row.id,
            "symbol": row.symbol,
            "timeframe": row.timeframe,
            "strategy": row.strategy,
            "data_source": row.data_source,
            "created_at": row.created_at,
            "metrics": row.metrics,
            "num_trades": len(row.trades or []),
            "num_signals": 0,
            "num_candles": len(row.equity or []),
        }

    @app.get(
        "/api/v1/backtests/{backtest_id}/trades",
        response_model=list[schemas.TradeOut],
        tags=["backtests"],
    )
    def backtest_trades(backtest_id: str, session: Session = Depends(get_db)) -> list[Any]:
        return _load_or_404(session, backtest_id).trades or []

    @app.get(
        "/api/v1/backtests/{backtest_id}/equity",
        response_model=list[schemas.EquityPointOut],
        tags=["backtests"],
    )
    def backtest_equity(
        backtest_id: str,
        limit: int = Query(default=5000, ge=1, le=5000),
        session: Session = Depends(get_db),
    ) -> list[Any]:
        equity = _load_or_404(session, backtest_id).equity or []
        return equity[-limit:]

    return app


app = create_app()
