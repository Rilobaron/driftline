"""Paper-trading service: decisions, mandatory risk, simulated fills, accounting.

Execution model (mirrors the backtester):
  * A signal forming on completed candle ``S`` (close known) may only fill at
    the *next* completed candle's OPEN — a price unknowable at signal time.
  * If that next open is unavailable the order stays PENDING; it is never
    filled early, never interpolated, never fabricated.
  * Each completed candle is evaluated at most once per session
    (``last_evaluated_ts`` cursor); contiguous data is required to advance.

All funds are simulated. No real-money endpoints are contacted — fills are
computed locally from stored candles.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.orm import Session

from driftline.core.exceptions import ValidationError
from driftline.core.logging import get_logger
from driftline.domain.models import Candle, SignalType
from driftline.market_data.clock import ensure_utc, expected_last_completed, expected_timestamps
from driftline.market_data.streams import validate_symbol, validate_timeframe
from driftline.market_data.validation import timeframe_to_seconds
from driftline.paper import store
from driftline.paper.models import (
    PaperEventKind,
    PaperFillRow,
    PaperOrderRow,
    PaperOrderStatus,
    PaperSessionRow,
    PaperSessionStatus,
    PaperSnapshotRow,
    PaperTradeRow,
)
from driftline.paper.money import D, quantize_cash, quantize_price, quantize_qty
from driftline.paper.risk_policy import PaperRiskConfig, PaperRiskContext, PaperRiskPolicy
from driftline.persistence import repository as repo
from driftline.risk.manager import OrderIntent
from driftline.strategies.registry import default_registry

log = get_logger("paper.service")

MAX_CONSECUTIVE_ERRORS = 10


class CandleLoader(Protocol):
    def __call__(
        self, exchange: str, symbol: str, timeframe: str, *, limit: int
    ) -> list[Candle]: ...


def db_candle_loader(db: Session) -> CandleLoader:
    def _load(exchange: str, symbol: str, timeframe: str, *, limit: int) -> list[Candle]:
        return repo.load_candles(db, symbol, timeframe, limit=limit, exchange=exchange)

    return _load


@dataclass(frozen=True)
class ExecConfig:
    fee_rate: float = 0.001
    slippage_rate: float = 0.0005
    allocation: float = 1.0
    min_quantity: float = 0.00001
    qty_precision: int = 8
    price_precision: int = 8

    def __post_init__(self) -> None:
        if not 0 <= self.fee_rate < 1:
            raise ValueError("fee_rate must be in [0, 1)")
        if not 0 <= self.slippage_rate < 1:
            raise ValueError("slippage_rate must be in [0, 1)")
        if not 0 < self.allocation <= 1:
            raise ValueError("allocation must be in (0, 1]")
        if not self.min_quantity >= 0:
            raise ValueError("min_quantity must be >= 0")
        if not 0 <= self.qty_precision <= 18:
            raise ValueError("qty_precision must be within [0, 18]")
        if not 0 <= self.price_precision <= 18:
            raise ValueError("price_precision must be within [0, 18]")

    def to_dict(self) -> dict[str, object]:
        return {
            "fee_rate": self.fee_rate,
            "slippage_rate": self.slippage_rate,
            "allocation": self.allocation,
            "min_quantity": self.min_quantity,
            "qty_precision": self.qty_precision,
            "price_precision": self.price_precision,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ExecConfig:
        return cls(
            fee_rate=_as_float(data, "fee_rate", 0.001),
            slippage_rate=_as_float(data, "slippage_rate", 0.0005),
            allocation=_as_float(data, "allocation", 1.0),
            min_quantity=_as_float(data, "min_quantity", 0.00001),
            qty_precision=_as_int(data, "qty_precision", 8),
            price_precision=_as_int(data, "price_precision", 8),
        )


@dataclass(frozen=True)
class EvaluationReport:
    session_id: str
    status: str
    evaluated_candles: int = 0
    new_orders: int = 0
    fills: int = 0
    trades_closed: int = 0
    snapshots: int = 0
    risk_rejections: int = 0
    skipped_reason: str | None = None
    error: str | None = None
    equity: Decimal = Decimal("0")


@dataclass(frozen=True)
class PaperServiceConfig:
    history_limit: int = 500
    max_new_candles: int = 48
    exec_defaults: ExecConfig = field(default_factory=ExecConfig)
    risk_defaults: PaperRiskConfig = field(default_factory=PaperRiskConfig)
    error_threshold: int = MAX_CONSECUTIVE_ERRORS


def service_config_from_settings(
    settings: Any, *, history_limit: int = 500, max_new_candles: int = 48
) -> PaperServiceConfig:
    """Build the server-side policy snapshot from deployment configuration.

    Sessions persist these values at creation; public API requests cannot
    override them. Reads attributes only, so tests can pass a namespace.
    """
    return PaperServiceConfig(
        history_limit=history_limit,
        max_new_candles=max_new_candles,
        exec_defaults=ExecConfig(
            fee_rate=float(settings.paper_fee_rate),
            slippage_rate=float(settings.paper_slippage_rate),
            allocation=float(settings.paper_allocation),
            min_quantity=float(settings.paper_min_quantity),
            qty_precision=8,
            price_precision=8,
        ),
        risk_defaults=PaperRiskConfig(
            max_position_allocation=1.0,
            max_risk_per_trade=0.02,
            max_portfolio_drawdown=float(settings.paper_max_drawdown),
            min_available_balance=float(settings.paper_min_balance),
            max_stale_seconds=int(settings.paper_max_stale_seconds),
            max_daily_loss_pct=float(settings.paper_max_daily_loss_pct),
            max_concurrent_positions=1,
        ),
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def order_key(session_id: str, signal_ts: datetime, side: str) -> str:
    return f"{session_id}:{ensure_utc(signal_ts).isoformat()}:{side}"


def _as_float(data: dict[str, object], key: str, default: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"invalid paper exec value for {key!r}: {value!r}")
    return float(value)


def _as_int(data: dict[str, object], key: str, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"invalid paper exec value for {key!r}: {value!r}")
    return int(value)


# -- session lifecycle --------------------------------------------------------


def create_session(
    db: Session,
    *,
    exchange: str = "binance",
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    strategy_id: str = "ema_crossover",
    strategy_params: dict[str, Any] | None = None,
    initial_capital: float = 10_000.0,
    service_config: PaperServiceConfig | None = None,
    now: datetime | None = None,
) -> PaperSessionRow:
    """Create a CREATED session. Risk/execution policy is server-side only."""
    ref = ensure_utc(now) if now is not None else _utcnow()
    cfg = service_config or PaperServiceConfig()
    try:
        clean_symbol = validate_symbol(symbol)
        clean_timeframe = validate_timeframe(timeframe)
    except Exception as exc:
        raise ValidationError(str(exc)) from exc
    if not exchange or len(exchange) > 32:
        raise ValidationError(f"invalid exchange: {exchange!r}")
    try:
        validated_params = default_registry().validate_params(strategy_id, strategy_params)
        version = default_registry().spec(strategy_id).version
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if not initial_capital > 0 or initial_capital > 100_000_000:
        raise ValidationError("initial_capital must be within (0, 100000000]")
    active = store.find_active_for_market(
        db, exchange=exchange, symbol=clean_symbol, timeframe=clean_timeframe
    )
    if active is not None:
        raise ValidationError(
            "an active paper session already exists for "
            f"{exchange}:{clean_symbol}:{clean_timeframe}"
        )
    capital = quantize_cash(D(str(initial_capital)))
    row = PaperSessionRow(
        id=_new_id(),
        created_at=ref,
        updated_at=ref,
        status=PaperSessionStatus.CREATED,
        exchange=exchange,
        symbol=clean_symbol,
        timeframe=clean_timeframe,
        strategy_id=strategy_id,
        strategy_version=version,
        strategy_params=dict(validated_params),
        exec_config=dict(cfg.exec_defaults.to_dict()),
        risk_config=dict(cfg.risk_defaults.to_dict()),
        initial_capital=capital,
        cash=capital,
        reserved_cash=Decimal("0"),
        position_qty=Decimal("0"),
        avg_entry_price=Decimal("0"),
        realized_pnl=Decimal("0"),
        total_fees=Decimal("0"),
        peak_equity=capital,
        open_qty=Decimal("0"),
        open_price=Decimal("0"),
        open_fee=Decimal("0"),
        open_time=None,
        equity_day=ref.date().isoformat(),
        day_start_equity=capital,
        last_evaluated_ts=None,
        last_execution_at=None,
        consecutive_errors=0,
        last_error=None,
        kill_switch=False,
        num_orders=0,
        num_fills=0,
        num_trades=0,
        risk_rejections=0,
    )
    db.add(row)
    store.add_event(
        db,
        row.id,
        ts=ref,
        kind=PaperEventKind.SESSION_CREATED,
        detail=store.event_payload({"strategy": strategy_id, "capital": str(capital)}),
    )
    db.commit()
    db.refresh(row)
    return row


def _transition_with_event(
    db: Session,
    session_id: str,
    target: str,
    kind: str,
    *,
    now: datetime | None = None,
    detail: str = "",
) -> PaperSessionRow:
    ref = ensure_utc(now) if now is not None else _utcnow()
    row = store.lock_session_row(db, session_id)
    if row is None:
        raise ValidationError(f"unknown paper session: {session_id!r}")
    store.transition(db, row, target, now=ref)
    row.updated_at = ref
    if target == PaperSessionStatus.RUNNING:
        row.consecutive_errors = 0
        row.last_error = None
    store.add_event(db, row.id, ts=ref, kind=kind, detail=detail)
    db.commit()
    db.refresh(row)
    return row


def start_session(db: Session, session_id: str, *, now: datetime | None = None) -> PaperSessionRow:
    row = store.get_session_row(db, session_id)
    if row is None:
        raise ValidationError(f"unknown paper session: {session_id!r}")
    if row.status == PaperSessionStatus.CREATED and store.find_active_for_market(
        db, exchange=row.exchange, symbol=row.symbol, timeframe=row.timeframe
    ):
        raise ValidationError("another active session already covers this market")
    return _transition_with_event(
        db, session_id, PaperSessionStatus.RUNNING, PaperEventKind.SESSION_STARTED, now=now
    )


def pause_session(db: Session, session_id: str, *, now: datetime | None = None) -> PaperSessionRow:
    return _transition_with_event(
        db, session_id, PaperSessionStatus.PAUSED, PaperEventKind.SESSION_PAUSED, now=now
    )


def resume_session(db: Session, session_id: str, *, now: datetime | None = None) -> PaperSessionRow:
    return _transition_with_event(
        db, session_id, PaperSessionStatus.RUNNING, PaperEventKind.SESSION_RESUMED, now=now
    )


def stop_session(db: Session, session_id: str, *, now: datetime | None = None) -> PaperSessionRow:
    ref = ensure_utc(now) if now is not None else _utcnow()
    row = store.lock_session_row(db, session_id)
    if row is None:
        raise ValidationError(f"unknown paper session: {session_id!r}")
    store.transition(db, row, PaperSessionStatus.STOPPED, now=ref)
    row.updated_at = ref
    # Pending simulated orders can never fill once stopped: cancel honestly.
    pending = store.pending_orders_for_session(db, row.id)
    for order in pending:
        order.status = PaperOrderStatus.CANCELLED
        order.reason = "session stopped before fill candle completed"
        store.add_event(
            db,
            row.id,
            ts=ref,
            kind=PaperEventKind.ORDER_REJECTED,
            detail=store.event_payload({"order_id": order.id, "reason": order.reason}),
        )
    store.add_event(db, row.id, ts=ref, kind=PaperEventKind.SESSION_STOPPED)
    db.commit()
    db.refresh(row)
    return row


def set_kill_switch(
    db: Session, session_id: str, *, on: bool, now: datetime | None = None
) -> PaperSessionRow:
    """Engage/release the persistent kill switch. Persists across restarts."""
    ref = ensure_utc(now) if now is not None else _utcnow()
    row = store.lock_session_row(db, session_id)
    if row is None:
        raise ValidationError(f"unknown paper session: {session_id!r}")
    row.kill_switch = on
    row.updated_at = ref
    store.add_event(
        db,
        row.id,
        ts=ref,
        kind=PaperEventKind.KILL_SWITCH_ON if on else PaperEventKind.KILL_SWITCH_OFF,
    )
    db.commit()
    db.refresh(row)
    log.warning("paper session %s kill switch %s", session_id, "ON" if on else "OFF")
    return row


# -- evaluation ---------------------------------------------------------------


def _check_invariants(row: PaperSessionRow) -> None:
    if row.cash < Decimal("0"):
        raise AssertionError(f"paper invariant violated: cash negative ({row.cash})")
    if row.position_qty < Decimal("0"):
        raise AssertionError(f"paper invariant violated: position negative ({row.position_qty})")
    if row.reserved_cash != Decimal("0"):
        raise AssertionError("paper invariant violated: reserved cash must be 0 (market fills)")
    if row.position_qty == Decimal("0") and (
        row.avg_entry_price != Decimal("0") or row.open_qty != Decimal("0")
    ):
        raise AssertionError("paper invariant violated: open legs not reset on flat")


def _estimate_buy_qty(
    equity: Decimal,
    ref_price: Decimal,
    exec_cfg: ExecConfig,
) -> Decimal:
    notional = equity * D(str(exec_cfg.allocation))
    ask = ref_price * (Decimal("1") + D(str(exec_cfg.slippage_rate)))
    if ask <= Decimal("0"):
        return Decimal("0")
    gross = notional / (ask * (Decimal("1") + D(str(exec_cfg.fee_rate))))
    return quantize_qty(gross, precision=exec_cfg.qty_precision)


def evaluate_session(
    db: Session,
    session_id: str,
    *,
    now: datetime | None = None,
    loader: CandleLoader | None = None,
    service_config: PaperServiceConfig | None = None,
) -> EvaluationReport:
    """Evaluate one session through all newly completed candles (atomic)."""
    ref = ensure_utc(now) if now is not None else _utcnow()
    cfg = service_config or PaperServiceConfig()
    load: CandleLoader = loader or db_candle_loader(db)
    row = store.lock_session_row(db, session_id)
    if row is None:
        raise ValidationError(f"unknown paper session: {session_id!r}")
    if row.status != PaperSessionStatus.RUNNING:
        return EvaluationReport(session_id=row.id, status=row.status, skipped_reason="not running")

    try:
        return _evaluate_locked(db, row, ref=ref, cfg=cfg, load=load)
    except Exception as exc:
        db.rollback()
        return _record_failure(db, session_id, exc, ref=ref, cfg=cfg)


def _record_failure(
    db: Session,
    session_id: str,
    exc: Exception,
    *,
    ref: datetime,
    cfg: PaperServiceConfig,
) -> EvaluationReport:
    message = str(exc)[:400]
    log.warning("paper session %s evaluation failed: %s", session_id, message)
    row = store.lock_session_row(db, session_id)
    if row is None:  # pragma: no cover - session deleted mid-flight
        raise ValidationError(f"unknown paper session: {session_id!r}")
    row.consecutive_errors = int(row.consecutive_errors or 0) + 1
    row.last_error = message
    row.updated_at = ref
    status = row.status
    if row.consecutive_errors >= cfg.error_threshold and row.status == PaperSessionStatus.RUNNING:
        row.status = PaperSessionStatus.ERROR
        status = row.status
        store.add_event(
            db,
            row.id,
            ts=ref,
            kind=PaperEventKind.SESSION_ERROR,
            detail=store.event_payload({"error": message}),
        )
    else:
        store.add_event(
            db,
            row.id,
            ts=ref,
            kind=PaperEventKind.DATA_SKIP,
            detail=store.event_payload({"error": message}),
        )
    db.commit()
    return EvaluationReport(session_id=session_id, status=status, error=message)


def _evaluate_locked(
    db: Session,
    row: PaperSessionRow,
    *,
    ref: datetime,
    cfg: PaperServiceConfig,
    load: CandleLoader,
) -> EvaluationReport:
    exec_cfg = ExecConfig.from_dict(dict(row.exec_config or {}))
    risk_cfg = PaperRiskConfig.from_dict(dict(row.risk_config or {}))
    policy = PaperRiskPolicy(risk_cfg)
    interval = timeframe_to_seconds(row.timeframe)
    latest_completed = expected_last_completed(ref, row.timeframe)

    candles = load(row.exchange, row.symbol, row.timeframe, limit=cfg.history_limit * 4)
    fresh = [c for c in candles if ensure_utc(c.ts) <= latest_completed]
    if not fresh:
        row.last_execution_at = ref
        db.commit()
        return EvaluationReport(
            session_id=row.id,
            status=row.status,
            skipped_reason="no completed candles",
            equity=D(row.cash) + D(row.position_qty) * D("0"),
        )

    # UTC-day reset before marking anything.
    today = ref.date().isoformat()
    mark = D(str(fresh[-1].close))
    if row.equity_day != today:
        row.equity_day = today
        row.day_start_equity = quantize_cash(D(row.cash) + D(row.position_qty) * mark)

    last_eval = ensure_utc(row.last_evaluated_ts) if row.last_evaluated_ts else None
    new = [c for c in fresh if last_eval is None or ensure_utc(c.ts) > last_eval]
    if not new:
        _fill_pending(db, row, [], exec_cfg, ref)  # heartbeat: nothing new, nothing due
        row.last_execution_at = ref
        db.commit()
        return EvaluationReport(
            session_id=row.id,
            status=row.status,
            skipped_reason="up to date",
            equity=quantize_cash(D(row.cash) + D(row.position_qty) * mark),
        )

    # Staleness gate on the freshest completed candle.
    age = (ref - ensure_utc(fresh[-1].ts)).total_seconds()
    if age > risk_cfg.max_stale_seconds:
        store.add_event(
            db,
            row.id,
            ts=ref,
            kind=PaperEventKind.DATA_SKIP,
            detail=store.event_payload({"reason": "stale market data", "age_s": age}),
        )
        row.last_execution_at = ref
        db.commit()
        return EvaluationReport(
            session_id=row.id,
            status=row.status,
            skipped_reason="stale market data",
            equity=quantize_cash(D(row.cash) + D(row.position_qty) * mark),
        )

    window = new[: max(1, cfg.max_new_candles)]
    # Contiguity gate: first new candle must continue the cursor, and the
    # window itself must have no holes. Gaps are tolerated by waiting, never
    # by fabricating or skipping over them.
    if last_eval is not None:
        expected_next = last_eval + timedelta(seconds=interval)
        if ensure_utc(window[0].ts) != expected_next:
            store.add_event(
                db,
                row.id,
                ts=ref,
                kind=PaperEventKind.DATA_SKIP,
                detail=store.event_payload(
                    {
                        "reason": "unresolved data gap",
                        "expected": expected_next.isoformat(),
                        "found": ensure_utc(window[0].ts).isoformat(),
                    }
                ),
            )
            row.last_execution_at = ref
            db.commit()
            return EvaluationReport(
                session_id=row.id,
                status=row.status,
                skipped_reason="unresolved data gap",
                equity=quantize_cash(D(row.cash) + D(row.position_qty) * mark),
            )
    have = {ensure_utc(c.ts) for c in window}
    for ts in expected_timestamps(window[0].ts, window[-1].ts, row.timeframe):
        if ensure_utc(ts) not in have:
            store.add_event(
                db,
                row.id,
                ts=ref,
                kind=PaperEventKind.DATA_SKIP,
                detail=store.event_payload({"reason": "hole inside evaluation window"}),
            )
            row.last_execution_at = ref
            db.commit()
            return EvaluationReport(
                session_id=row.id,
                status=row.status,
                skipped_reason="hole in window",
                equity=quantize_cash(D(row.cash) + D(row.position_qty) * mark),
            )

    # Warmup history for indicator seeding (never acted upon, only context).
    history = [c for c in fresh if ensure_utc(c.ts) < ensure_utc(window[0].ts)]
    history = history[-cfg.history_limit :]
    strategy = default_registry().create(row.strategy_id, dict(row.strategy_params or {}))
    full_window = history + window
    offset = len(history)
    signals = strategy.generate(full_window)
    by_ts: dict[datetime, list[Any]] = {}
    for sig in signals:
        if sig.index < offset:
            continue  # warmup-only signal: context, never action
        ts = ensure_utc(full_window[sig.index].ts)
        by_ts.setdefault(ts, []).append(sig)

    evaluated = 0
    new_orders = 0
    fills = 0
    closed = 0
    snapshots = 0
    rejects = 0
    for candle in window:
        cts = ensure_utc(candle.ts)
        filled_now = _fill_pending(db, row, [candle], exec_cfg, ref)
        fills += filled_now[0]
        closed += filled_now[1]
        for sig in by_ts.get(cts, []):
            side = "buy" if sig.signal is SignalType.ENTRY else "sell"
            outcome = _decide_signal(db, row, candle, side, exec_cfg, policy, ref)
            new_orders += outcome[0]
            rejects += outcome[1]
        if not store.snapshot_exists(db, row.id, candle.ts):
            equity = quantize_cash(D(row.cash) + D(row.position_qty) * D(str(candle.close)))
            db.add(
                PaperSnapshotRow(
                    session_id=row.id,
                    ts=candle.ts,
                    equity=equity,
                    cash=D(row.cash),
                    position_qty=D(row.position_qty),
                    close=D(str(candle.close)),
                )
            )
            snapshots += 1
            if equity > D(row.peak_equity):
                row.peak_equity = equity
        row.last_evaluated_ts = candle.ts
        evaluated += 1

    # Kill-switch flatten: keep working exits even while entries are blocked.
    if row.kill_switch and D(row.position_qty) > Decimal("0"):
        extra = _ensure_exit_order(db, row, window[-1], exec_cfg, policy, ref, reason="kill switch")
        new_orders += extra[0]
        rejects += extra[1]

    row.last_execution_at = ref
    row.consecutive_errors = 0
    row.last_error = None
    row.updated_at = ref
    _check_invariants(row)
    db.commit()
    final_equity = quantize_cash(D(row.cash) + D(row.position_qty) * D(str(window[-1].close)))
    log.info(
        "paper session %s evaluated %d candles (%d orders, %d fills, %d closed)",
        row.id,
        evaluated,
        new_orders,
        fills,
        closed,
    )
    return EvaluationReport(
        session_id=row.id,
        status=row.status,
        evaluated_candles=evaluated,
        new_orders=new_orders,
        fills=fills,
        trades_closed=closed,
        snapshots=snapshots,
        risk_rejections=rejects,
        equity=final_equity,
    )


def _fill_pending(
    db: Session,
    row: PaperSessionRow,
    candles: list[Candle],
    exec_cfg: ExecConfig,
    ref: datetime,
) -> tuple[int, int]:
    """Fill due pending orders at the given candle's open. Returns (fills, closed)."""
    fills = 0
    closed = 0
    if not candles:
        return (0, 0)
    interval = timeframe_to_seconds(row.timeframe)
    for candle in candles:
        cts = ensure_utc(candle.ts)
        due = [
            o
            for o in store.pending_orders_for_session(db, row.id)
            if ensure_utc(o.signal_ts) + timedelta(seconds=interval) <= cts
        ]
        for order in due:
            if _fill_order(db, row, order, candle, exec_cfg, ref):
                fills += 1
                if order.side == "sell":
                    closed += 1
    return (fills, closed)


