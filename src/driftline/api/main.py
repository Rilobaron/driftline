"""FastAPI application factory and versioned routes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from driftline import __version__
from driftline.api import schemas
from driftline.api.deps import get_db
from driftline.api.deps import require_ops_token as _require_ops_token
from driftline.backtesting.engine import BacktestConfig, run_backtest
from driftline.core.config import get_settings
from driftline.core.exceptions import DriftlineError
from driftline.core.logging import configure_logging
from driftline.indicators import compute_all
from driftline.market_data.fixtures import make_oscillating_candles, make_trend_candles
from driftline.persistence.models import BacktestRow, ExperimentRow
from driftline.persistence.repository import get_backtest as fetch_backtest_row
from driftline.persistence.repository import (
    get_experiment as fetch_experiment_row,
)
from driftline.persistence.repository import (
    list_experiments as list_experiment_rows,
)
from driftline.persistence.repository import load_candles, save_backtest, save_experiment
from driftline.research import (
    build_experiment,
    compare_strategies,
    run_split_test,
    run_walk_forward,
)
from driftline.research.comparison import sort_entries
from driftline.research.serialize import (
    comparison_to_dict,
    split_to_dict,
    walk_forward_to_dict,
)
from driftline.research.walk_forward import OBJECTIVES, WalkForwardConfig, expand_grid
from driftline.risk.manager import RiskConfig
from driftline.strategies import registry
from driftline.strategies.registry import default_registry

settings = get_settings()
configure_logging(settings.log_level)


def _resolve_candles(
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    limit: int,
    use_fixture: bool,
    fixture_kind: str = "oscillating",
    exchange: str = "binance",
) -> tuple[list[Any], str]:
    # Normalize market identity ("btc/usdt"+"1H" hits the same rows as the
    # canonical "BTC/USDT"+"1h"); never split one market into two datasets.
    symbol = symbol.strip().upper()
    timeframe = timeframe.strip().lower()
    exchange = exchange.strip().lower()
    stored = load_candles(session, symbol, timeframe, limit=limit, exchange=exchange)
    if stored:
        return stored, f"exchange:{exchange}"
    if use_fixture:
        if fixture_kind == "trend":
            candles = make_trend_candles(n=min(limit, 2000))
        else:
            candles = make_oscillating_candles(n=min(limit, 2000))
        return candles, "synthetic_fixture"
    return [], "database(empty)"


def _stream_out(row: Any) -> dict[str, Any]:
    gaps = list(row.unresolved_gaps or [])
    return {
        "id": row.id,
        "exchange": row.exchange,
        "symbol": row.symbol,
        "timeframe": row.timeframe,
        "enabled": bool(row.enabled),
        "status": row.status,
        "last_attempt_at": row.last_attempt_at,
        "last_success_at": row.last_success_at,
        "last_completed_ts": row.last_completed_ts,
        "last_error": row.last_error,
        "consecutive_failures": int(row.consecutive_failures or 0),
        "bootstrap_done": bool(row.bootstrap_done),
        "unresolved_gaps": [str(g) for g in gaps],
        "last_duration_ms": int(row.last_duration_ms or 0),
    }


def _report_out(report: Any) -> dict[str, Any]:
    return {
        "exchange": report.exchange,
        "symbol": report.symbol,
        "timeframe": report.timeframe,
        "kind": report.kind,
        "success": report.success,
        "fetched": report.fetched,
        "stored_new": report.stored_new,
        "total_stored": report.total_stored,
        "duplicates": report.duplicates,
        "invalid": report.invalid,
        "missing": report.missing,
        "error": report.error,
        "duration_ms": report.duration_ms,
        "data_source": report.data_source,
    }


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
        exchange: str = Query(default="binance", max_length=32),
        session: Session = Depends(get_db),
    ) -> list[dict[str, Any]]:
        candles, source = _resolve_candles(
            session,
            symbol=symbol,
            timeframe=timeframe,
            limit=limit,
            use_fixture=use_fixture,
            exchange=exchange,
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
        exchange: str = Query(default="binance", max_length=32),
        session: Session = Depends(get_db),
    ) -> list[dict[str, Any]]:
        if fast_period >= slow_period:
            raise HTTPException(
                status_code=422, detail="fast_period must be smaller than slow_period"
            )
        candles, source = _resolve_candles(
            session,
            symbol=symbol,
            timeframe=timeframe,
            limit=limit,
            use_fixture=use_fixture,
            exchange=exchange,
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

    @app.get(
        "/api/v1/strategies/{strategy_id}",
        response_model=schemas.StrategyDetailOut,
        tags=["strategies"],
    )
    def strategy_detail(strategy_id: str) -> dict[str, Any]:
        try:
            spec = default_registry().spec(strategy_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "strategy_id": spec.strategy_id,
            "name": spec.name,
            "description": spec.description,
            "version": spec.version,
            "warmup_bars": spec.warmup_bars,
            "required_indicators": spec.required_indicators,
            "default_params": spec.default_params,
            "parameter_schema": [p.model_dump() for p in spec.parameter_schema],
        }

    def _backtest_config(body: Any) -> BacktestConfig:
        try:
            return BacktestConfig(
                initial_capital=body.initial_capital,
                fee_rate=body.fee_rate,
                slippage_rate=body.slippage_rate,
                allocation=body.allocation,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def _risk_config(body: Any) -> RiskConfig | None:
        if body.risk is None:
            return None
        try:
            return RiskConfig(**body.risk)
        except TypeError as exc:
            raise HTTPException(status_code=422, detail=f"invalid risk config: {exc}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def report_to_risk_dict(risk: RiskConfig | None) -> dict[str, Any] | None:
        if risk is None:
            return None
        return {
            "max_position_allocation": risk.max_position_allocation,
            "max_risk_per_trade": risk.max_risk_per_trade,
            "max_portfolio_drawdown": risk.max_portfolio_drawdown,
            "min_available_balance": risk.min_available_balance,
            "max_stale_seconds": risk.max_stale_seconds,
        }

    def _persist_experiment(session: Session, record: Any) -> None:
        save_experiment(
            session,
            ExperimentRow(
                id=record.experiment_id,
                created_at=record.created_at,
                methodology=record.methodology,
                strategy_id=record.strategy_id,
                record=record.model_dump(mode="json"),
            ),
        )

    @app.post("/api/v1/research/compare", tags=["research"])
    def research_compare(
        body: schemas.CompareRequest, session: Session = Depends(get_db)
    ) -> dict[str, Any]:
        config = _backtest_config(body)
        risk = _risk_config(body)
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
            report = compare_strategies(
                candles,
                [{"strategy_id": c.strategy_id, "params": dict(c.params)} for c in body.strategies],
                config,
                default_registry(),
                symbol=body.symbol,
                timeframe=body.timeframe,
                data_source=data_source,
                risk_config=risk,
            )
            if body.sort_by is not None:
                report.entries = sort_entries(
                    report.entries, metric=body.sort_by, descending=body.sort_descending
                )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        payload = comparison_to_dict(report)
        record = build_experiment(
            methodology="compare",
            strategy_id=",".join(e.strategy_id for e in report.entries),
            symbol=body.symbol,
            timeframe=body.timeframe,
            data_provider=data_source,
            data_start=report.data_start,
            data_end=report.data_end,
            num_candles=report.num_candles,
            fingerprint=report.fingerprint,
            backtest_config=report.backtest_config,
            risk_config=report.risk_config,
            results=payload,
            warnings=report.warnings,
        )
        _persist_experiment(session, record)
        payload["experiment_id"] = record.experiment_id
        return payload

    @app.post("/api/v1/research/split-test", tags=["research"])
    def research_split_test(
        body: schemas.SplitTestRequest, session: Session = Depends(get_db)
    ) -> dict[str, Any]:
        config = _backtest_config(body)
        risk = _risk_config(body)
        candles, data_source = _resolve_candles(
            session,
            symbol=body.symbol,
            timeframe=body.timeframe,
            limit=body.max_candles,
            use_fixture=body.use_fixture,
        )
        if len(candles) < 200:
            raise HTTPException(
                status_code=422,
                detail="not enough candle data for split testing (need >= 200)",
            )
        try:
            report = run_split_test(
                candles,
                body.strategy_id,
                dict(body.strategy_params),
                config,
                default_registry(),
                train_ratio=body.train_ratio,
                symbol=body.symbol,
                timeframe=body.timeframe,
                data_source=data_source,
                risk_config=risk,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        payload = split_to_dict(report)
        assert report.in_sample is not None and report.out_sample is not None
        record = build_experiment(
            methodology="split",
            strategy_id=report.strategy_id,
            strategy_version=report.version,
            strategy_params=report.params,
            evaluation={"train_ratio": report.train_ratio, "split_index": report.split_index},
            symbol=body.symbol,
            timeframe=body.timeframe,
            data_provider=data_source,
            data_start=candles[0].ts,
            data_end=candles[-1].ts,
            num_candles=len(candles),
            fingerprint=report.fingerprint,
            backtest_config={
                "initial_capital": config.initial_capital,
                "fee_rate": config.fee_rate,
                "slippage_rate": config.slippage_rate,
                "allocation": config.allocation,
            },
            risk_config=report_to_risk_dict(risk),
            results=payload,
            warnings=report.warnings,
        )
        _persist_experiment(session, record)
        payload["experiment_id"] = record.experiment_id
        return payload

    @app.post("/api/v1/research/walk-forward", tags=["research"])
    def research_walk_forward(
        body: schemas.WalkForwardRequest, session: Session = Depends(get_db)
    ) -> dict[str, Any]:
        config = _backtest_config(body)
        risk = _risk_config(body)
        if body.objective not in OBJECTIVES:
            raise HTTPException(
                status_code=422,
                detail=f"unknown objective {body.objective!r} (choose from {OBJECTIVES})",
            )
        for key, values in body.param_grid.items():
            if not 1 <= len(values) <= 8:
                raise HTTPException(
                    status_code=422,
                    detail=f"param_grid[{key!r}] must list 1..8 values",
                )
        try:
            wf_config = WalkForwardConfig(
                train_window=body.train_window,
                test_window=body.test_window,
                step=body.step,
                expanding=body.expanding,
                objective=body.objective,
                min_trades=body.min_trades,
            )
            expand_grid({k: list(v) for k, v in body.param_grid.items()})
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        candles, data_source = _resolve_candles(
            session,
            symbol=body.symbol,
            timeframe=body.timeframe,
            limit=body.max_candles,
            use_fixture=body.use_fixture,
        )
        if len(candles) < body.train_window + body.test_window:
            raise HTTPException(
                status_code=422,
                detail="not enough candle data for the requested walk-forward windows",
            )
        try:
            report = run_walk_forward(
                candles,
                body.strategy_id,
                {k: list(v) for k, v in body.param_grid.items()},
                config,
                wf_config,
                default_registry(),
                symbol=body.symbol,
                timeframe=body.timeframe,
                data_source=data_source,
                risk_config=risk,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        payload = walk_forward_to_dict(report)
        record = build_experiment(
            methodology="walk_forward",
            strategy_id=report.strategy_id,
            strategy_version=report.version,
            evaluation={
                "param_grid": {k: list(v) for k, v in body.param_grid.items()},
                "train_window": body.train_window,
                "test_window": body.test_window,
                "step": body.step,
                "expanding": body.expanding,
                "objective": body.objective,
                "min_trades": body.min_trades,
            },
            symbol=body.symbol,
            timeframe=body.timeframe,
            data_provider=data_source,
            data_start=candles[0].ts,
            data_end=candles[-1].ts,
            num_candles=len(candles),
            fingerprint=report.fingerprint,
            backtest_config={
                "initial_capital": config.initial_capital,
                "fee_rate": config.fee_rate,
                "slippage_rate": config.slippage_rate,
                "allocation": config.allocation,
            },
            risk_config=report_to_risk_dict(risk),
            results={
                "aggregate_metrics": payload["aggregate_metrics"],
                "folds": payload["folds"],
                "warnings": payload["warnings"],
            },
            warnings=report.warnings,
        )
        _persist_experiment(session, record)
        payload["experiment_id"] = record.experiment_id
        return payload

    def _load_experiment_or_404(session: Session, experiment_id: str) -> ExperimentRow:
        row = fetch_experiment_row(session, experiment_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"experiment {experiment_id!r} not found")
        return row

    @app.get(
        "/api/v1/research/experiments",
        response_model=list[schemas.ExperimentSummaryOut],
        tags=["research"],
    )
    def list_experiments(
        limit: int = Query(default=50, ge=1, le=200),
        session: Session = Depends(get_db),
    ) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for row in list_experiment_rows(session, limit=limit):
            rec = row.record or {}
            summaries.append(
                {
                    "experiment_id": row.id,
                    "created_at": row.created_at,
                    "methodology": row.methodology,
                    "strategy_id": row.strategy_id,
                    "strategy_version": rec.get("strategy_version", "1.0.0"),
                    "symbol": rec.get("symbol", ""),
                    "timeframe": rec.get("timeframe", "1h"),
                    "fingerprint": rec.get("fingerprint", ""),
                }
            )
        return summaries

    @app.get(
        "/api/v1/research/experiments/{experiment_id}",
        response_model=schemas.ExperimentDetailOut,
        tags=["research"],
    )
    def get_experiment(experiment_id: str, session: Session = Depends(get_db)) -> dict[str, Any]:
        row = _load_experiment_or_404(session, experiment_id)
        rec = dict(row.record or {})
        rec.setdefault("experiment_id", row.id)
        rec.setdefault("created_at", row.created_at)
        rec.setdefault("methodology", row.methodology)
        rec.setdefault("strategy_id", row.strategy_id)
        return rec

    @app.post("/api/v1/backtests", response_model=schemas.BacktestSummary, tags=["backtests"])
    def create_backtest(
        body: schemas.BacktestRequest, session: Session = Depends(get_db)
    ) -> dict[str, Any]:
        params: dict[str, Any] = dict(body.strategy_params)
        if body.strategy == "ema_crossover":
            # Legacy EMA-specific top-level fields (other strategies use strategy_params).
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
            exchange=body.exchange,
        )
        if len(candles) < 10:
            raise HTTPException(
                status_code=422,
                detail="not enough candle data; enable use_fixture or sync market data",
            )
        if body.min_candles is not None and len(candles) < body.min_candles:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"insufficient historical coverage: {len(candles)} {body.exchange}:"
                    f"{body.symbol}:{body.timeframe} candles < min_candles={body.min_candles}"
                ),
            )
        # Data-quality warnings travel with the result (never mutate the snapshot).
        data_warnings: list[str] = []
        try:
            from driftline.market_data.validation import analyze_gaps as _gaps

            gap_report = _gaps(candles, body.timeframe)
            if gap_report.missing:
                data_warnings.append(
                    f"{len(gap_report.missing)} missing {body.timeframe} candles in "
                    f"{body.exchange}:{body.symbol} dataset — metrics bridge gaps; "
                    "treat with caution."
                )
            if data_source == "synthetic_fixture":
                data_warnings.append(
                    "synthetic_fixture dataset: clearly labeled test data, not market performance."
                )
        except Exception:
            pass
        try:
            from driftline.research.fingerprints import dataset_fingerprint as _fp

            fingerprint = _fp(candles, body.symbol, body.timeframe, exchange=body.exchange)
        except Exception:
            fingerprint = ""
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
            "exchange": body.exchange,
            "strategy": body.strategy,
            "data_source": data_source,
            "created_at": created,
            "metrics": result.metrics.model_dump(mode="json"),
            "num_trades": len(result.trades),
            "num_signals": len(signals),
            "num_candles": len(candles),
            "dataset_fingerprint": fingerprint,
            "data_warnings": data_warnings,
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
        source = str(row.data_source or "")
        exchange = source.split(":", 1)[1] if source.startswith("exchange:") else "binance"
        return {
            "id": row.id,
            "symbol": row.symbol,
            "timeframe": row.timeframe,
            "exchange": exchange,
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

    # --- v0.2b market automation (operational visibility) ---

    @app.get("/api/v1/market/streams", response_model=list[schemas.StreamOut], tags=["market"])
    def list_market_streams(session: Session = Depends(get_db)) -> list[dict[str, Any]]:
        from driftline.market_data.streams import parse_market_streams
        from driftline.persistence import repository as _repo

        for preset in parse_market_streams(settings.market_streams):
            _repo.get_or_create_stream(
                session,
                exchange=preset.exchange,
                symbol=preset.symbol,
                timeframe=preset.timeframe,
                enabled=preset.enabled,
            )
        return [_stream_out(r) for r in _repo.list_streams(session)]

    @app.get(
        "/api/v1/market/streams/{stream_id}",
        response_model=schemas.StreamOut,
        tags=["market"],
    )
    def get_market_stream(stream_id: int, session: Session = Depends(get_db)) -> dict[str, Any]:
        from driftline.persistence import repository as _repo

        row = _repo.get_stream(session, stream_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"stream {stream_id} not found")
        return _stream_out(row)

    @app.get("/api/v1/market/health", response_model=schemas.MarketHealthOut, tags=["market"])
    def market_health(session: Session = Depends(get_db)) -> dict[str, Any]:
        from driftline.market_data.quality import compute_quality
        from driftline.market_data.streams import parse_market_streams
        from driftline.persistence import repository as _repo

        for preset in parse_market_streams(settings.market_streams):
            _repo.get_or_create_stream(
                session,
                exchange=preset.exchange,
                symbol=preset.symbol,
                timeframe=preset.timeframe,
                enabled=preset.enabled,
            )
        items: list[dict[str, Any]] = []
        for row in _repo.list_streams(session):
            q = compute_quality(session, row, stale_after_seconds=settings.stale_after_seconds)
            items.append(
                {
                    "exchange": q.exchange,
                    "symbol": q.symbol,
                    "timeframe": q.timeframe,
                    "status": q.status,
                    "last_completed_ts": q.last_completed_ts,
                    "last_success_at": q.last_success_at,
                    "expected_last_completed": q.expected_last_completed,
                    "freshness_seconds": q.freshness_seconds,
                    "total_stored": q.total_stored,
                    "missing_count": q.missing_count,
                    "unresolved_gaps": q.unresolved_gaps,
                    "last_duration_ms": q.last_duration_ms,
                    "last_error": q.last_error,
                    "provider_available": q.provider_available,
                }
            )
        rank = {"ERROR": 4, "STALE": 3, "DEGRADED": 2, "BOOTSTRAPPING": 1, "HEALTHY": 0}
        overall = "HEALTHY"
        worst = -1
        for item in items:
            score = rank.get(str(item["status"]), 2 if item["status"] != "DISABLED" else -1)
            if score > worst:
                worst = score
                overall = str(item["status"])
        if not items:
            overall = "HEALTHY"
        return {"overall": overall, "generated_at": datetime.now(UTC), "streams": items}

    @app.post("/api/v1/market/sync", response_model=schemas.SyncReportOut, tags=["market"])
    def trigger_sync(
        body: schemas.SyncRequest, request: Request, session: Session = Depends(get_db)
    ) -> dict[str, Any]:
        _require_ops_token(request)
        from driftline.market_data.ccxt_provider import CcxtMarketDataProvider
        from driftline.market_data.streams import (
            validate_exchange,
            validate_symbol,
            validate_timeframe,
        )
        from driftline.persistence import repository as _repo

        try:
            exchange = validate_exchange(body.exchange)
            symbol = validate_symbol(body.symbol)
            timeframe = validate_timeframe(body.timeframe)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if body.kind not in ("incremental", "bootstrap"):
            raise HTTPException(status_code=422, detail=f"unknown sync kind {body.kind!r}")
        stream = _repo.get_or_create_stream(
            session, exchange=exchange, symbol=symbol, timeframe=timeframe
        )
        # Unique owner per request: concurrent manual syncs must conflict (409),
        # not silently share a static owner (which would renew instead of deny).
        owner = f"api-manual:{uuid.uuid4().hex[:8]}"
        if not _repo.acquire_stream_lock(session, stream, owner=owner, now=datetime.now(UTC)):
            raise HTTPException(status_code=409, detail="sync already running for this stream")
        try:
            from driftline.market_data.sync import sync_stream

            provider = CcxtMarketDataProvider(
                exchange,
                timeout_seconds=settings.fetch_timeout_seconds,
                max_retries=settings.fetch_max_retries,
            )
            report = sync_stream(
                session,
                exchange=exchange,
                symbol=symbol,
                timeframe=timeframe,
                provider=provider,
                kind=body.kind,
                lookback_months=settings.bootstrap_lookback_months,
                total_limit=body.total_limit,
                bootstrap_target=max(1, settings.bootstrap_lookback_months * 744),
            )
        finally:
            fresh = _repo.get_stream(session, stream.id)
            if fresh is not None:
                _repo.release_stream_lock(session, fresh, owner=owner)
        return _report_out(report)

    @app.post("/api/v1/market/repair", response_model=schemas.SyncReportOut, tags=["market"])
    def trigger_repair(
        body: schemas.RepairRequest, request: Request, session: Session = Depends(get_db)
    ) -> dict[str, Any]:
        _require_ops_token(request)
        from driftline.market_data.ccxt_provider import CcxtMarketDataProvider
        from driftline.market_data.streams import (
            validate_exchange,
            validate_symbol,
            validate_timeframe,
        )
        from driftline.persistence import repository as _repo

        try:
            exchange = validate_exchange(body.exchange)
            symbol = validate_symbol(body.symbol)
            timeframe = validate_timeframe(body.timeframe)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        stream = _repo.get_or_create_stream(
            session, exchange=exchange, symbol=symbol, timeframe=timeframe
        )
        owner = f"api-manual:{uuid.uuid4().hex[:8]}"
        if not _repo.acquire_stream_lock(session, stream, owner=owner, now=datetime.now(UTC)):
            raise HTTPException(status_code=409, detail="repair already running for this stream")
        try:
            from driftline.market_data.sync import repair_stream_gaps

            provider = CcxtMarketDataProvider(
                exchange,
                timeout_seconds=settings.fetch_timeout_seconds,
                max_retries=settings.fetch_max_retries,
            )
            report = repair_stream_gaps(
                session,
                exchange=exchange,
                symbol=symbol,
                timeframe=timeframe,
                provider=provider,
                max_intervals=body.max_intervals,
            )
        finally:
            fresh = _repo.get_stream(session, stream.id)
            if fresh is not None:
                _repo.release_stream_lock(session, fresh, owner=owner)
        return _report_out(report)

    @app.get(
        "/api/v1/market/sync-history",
        response_model=schemas.SyncHistoryOut,
        tags=["market"],
    )
    def sync_history(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        exchange: str | None = Query(default=None, max_length=32),
        symbol: str | None = Query(default=None, max_length=32),
        timeframe: str | None = Query(default=None, max_length=16),
        session: Session = Depends(get_db),
    ) -> dict[str, Any]:
        from driftline.persistence import repository as _repo

        rows, total = _repo.list_sync_runs(
            session,
            limit=limit,
            offset=offset,
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
        )
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "runs": [
                {
                    "id": r.id,
                    "exchange": r.exchange,
                    "symbol": r.symbol,
                    "timeframe": r.timeframe,
                    "kind": r.kind,
                    "started_at": r.started_at,
                    "finished_at": r.finished_at,
                    "success": bool(r.success),
                    "fetched": r.fetched,
                    "stored_new": r.stored_new,
                    "duplicates": r.duplicates,
                    "invalid": r.invalid,
                    "missing": r.missing,
                    "error": r.error,
                    "duration_ms": r.duration_ms,
                    "data_source": r.data_source,
                }
                for r in rows
            ],
        }

    from driftline.api.paper import router as paper_router

    app.include_router(paper_router)

    return app


app = create_app()
