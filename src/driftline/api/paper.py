"""Paper-trading operations API (simulated funds only — never real orders)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from driftline.api import schemas
from driftline.api.deps import get_db, require_ops_token
from driftline.core.exceptions import ValidationError
from driftline.paper import service, store
from driftline.paper.models import PaperSessionRow
from driftline.paper.money import D

router = APIRouter(prefix="/api/v1/paper", tags=["paper"])


def _session_or_404(db: Session, session_id: str) -> PaperSessionRow:
    row = store.get_session_row(db, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"paper session {session_id!r} not found")
    return row


def _session_out(row: PaperSessionRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "status": row.status,
        "exchange": row.exchange,
        "symbol": row.symbol,
        "timeframe": row.timeframe,
        "strategy_id": row.strategy_id,
        "strategy_version": row.strategy_version,
        "strategy_params": dict(row.strategy_params or {}),
        "initial_capital": str(row.initial_capital),
        "kill_switch": bool(row.kill_switch),
        "last_evaluated_ts": row.last_evaluated_ts,
        "last_execution_at": row.last_execution_at,
        "consecutive_errors": int(row.consecutive_errors or 0),
        "last_error": row.last_error,
        "num_orders": int(row.num_orders or 0),
        "num_fills": int(row.num_fills or 0),
        "num_trades": int(row.num_trades or 0),
        "risk_rejections": int(row.risk_rejections or 0),
    }


@router.post("/sessions", response_model=schemas.PaperSessionOut)
def create_paper_session(
    body: schemas.PaperSessionCreate, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    require_ops_token(request)
    try:
        row = service.create_session(
            db,
            exchange=body.exchange,
            symbol=body.symbol,
            timeframe=body.timeframe,
            strategy_id=body.strategy_id,
            strategy_params=dict(body.strategy_params),
            initial_capital=body.initial_capital,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _session_out(row)


@router.get("/sessions", response_model=schemas.PaperSessionListOut)
def list_paper_sessions(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows, total = store.list_sessions(db, limit=limit, offset=offset)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "sessions": [_session_out(r) for r in rows],
    }


@router.get("/sessions/{session_id}", response_model=schemas.PaperSessionOut)
def get_paper_session(session_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _session_out(_session_or_404(db, session_id))


def _mutate(
    db: Session,
    session_id: str,
    action: str,
) -> PaperSessionRow:
    _session_or_404(db, session_id)
    try:
        if action == "start":
            return service.start_session(db, session_id)
        if action == "pause":
            return service.pause_session(db, session_id)
        if action == "resume":
            return service.resume_session(db, session_id)
        if action == "stop":
            return service.stop_session(db, session_id)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=f"unknown action {action!r}")


@router.post("/sessions/{session_id}/start", response_model=schemas.PaperSessionOut)
def start_paper_session(
    session_id: str, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    require_ops_token(request)
    return _session_out(_mutate(db, session_id, "start"))


@router.post("/sessions/{session_id}/pause", response_model=schemas.PaperSessionOut)
def pause_paper_session(
    session_id: str, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    require_ops_token(request)
    return _session_out(_mutate(db, session_id, "pause"))


@router.post("/sessions/{session_id}/resume", response_model=schemas.PaperSessionOut)
def resume_paper_session(
    session_id: str, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    require_ops_token(request)
    return _session_out(_mutate(db, session_id, "resume"))


@router.post("/sessions/{session_id}/stop", response_model=schemas.PaperSessionOut)
def stop_paper_session(
    session_id: str, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    require_ops_token(request)
    return _session_out(_mutate(db, session_id, "stop"))


@router.post("/sessions/{session_id}/kill", response_model=schemas.PaperSessionOut)
def kill_paper_session(
    session_id: str, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Engage the persistent kill switch (entries blocked, exits still work)."""
    require_ops_token(request)
    _session_or_404(db, session_id)
    try:
        row = service.set_kill_switch(db, session_id, on=True)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _session_out(row)


@router.post("/sessions/{session_id}/unkill", response_model=schemas.PaperSessionOut)
def unkill_paper_session(
    session_id: str, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    require_ops_token(request)
    _session_or_404(db, session_id)
    try:
        row = service.set_kill_switch(db, session_id, on=False)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _session_out(row)


@router.get("/sessions/{session_id}/portfolio", response_model=schemas.PaperPortfolioOut)
def paper_portfolio(session_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = _session_or_404(db, session_id)
    snaps = store.list_snapshots(db, session_id, limit=1)
    mark = D(snaps[-1].close) if snaps else None
    return service.portfolio_summary(db, row, mark_close=mark)


@router.get("/sessions/{session_id}/orders", response_model=schemas.PaperOrderListOut)
def paper_orders(
    session_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None, max_length=16),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _session_or_404(db, session_id)
    rows, total = store.list_orders(db, session_id, limit=limit, offset=offset, status=status)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "orders": [
            {
                "id": r.id,
                "session_id": r.session_id,
                "idempotency_key": r.idempotency_key,
                "side": r.side,
                "signal_ts": r.signal_ts,
                "status": r.status,
                "qty": str(r.qty),
                "created_at": r.created_at,
                "decided_at": r.decided_at,
                "reason": r.reason,
            }
            for r in rows
        ],
    }


@router.get("/sessions/{session_id}/trades", response_model=schemas.PaperTradeListOut)
def paper_trades(
    session_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _session_or_404(db, session_id)
    rows, total = store.list_trades(db, session_id, limit=limit, offset=offset)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "trades": [
            {
                "id": r.id,
                "session_id": r.session_id,
                "entry_time": r.entry_time,
                "exit_time": r.exit_time,
                "entry_price": str(r.entry_price),
                "exit_price": str(r.exit_price),
                "qty": str(r.qty),
                "entry_fee": str(r.entry_fee),
                "exit_fee": str(r.exit_fee),
                "realized_pnl": str(r.realized_pnl),
                "return_pct": str(r.return_pct),
            }
            for r in rows
        ],
    }


@router.get("/sessions/{session_id}/equity", response_model=list[schemas.PaperEquityPointOut])
def paper_equity(
    session_id: str,
    limit: int = Query(default=5000, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    _session_or_404(db, session_id)
    return [
        {
            "ts": s.ts,
            "equity": str(s.equity),
            "cash": str(s.cash),
            "position_qty": str(s.position_qty),
            "close": str(s.close),
        }
        for s in store.list_snapshots(db, session_id, limit=limit)
    ]


@router.get("/sessions/{session_id}/events", response_model=schemas.PaperEventListOut)
def paper_events(
    session_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    kind: str | None = Query(default=None, max_length=32),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _session_or_404(db, session_id)
    rows, total = store.list_events(db, session_id, limit=limit, offset=offset, kind=kind)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "events": [
            {"id": e.id, "session_id": e.session_id, "ts": e.ts, "kind": e.kind, "detail": e.detail}
            for e in rows
        ],
    }