def _fill_order(
    db: Session,
    row: PaperSessionRow,
    order: PaperOrderRow,
    candle: Candle,
    exec_cfg: ExecConfig,
    ref: datetime,
) -> bool:
    """Simulate one market fill at the candle open (scaled down if unaffordable)."""
    qty = D(order.qty)
    if qty <= Decimal("0"):
        order.status = PaperOrderStatus.EXPIRED
        order.reason = "non-positive order quantity"
        return False
    raw_open = D(str(candle.open))
    scaled = False
    if order.side == "buy":
        price = quantize_price(
            raw_open * (Decimal("1") + D(str(exec_cfg.slippage_rate))),
            precision=exec_cfg.price_precision,
        )
        cost = qty * price
        fee = quantize_cash(cost * D(str(exec_cfg.fee_rate)))
        scaled = False
        if cost + fee > D(row.cash):
            # Same rule as the backtester: scale down to an affordable size
            # instead of overspending; expire only below the minimum size.
            affordable = quantize_qty(
                D(row.cash) / (price * (Decimal("1") + D(str(exec_cfg.fee_rate)))),
                precision=exec_cfg.qty_precision,
            )
            if affordable <= max(D(str(exec_cfg.min_quantity)), Decimal("0")):
                order.status = PaperOrderStatus.EXPIRED
                order.reason = f"insufficient cash at fill: need {cost + fee}, have {D(row.cash)}"
                store.add_event(
                    db,
                    row.id,
                    ts=ref,
                    kind=PaperEventKind.ORDER_REJECTED,
                    detail=store.event_payload({"order_id": order.id, "reason": order.reason}),
                )
                return False
            qty, cost = affordable, affordable * price
            fee = quantize_cash(cost * D(str(exec_cfg.fee_rate)))
            scaled = True
        new_qty = D(row.position_qty) + qty
        row.avg_entry_price = (
            quantize_cash((D(row.avg_entry_price) * D(row.position_qty) + cost) / new_qty)
            if new_qty > Decimal("0")
            else Decimal("0")
        )
        row.position_qty = new_qty
        row.cash = quantize_cash(D(row.cash) - cost - fee)
        row.total_fees = quantize_cash(D(row.total_fees) + fee)
        row.open_qty = D(row.position_qty)
        row.open_price = price
        row.open_fee = quantize_cash(D(row.open_fee) + fee)
        if row.open_time is None:
            row.open_time = candle.ts
    elif order.side == "sell":
        if qty > D(row.position_qty):
            order.status = PaperOrderStatus.EXPIRED
            order.reason = f"exit qty {qty} exceeds position {D(row.position_qty)}"
            return False
        price = quantize_price(
            raw_open * (Decimal("1") - D(str(exec_cfg.slippage_rate))),
            precision=exec_cfg.price_precision,
        )
        proceeds = qty * price
        fee = quantize_cash(proceeds * D(str(exec_cfg.fee_rate)))
        pnl = (price - D(row.avg_entry_price)) * qty - fee - D(row.open_fee)
        row.realized_pnl = quantize_cash(D(row.realized_pnl) + pnl)
        row.position_qty = D(row.position_qty) - qty
        row.cash = quantize_cash(D(row.cash) + proceeds - fee)
        row.total_fees = quantize_cash(D(row.total_fees) + fee)
        cost_basis = D(row.open_price) * qty + D(row.open_fee)
        db.add(
            PaperTradeRow(
                session_id=row.id,
                entry_time=row.open_time or order.signal_ts,
                exit_time=candle.ts,
                entry_price=D(row.open_price),
                exit_price=price,
                qty=qty,
                entry_fee=D(row.open_fee),
                exit_fee=fee,
                realized_pnl=quantize_cash(pnl),
                return_pct=(
                    quantize_cash(pnl / cost_basis * Decimal("100"))
                    if cost_basis > Decimal("0")
                    else Decimal("0")
                ),
            )
        )
        row.num_trades = int(row.num_trades or 0) + 1
        row.position_qty = Decimal("0")
        row.avg_entry_price = Decimal("0")
        row.open_qty = Decimal("0")
        row.open_price = Decimal("0")
        row.open_fee = Decimal("0")
        row.open_time = None
    else:  # pragma: no cover - guarded at creation
        order.status = PaperOrderStatus.EXPIRED
        order.reason = f"invalid side {order.side!r}"
        return False

    order.status = PaperOrderStatus.FILLED
    fill = PaperFillRow(
        session_id=row.id,
        order_id=order.id,
        ts=candle.ts,
        price=price,
        qty=qty,
        fee=fee,
        slippage_rate=D(str(exec_cfg.slippage_rate)),
        assumptions={
            "model": "next-candle-open",
            "execution": "simulated",
            "signal_ts": ensure_utc(order.signal_ts).isoformat(),
            "fill_ts": ensure_utc(candle.ts).isoformat(),
            "fee_rate": exec_cfg.fee_rate,
            "slippage_rate": exec_cfg.slippage_rate,
            "scaled_to_affordable": scaled,
            "data_source": f"exchange:{row.exchange}",
        },
    )
    db.add(fill)
    db.flush()  # surface duplicate-fill violations immediately
    row.num_fills = int(row.num_fills or 0) + 1
    store.add_event(
        db,
        row.id,
        ts=ref,
        kind=PaperEventKind.FILL,
        detail=store.event_payload(
            {
                "order_id": order.id,
                "side": order.side,
                "price": str(price),
                "qty": str(qty),
                "fee": str(fee),
            }
        ),
    )
    _check_invariants(row)
    return True


def _place_order(
    db: Session,
    row: PaperSessionRow,
    *,
    side: str,
    signal_ts: datetime,
    qty: Decimal,
    ref: datetime,
    reason: str | None = None,
    rejected: bool = False,
) -> PaperOrderRow | None:
    """Idempotent order placement. Returns None when the key already exists."""
    key = order_key(row.id, signal_ts, side)
    if store.order_by_key(db, row.id, key) is not None:
        return None  # duplicate signal delivery: never double-order
    order = PaperOrderRow(
        session_id=row.id,
        idempotency_key=key,
        side=side,
        signal_ts=signal_ts,
        status=PaperOrderStatus.REJECTED if rejected else PaperOrderStatus.PENDING,
        qty=qty,
        created_at=ref,
        decided_at=ref,
        reason=(reason or "")[:512] if reason else None,
    )
    db.add(order)
    db.flush()  # unique constraint enforced now, inside the atomic tick
    row.num_orders = int(row.num_orders or 0) + 1
    return order


def _decide_signal(
    db: Session,
    row: PaperSessionRow,
    candle: Candle,
    side: str,
    exec_cfg: ExecConfig,
    policy: PaperRiskPolicy,
    ref: datetime,
) -> tuple[int, int]:
    """Evaluate one signal: risk gate then pending order (or recorded rejection)."""
    cts = ensure_utc(candle.ts)
    flat = D(row.position_qty) <= Decimal("0")
    if side == "buy" and not flat:
        return (0, 0)  # already long: no pyramid, no duplicate entry
    if side == "sell" and flat:
        return (0, 0)  # nothing to exit
    if side == "buy" and any(o.side == "buy" for o in store.pending_orders_for_session(db, row.id)):
        return (0, 0)  # entry already pending: duplicate signal prevention
    if side == "sell" and any(
        o.side == "sell" for o in store.pending_orders_for_session(db, row.id)
    ):
        return (0, 0)

    if side == "buy":
        equity_mark = D(row.cash) + D(row.position_qty) * D(str(candle.close))
        qty = _estimate_buy_qty(equity_mark, D(str(candle.close)), exec_cfg)
        est_cost = qty * D(str(candle.close)) * (Decimal("1") + D(str(exec_cfg.slippage_rate)))
        est_fee = est_cost * D(str(exec_cfg.fee_rate))
        if qty <= D(str(exec_cfg.min_quantity)) or est_cost + est_fee > D(row.cash):
            return _reject(
                db,
                row,
                side,
                candle.ts,
                ref,
                f"insufficient cash for minimum size (qty {qty}, cash {D(row.cash)})",
            )
        ref_price = D(str(candle.close))
    else:
        qty = D(row.position_qty)
        ref_price = D(str(candle.close))

    decision = policy.check(
        OrderIntent(side=side, quantity=float(qty), price=float(ref_price), timestamp=candle.ts),
        PaperRiskContext(
            cash=D(row.cash),
            position_qty=D(row.position_qty),
            equity=D(row.cash) + D(row.position_qty) * D(str(candle.close)),
            peak_equity=D(row.peak_equity),
            day_start_equity=D(row.day_start_equity),
            kill_switch=bool(row.kill_switch),
            # Simulation clock (like the backtester): the session-level
            # staleness gate owns freshness; per-order checks must behave
            # identically during live ticks and historical catch-up.
            now=candle.ts,
        ),
    )
    if not decision.approved:
        return _reject(db, row, side, candle.ts, ref, "; ".join(decision.reasons))

    store.add_event(
        db,
        row.id,
        ts=ref,
        kind=PaperEventKind.SIGNAL,
        detail=store.event_payload({"side": side, "signal_ts": cts.isoformat()}),
    )
    order = _place_order(db, row, side=side, signal_ts=candle.ts, qty=qty, ref=ref)
    if order is None:
        return (0, 0)
    store.add_event(
        db,
        row.id,
        ts=ref,
        kind=PaperEventKind.ORDER_PLACED,
        detail=store.event_payload({"order_id": order.id, "side": side, "qty": str(qty)}),
    )
    log.info("paper session %s placed %s %s @ signal %s", row.id, side, qty, cts.isoformat())
    return (1, 0)


def _reject(
    db: Session,
    row: PaperSessionRow,
    side: str,
    signal_ts: datetime,
    ref: datetime,
    reason: str,
) -> tuple[int, int]:
    order = _place_order(
        db,
        row,
        side=side,
        signal_ts=signal_ts,
        qty=D(row.position_qty) if side == "sell" else Decimal("0"),
        ref=ref,
        reason=reason,
        rejected=True,
    )
    if order is None:
        return (0, 0)
    row.risk_rejections = int(row.risk_rejections or 0) + 1
    store.add_event(
        db,
        row.id,
        ts=ref,
        kind=PaperEventKind.RISK_REJECT,
        detail=store.event_payload({"side": side, "reason": reason}),
    )
    log.info("paper session %s risk reject %s: %s", row.id, side, reason[:200])
    return (1, 1)


def _ensure_exit_order(
    db: Session,
    row: PaperSessionRow,
    candle: Candle,
    exec_cfg: ExecConfig,
    policy: PaperRiskPolicy,
    ref: datetime,
    *,
    reason: str,
) -> tuple[int, int]:
    if D(row.position_qty) <= Decimal("0"):
        return (0, 0)
    if any(o.side == "sell" for o in store.pending_orders_for_session(db, row.id)):
        return (0, 0)
    decision = policy.check(
        OrderIntent(
            side="sell",
            quantity=float(D(row.position_qty)),
            price=float(D(str(candle.close))),
            timestamp=candle.ts,
        ),
        PaperRiskContext(
            cash=D(row.cash),
            position_qty=D(row.position_qty),
            equity=D(row.cash) + D(row.position_qty) * D(str(candle.close)),
            peak_equity=D(row.peak_equity),
            day_start_equity=D(row.day_start_equity),
            kill_switch=bool(row.kill_switch),
            now=candle.ts,
        ),
    )
    if not decision.approved:
        return _reject(db, row, "sell", candle.ts, ref, f"{reason}: " + "; ".join(decision.reasons))
    order = _place_order(
        db,
        row,
        side="sell",
        signal_ts=candle.ts,
        qty=D(row.position_qty),
        ref=ref,
        reason=reason,
    )
    if order is None:
        return (0, 0)
    store.add_event(
        db,
        row.id,
        ts=ref,
        kind=PaperEventKind.ORDER_PLACED,
        detail=store.event_payload({"order_id": order.id, "side": "sell", "reason": reason}),
    )
    return (1, 0)


def portfolio_summary(
    db: Session, row: PaperSessionRow, *, mark_close: Decimal | None = None
) -> dict[str, object]:
    close = (
        mark_close
        if mark_close is not None
        else (D(row.avg_entry_price) if D(row.position_qty) > Decimal("0") else None)
    )
    equity = D(row.cash) + (D(row.position_qty) * close if close is not None else Decimal("0"))
    unrealized = (
        (close - D(row.avg_entry_price)) * D(row.position_qty)
        if close is not None and D(row.position_qty) > Decimal("0")
        else Decimal("0")
    )
    return {
        "session_id": row.id,
        "status": row.status,
        "initial_capital": str(D(row.initial_capital)),
        "cash": str(D(row.cash)),
        "reserved_cash": str(D(row.reserved_cash)),
        "position_qty": str(D(row.position_qty)),
        "avg_entry_price": str(D(row.avg_entry_price)),
        "realized_pnl": str(D(row.realized_pnl)),
        "unrealized_pnl": str(quantize_cash(unrealized)),
        "total_equity": str(quantize_cash(equity)),
        "total_fees": str(D(row.total_fees)),
        "peak_equity": str(D(row.peak_equity)),
        "num_orders": int(row.num_orders or 0),
        "num_fills": int(row.num_fills or 0),
        "num_trades": int(row.num_trades or 0),
        "risk_rejections": int(row.risk_rejections or 0),
        "kill_switch": bool(row.kill_switch),
        "last_evaluated_ts": (
            ensure_utc(row.last_evaluated_ts).isoformat() if row.last_evaluated_ts else None
        ),
        "last_execution_at": (
            ensure_utc(row.last_execution_at).isoformat() if row.last_execution_at else None
        ),
        "last_error": row.last_error,
    }
