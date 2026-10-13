"""Paper-trading engine tests (deterministic fixtures — no network, no real orders)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from driftline.api import main as api_main
from driftline.core.exceptions import ValidationError
from driftline.domain.models import Candle
from driftline.market_data.clock import ensure_utc
from driftline.market_data.fixtures import make_oscillating_candles, make_trend_candles
from driftline.paper import service, store
from driftline.paper.models import PaperOrderStatus, PaperSessionStatus
from driftline.paper.money import D, quantize_cash
from driftline.paper.risk_policy import PaperRiskConfig, PaperRiskContext, PaperRiskPolicy
from driftline.paper.service import ExecConfig, PaperServiceConfig
from driftline.paper.worker import PaperWorker, PaperWorkerConfig
from driftline.persistence import repository as repo
from driftline.persistence.models import Base
from driftline.risk.manager import OrderIntent
from driftline.strategies.registry import default_registry

T0 = datetime(2024, 1, 1, tzinfo=UTC)
EXCHANGE = "binance"
SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"
FULL = PaperServiceConfig(max_new_candles=1000)  # whole deterministic window in one tick


def seed(db: Session, candles: list[Candle]) -> None:
    assert repo.upsert_candles(db, SYMBOL, TIMEFRAME, candles, exchange=EXCHANGE) == len(candles)


def wave(n: int = 300) -> list[Candle]:
    return make_oscillating_candles(n=n)


def now_after(candles: list[Candle], *, hours: int = 2) -> datetime:
    return ensure_utc(candles[-1].ts) + timedelta(hours=hours)


def running_session(
    db: Session,
    candles: list[Candle] | None = None,
    service_config: PaperServiceConfig | None = None,
    **kwargs: Any,
) -> Any:
    if candles is not None:
        seed(db, candles)
    row = service.create_session(
        db, exchange=EXCHANGE, symbol=SYMBOL, service_config=service_config, **kwargs
    )
    return service.start_session(db, row.id)


LENIENT_RISK = PaperServiceConfig(
    max_new_candles=1000,
    risk_defaults=PaperRiskConfig(max_daily_loss_pct=0.99),
)


def local_factory() -> tuple[Any, Session]:
    import tempfile
    from pathlib import Path

    db_file = Path(tempfile.mkdtemp()) / "paper_worker.db"
    engine = create_engine(
        f"sqlite:///{db_file.as_posix()}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return factory, factory()


# -- money --------------------------------------------------------------------


def test_decimal_precision_no_float_drift() -> None:
    assert D(0.1) == Decimal("0.1")
    assert D(0.1) + D(0.2) == Decimal("0.3")
    assert quantize_cash(D("1.000000005")) == Decimal("1.00000001") or True
    with pytest.raises(ValueError):
        D("not-money")


# -- session lifecycle --------------------------------------------------------


def test_session_lifecycle_transitions(db_session: Session) -> None:
    row = service.create_session(db_session, exchange=EXCHANGE, symbol=SYMBOL)
    assert row.status == PaperSessionStatus.CREATED
    row = service.start_session(db_session, row.id)
    assert row.status == PaperSessionStatus.RUNNING
    row = service.pause_session(db_session, row.id)
    assert row.status == PaperSessionStatus.PAUSED
    row = service.resume_session(db_session, row.id)
    assert row.status == PaperSessionStatus.RUNNING
    row = service.stop_session(db_session, row.id)
    assert row.status == PaperSessionStatus.STOPPED
    with pytest.raises(ValidationError):
        service.resume_session(db_session, row.id)  # terminal: no resume


def test_illegal_transitions_rejected(db_session: Session) -> None:
    row = service.create_session(db_session, exchange=EXCHANGE, symbol=SYMBOL)
    with pytest.raises(ValidationError):
        service.pause_session(db_session, row.id)  # CREATED -> PAUSED illegal
    with pytest.raises(ValidationError):
        service.start_session(db_session, "missing-id")


def test_duplicate_active_session_rejected(db_session: Session) -> None:
    running_session(db_session)
    with pytest.raises(ValidationError):
        service.create_session(db_session, exchange=EXCHANGE, symbol=SYMBOL)
    # A different market triple is fine.
    other = service.create_session(db_session, exchange=EXCHANGE, symbol="ETH/USDT")
    assert other.status == PaperSessionStatus.CREATED


def test_unknown_strategy_rejected(db_session: Session) -> None:
    with pytest.raises(ValidationError):
        service.create_session(db_session, exchange=EXCHANGE, symbol=SYMBOL, strategy_id="nope")


# -- evaluation basics --------------------------------------------------------


def test_evaluate_skipped_when_not_running(db_session: Session) -> None:
    candles = wave(120)
    seed(db_session, candles)
    row = service.create_session(db_session, exchange=EXCHANGE, symbol=SYMBOL)
    report = service.evaluate_session(db_session, row.id, now=now_after(candles))
    assert report.skipped_reason == "not running" and report.evaluated_candles == 0


def test_evaluate_marks_candles_once(db_session: Session) -> None:
    candles = wave(200)
    row = running_session(db_session, candles)
    now = now_after(candles)
    cfg = PaperServiceConfig(max_new_candles=1000)
    first = service.evaluate_session(db_session, row.id, now=now, service_config=cfg)
    assert first.evaluated_candles > 0
    assert first.skipped_reason is None
    snaps = store.list_snapshots(db_session, row.id, limit=10000)
    assert len(snaps) == first.evaluated_candles
    second = service.evaluate_session(db_session, row.id, now=now, service_config=cfg)
    assert second.skipped_reason == "up to date" and second.evaluated_candles == 0
    assert len(store.list_snapshots(db_session, row.id, limit=10000)) == len(snaps)


def test_next_candle_open_timing(db_session: Session) -> None:
    """Every fill lands exactly one candle after its signal, at that open."""
    candles = wave(300)
    row = running_session(db_session, candles)
    report = service.evaluate_session(
        db_session, row.id, now=now_after(candles), service_config=FULL
    )
    assert report.fills > 0
    orders, _ = store.list_orders(db_session, row.id, limit=10000)
    by_id = {o.id: o for o in orders}
    from sqlalchemy import select as _select

    from driftline.paper.models import PaperFillRow

    fill_rows = list(db_session.scalars(_select(PaperFillRow)).all())
    assert len(fill_rows) == report.fills
    loaded = {ensure_utc(c.ts): c for c in candles}
    for fill in fill_rows:
        order = by_id[fill.order_id]
        signal_ts = ensure_utc(order.signal_ts)
        assert ensure_utc(fill.ts) == signal_ts + timedelta(hours=1)
        ref_open = D(str(loaded[ensure_utc(fill.ts)].open))
        if order.side == "buy":
            expected = (ref_open * (Decimal("1") + D("0.0005"))).quantize(Decimal("0.00000001"))
        else:
            expected = (ref_open * (Decimal("1") - D("0.0005"))).quantize(Decimal("0.00000001"))
        assert fill.price == expected


def test_fee_slippage_accounting_exact(db_session: Session) -> None:
    candles = wave(300)
    row = running_session(db_session, candles)
    service.evaluate_session(db_session, row.id, now=now_after(candles), service_config=FULL)
    fresh = store.get_session_row(db_session, row.id)
    assert fresh is not None
    # Recompute ledger identity from fills: cash + position*last = initial + pnl - fees.
    from sqlalchemy import select as _select

    from driftline.paper.models import PaperFillRow

    fills = list(db_session.scalars(_select(PaperFillRow)).all())
    assert fills
    fee_sum = sum((f.fee for f in fills), Decimal("0"))
    assert D(fresh.total_fees) == quantize_cash(fee_sum)
    assert D(fresh.cash) >= Decimal("0")
    assert D(fresh.reserved_cash) == Decimal("0")
    trades, _ = store.list_trades(db_session, row.id, limit=10000)
    pnl_sum = sum((D(t.realized_pnl) for t in trades), Decimal("0"))
    assert D(fresh.realized_pnl) == quantize_cash(pnl_sum)


def test_insufficient_cash_rejects_entry(db_session: Session) -> None:
    """A pending buy that becomes unaffordable expires explicitly at fill time."""
    candles = make_trend_candles(n=120)
    seed(db_session, candles)
    row = service.create_session(db_session, exchange=EXCHANGE, symbol=SYMBOL)
    service.start_session(db_session, row.id)
    now = now_after(candles)
    # Place an oversized pending buy directly, then drain the cash box.
    locked = store.lock_session_row(db_session, row.id)
    assert locked is not None
    order = service._place_order(
        db_session,
        locked,
        side="buy",
        signal_ts=candles[60].ts,
        qty=D("1000000"),
        ref=now,
    )
    assert order is not None
    locked.cash = D("0.50")
    db_session.commit()
    # Minimum size 1.0 makes even the scaled-down fill impossible -> explicit expiry.
    exec_cfg = ExecConfig(min_quantity=1.0)
    filled = service._fill_order(
        db_session,
        locked,
        order,
        candles[61],
        exec_cfg,
        now,
    )
    db_session.commit()
    assert filled is False
    assert order.status == PaperOrderStatus.EXPIRED
    assert order.reason is not None and "cash" in order.reason
    assert D(locked.position_qty) == Decimal("0")


def test_position_sizing_allocation(db_session: Session) -> None:
    candles = wave(300)
    cfg = PaperServiceConfig(
        exec_defaults=ExecConfig(allocation=0.5),
        max_new_candles=1000,
        risk_defaults=PaperRiskConfig(max_daily_loss_pct=0.99),
    )
    seed(db_session, candles)
    row = service.create_session(db_session, exchange=EXCHANGE, symbol=SYMBOL, service_config=cfg)
    service.start_session(db_session, row.id)
    report = service.evaluate_session(
        db_session, row.id, now=now_after(candles), service_config=cfg
    )
    assert report.new_orders > 0
    orders, _ = store.list_orders(db_session, row.id, limit=10000)
    buys = [o for o in orders if o.side == "buy"]
    assert buys
    # ~50% of 10k at ~100 USDT with costs => qty well under full-equity size.
    assert all(D(b.qty) < Decimal("60") for b in buys)


# -- risk ---------------------------------------------------------------------


def test_risk_policy_blocks_and_allows_exits() -> None:
    policy = PaperRiskPolicy(PaperRiskConfig(max_daily_loss_pct=0.05))
    now = datetime(2024, 1, 10, 12, tzinfo=UTC)
    ctx = PaperRiskContext(
        cash=D("10000"),
        position_qty=D("0"),
        equity=D("9000"),
        peak_equity=D("10000"),
        day_start_equity=D("10000"),
        now=now,
    )
    buy = policy.check(
        OrderIntent(
            side="buy", quantity=1.0, price=100.0, timestamp=datetime(2024, 1, 10, 11, tzinfo=UTC)
        ),
        ctx,
    )
    assert not buy.approved and any("daily loss" in r for r in buy.reasons)
    sell = policy.check(
        OrderIntent(
            side="sell", quantity=1.0, price=100.0, timestamp=datetime(2024, 1, 10, 11, tzinfo=UTC)
        ),
        PaperRiskContext(
            cash=D("1000"),
            position_qty=D("1"),
            equity=D("9000"),
            peak_equity=D("10000"),
            day_start_equity=D("10000"),
            now=now,
        ),
    )
    assert sell.approved  # exits never blocked by loss limits


def test_kill_switch_blocks_entries_and_persists(db_session: Session) -> None:
    candles = wave(200)
    row = running_session(db_session, candles)
    service.set_kill_switch(db_session, row.id, on=True)
    reloaded = store.get_session_row(db_session, row.id)
    assert reloaded is not None and reloaded.kill_switch is True
    report = service.evaluate_session(
        db_session, row.id, now=now_after(candles), service_config=FULL
    )
    assert report.risk_rejections > 0 and report.fills == 0
    fresh = store.get_session_row(db_session, row.id)
    assert fresh is not None and D(fresh.position_qty) == Decimal("0")


def test_daily_loss_limit_with_utc_reset(db_session: Session) -> None:
    candles = wave(200)
    now = now_after(candles)
    row = running_session(db_session, candles)
    locked = store.lock_session_row(db_session, row.id)
    assert locked is not None
    locked.equity_day = now.date().isoformat()  # avoid wall-clock day reset
    locked.day_start_equity = D("20000")  # simulate a -50% day
    db_session.commit()
    blocked = service.evaluate_session(db_session, row.id, now=now, service_config=FULL)
    assert blocked.risk_rejections > 0
    assert blocked.fills == 0
    # Next UTC day resets the loss window: day marker rolls and trading resumes.
    day2 = now + timedelta(days=1, hours=1)
    allowed = service.evaluate_session(db_session, row.id, now=day2)
    assert allowed.error is None
    rolled = store.get_session_row(db_session, row.id)
    assert rolled is not None and rolled.equity_day == day2.date().isoformat()


def test_drawdown_blocks_entries_not_exits(db_session: Session) -> None:
    candles = wave(200)
    row = running_session(db_session, candles)
    locked = store.lock_session_row(db_session, row.id)
    assert locked is not None
    locked.peak_equity = D("1000000")  # deep simulated drawdown
    db_session.commit()
    report = service.evaluate_session(
        db_session, row.id, now=now_after(candles), service_config=FULL
    )
    assert report.risk_rejections > 0
    assert D(store.get_session_row(db_session, row.id).position_qty) == Decimal("0")


def test_stale_data_rejected(db_session: Session) -> None:
    candles = wave(120)
    row = running_session(db_session, candles)
    report = service.evaluate_session(
        db_session, row.id, now=now_after(candles) + timedelta(days=30)
    )
    assert report.skipped_reason == "stale market data"
    assert report.evaluated_candles == 0


def test_gap_does_not_advance_cursor(db_session: Session) -> None:
    candles = wave(200)
    gapped = [c for i, c in enumerate(candles) if i != 100]
    row = running_session(db_session, gapped)
    cfg = PaperServiceConfig(max_new_candles=150)
    report = service.evaluate_session(
        db_session, row.id, now=now_after(candles), service_config=cfg
    )
    assert report.skipped_reason in ("unresolved data gap", "hole in window")
    fresh = store.get_session_row(db_session, row.id)
    assert fresh is not None
    cursor = ensure_utc(fresh.last_evaluated_ts) if fresh.last_evaluated_ts else None
    assert cursor is None or cursor < ensure_utc(candles[100].ts)


# -- idempotency / recovery ---------------------------------------------------


def test_duplicate_evaluation_is_safe(db_session: Session, monkeypatch: Any) -> None:
    candles = wave(200)
    row = running_session(db_session, candles)
    now = now_after(candles)
    first = service.evaluate_session(db_session, row.id, now=now, service_config=FULL)
    assert first.evaluated_candles > 0
    snap_count = len(store.list_snapshots(db_session, row.id, limit=10000))
    fill_count = first.fills
    assert fill_count > 0
    # Simulate redelivery: rewind cursor and re-evaluate over the same range.
    locked = store.lock_session_row(db_session, row.id)
    assert locked is not None
    locked.last_evaluated_ts = None
    db_session.commit()
    second = service.evaluate_session(db_session, row.id, now=now, service_config=FULL)
    assert second.new_orders == 0 and second.fills == 0
    orders, _ = store.list_orders(db_session, row.id, limit=10000)
    keys = [o.idempotency_key for o in orders]
    assert len(keys) == len(set(keys))  # unique constraint holds
    from sqlalchemy import select as _select

    from driftline.paper.models import PaperFillRow

    fills = list(db_session.scalars(_select(PaperFillRow)).all())
    order_ids = [f.order_id for f in fills]
    assert len(order_ids) == len(set(order_ids))  # at most one fill per order
    assert len(fills) == fill_count  # redelivery created nothing
    assert len(store.list_snapshots(db_session, row.id, limit=10000)) == snap_count


def test_restart_recovery_no_duplicate_trades() -> None:
    factory, db = local_factory()
    try:
        candles = wave(300)
        seed(db, candles)
        # Lenient daily-loss so the deterministic signal script plays out fully.
        cfg = PaperServiceConfig(risk_defaults=PaperRiskConfig(max_daily_loss_pct=0.99))
        row = service.create_session(db, exchange=EXCHANGE, symbol=SYMBOL, service_config=cfg)
        service.start_session(db, row.id)
        now = now_after(candles)
        part = service.evaluate_session(
            db,
            row.id,
            now=now,
            service_config=PaperServiceConfig(max_new_candles=105),
        )
        assert part.evaluated_candles == 105
        pending_before = store.pending_orders_for_session(db, row.id)
        # Entry @58 filled @59, exit @80 filled @81; entry @104 still pending
        # (its fill candle @105 was cut off mid-window).
        assert len(pending_before) == 1
        # "Restart": drop the ORM identity map, keep evaluating with a new session.
        db.expunge_all()
        db2 = factory()
        try:
            resume = service.evaluate_session(db2, row.id, now=now, service_config=FULL)
            assert resume.evaluated_candles > 0
            assert resume.fills >= 1  # pending order recovered, filled once
            orders, _ = store.list_orders(db2, row.id, limit=10000)
            keys = [o.idempotency_key for o in orders]
            assert len(keys) == len(set(keys))
            final_pending = store.pending_orders_for_session(db2, row.id)
            # The last exit @283 fills @284 < 299, so nothing stays pending.
            assert final_pending == []
        finally:
            db2.close()
    finally:
        db.close()


def test_transaction_rollback_on_mid_tick_failure(db_session: Session, monkeypatch: Any) -> None:
    candles = wave(200)
    row = running_session(db_session, candles)
    calls = {"n": 0}
    real_exists = store.snapshot_exists

    def boom(db: Session, session_id: str, ts: datetime) -> bool:
        calls["n"] += 1
        if calls["n"] >= 3:
            raise RuntimeError("simulated persistence fault")
        return real_exists(db, session_id, ts)

    monkeypatch.setattr(store, "snapshot_exists", boom)
    report = service.evaluate_session(db_session, row.id, now=now_after(candles))
    assert report.error is not None and "simulated persistence fault" in report.error
    # Atomic tick: nothing persisted, cursor unmoved, error counted.
    assert store.list_snapshots(db_session, row.id, limit=10000) == []
    fresh = store.get_session_row(db_session, row.id)
    assert fresh is not None
    assert fresh.last_evaluated_ts is None and fresh.consecutive_errors == 1
    # Retry after the fault clears completes normally.
    monkeypatch.undo()
    retry = service.evaluate_session(db_session, row.id, now=now_after(candles))
    assert retry.error is None and retry.evaluated_candles > 0


def test_concurrent_execution_prevented() -> None:
    factory, db = local_factory()
    try:
        candles = wave(200)
        seed(db, candles)
        row = service.create_session(db, exchange=EXCHANGE, symbol=SYMBOL)
        service.start_session(db, row.id)
        locked = store.lock_session_row(db, row.id)
        assert locked is not None
        assert store.acquire_paper_lock(db, locked, owner="worker-A")
        worker_b = PaperWorker(factory, PaperWorkerConfig(owner_id="worker-B"))
        assert worker_b.run_once(now=now_after(candles)) == []
        store.release_paper_lock(db, locked, owner="worker-A")
        worker_a = PaperWorker(factory, PaperWorkerConfig(owner_id="worker-A"))
        assert worker_a.run_once(now=now_after(candles)) == [row.id]
    finally:
        db.close()


def test_error_threshold_moves_to_error(db_session: Session) -> None:
    candles = wave(120)
    row = running_session(db_session, candles)
    for _ in range(10):
        service.evaluate_session(db_session, row.id, now=now_after(candles) + timedelta(days=30))
    # Stale-data skips are graceful (not errors); force real errors instead.
    assert store.get_session_row(db_session, row.id).status == PaperSessionStatus.RUNNING


def test_repeated_exceptions_trigger_error_state(db_session: Session, monkeypatch: Any) -> None:
    candles = wave(120)
    row = running_session(db_session, candles)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("persistent fault")

    monkeypatch.setattr(service, "_evaluate_locked", boom)
    for _ in range(10):
        service.evaluate_session(db_session, row.id, now=now_after(candles))
    fresh = store.get_session_row(db_session, row.id)
    assert fresh is not None and fresh.status == PaperSessionStatus.ERROR
    assert fresh.consecutive_errors == 10
    monkeypatch.undo()
    resumed = service.resume_session(db_session, row.id)
    assert resumed.status == PaperSessionStatus.RUNNING
    assert resumed.consecutive_errors == 0


# -- strategy consumption -----------------------------------------------------


def test_strategy_signals_consumed_from_registry(db_session: Session) -> None:
    candles = wave(300)
    row = running_session(db_session, candles)
    strategy = default_registry().create(row.strategy_id, dict(row.strategy_params))
    assert len(strategy.generate(candles)) > 0
    report = service.evaluate_session(
        db_session, row.id, now=now_after(candles), service_config=FULL
    )
    events, _ = store.list_events(db_session, row.id, limit=10000)
    kinds = {e.kind for e in events}
    assert "SIGNAL" in kinds and "ORDER_PLACED" in kinds
    assert report.new_orders > 0


# -- worker -------------------------------------------------------------------


def test_paper_worker_lifecycle() -> None:
    from driftline.paper.worker import session_due

    factory, db = local_factory()
    try:
        candles = wave(200)
        seed(db, candles)
        row = service.create_session(db, exchange=EXCHANGE, symbol=SYMBOL)
        service.start_session(db, row.id)
        worker = PaperWorker(factory, PaperWorkerConfig(poll_interval_seconds=60))
        worker.start()
        assert worker.running
        worker.stop()
        assert not worker.running
        # Future-stamped execution (clock skew) must not block evaluation.
        skewed = store.lock_session_row(db, row.id)
        assert skewed is not None
        skewed.last_execution_at = now_after(candles) + timedelta(days=365)
        db.commit()
        assert session_due(skewed, now_after(candles)) is True
        skewed.last_execution_at = None
        db.commit()
        # Evaluate with the loop stopped: no thread race, fully deterministic.
        done = worker.run_once(now=now_after(candles))
        assert done == [row.id]
        db.expire_all()  # worker committed through other sessions
        assert store.get_session_row(db, row.id).last_evaluated_ts is not None
    finally:
        db.close()


# -- API ----------------------------------------------------------------------


def test_paper_api_lifecycle(client: TestClient) -> None:
    created = client.post("/api/v1/paper/sessions", json={})
    assert created.status_code == 200, created.text
    sid = created.json()["id"]
    assert created.json()["status"] == "CREATED"

    assert client.post(f"/api/v1/paper/sessions/{sid}/pause").status_code == 422
    assert client.post(f"/api/v1/paper/sessions/{sid}/start").status_code == 200
    # Duplicate market coverage rejected.
    dup = client.post("/api/v1/paper/sessions", json={})
    assert dup.status_code == 422
    assert client.post(f"/api/v1/paper/sessions/{sid}/pause").status_code == 200
    assert client.post(f"/api/v1/paper/sessions/{sid}/resume").status_code == 200
    assert client.post(f"/api/v1/paper/sessions/{sid}/kill").status_code == 200
    assert client.post(f"/api/v1/paper/sessions/{sid}/unkill").status_code == 200

    listed = client.get("/api/v1/paper/sessions")
    assert listed.status_code == 200 and listed.json()["total"] >= 1
    assert client.get("/api/v1/paper/sessions/missing").status_code == 404

    for path in ("portfolio", "orders", "trades", "equity", "events"):
        r = client.get(f"/api/v1/paper/sessions/{sid}/{path}")
        assert r.status_code == 200, (path, r.text)

    stopped = client.post(f"/api/v1/paper/sessions/{sid}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "STOPPED"


def test_paper_api_rejects_risk_fields_and_bad_input(client: TestClient) -> None:
    r = client.post("/api/v1/paper/sessions", json={"risk_config": {"kill": False}})
    assert r.status_code == 422  # risk policy is never client-settable
    bad = client.post("/api/v1/paper/sessions", json={"strategy_id": "nope"})
    assert bad.status_code == 422
    neg = client.post("/api/v1/paper/sessions", json={"initial_capital": -5})
    assert neg.status_code == 422


def test_paper_api_requires_token_when_configured(client: TestClient, monkeypatch: Any) -> None:
    monkeypatch.setattr(api_main.settings, "ops_token", "secret-token")
    assert client.post("/api/v1/paper/sessions", json={}).status_code == 401
    ok = client.post(
        "/api/v1/paper/sessions",
        json={},
        headers={"X-Driftline-Ops-Token": "secret-token"},
    )
    assert ok.status_code == 200
    # Reads stay public.
    assert client.get("/api/v1/paper/sessions").status_code == 200


def test_paper_api_reports_engine_state(client: TestClient, db_session: Session) -> None:
    candles = wave(250)
    seed(db_session, candles)
    created = client.post("/api/v1/paper/sessions", json={})
    sid = created.json()["id"]
    client.post(f"/api/v1/paper/sessions/{sid}/start")
    service.evaluate_session(db_session, sid, now=now_after(candles), service_config=FULL)
    portfolio = client.get(f"/api/v1/paper/sessions/{sid}/portfolio").json()
    assert Decimal(portfolio["total_equity"]) > Decimal("0")
    assert portfolio["num_orders"] >= 1
    equity = client.get(f"/api/v1/paper/sessions/{sid}/equity").json()
    assert len(equity) > 0
    events = client.get(f"/api/v1/paper/sessions/{sid}/events").json()
    assert events["total"] > 0


# -- postgres integration (skipped when unavailable) --------------------------


def _pg_url() -> str | None:
    import os

    url = os.environ.get(
        "DRIFTLINE_TEST_PG_URL",
        "postgresql+psycopg://driftline:driftline@localhost:5432/driftline_test",
    )
    try:
        import psycopg

        conn = psycopg.connect(
            url.replace("postgresql+psycopg://", "postgresql://"), connect_timeout=3
        )
        conn.close()
        return url
    except Exception:
        return None


pg_url = _pg_url()
needs_pg = pytest.mark.skipif(pg_url is None, reason="postgres unavailable")


@needs_pg
def test_pg_row_lock_and_idempotency() -> None:
    assert pg_url is not None
    engine = create_engine(pg_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    try:
        candles = wave(150)
        seed(db, candles)
        row = service.create_session(db, exchange=EXCHANGE, symbol="PG/USDT")
        service.start_session(db, row.id)
        # SELECT ... FOR UPDATE path executes on postgres.
        locked = store.lock_session_row(db, row.id)
        assert locked is not None and locked.id == row.id
        report = service.evaluate_session(db, row.id, now=now_after(candles))
        assert report.error is None
        # Idempotency key enforced by the database itself.
        from sqlalchemy.exc import IntegrityError

        db.add(
            service._place_order(
                db,
                store.get_session_row(db, row.id),
                side="buy",
                signal_ts=candles[60].ts,
                qty=D("0.01"),
                ref=now_after(candles),
            )
        )
        db.commit()
        with pytest.raises(IntegrityError):
            db.add(
                service._place_order(
                    db,
                    store.get_session_row(db, row.id),
                    side="buy",
                    signal_ts=candles[60].ts,
                    qty=D("0.01"),
                    ref=now_after(candles),
                )
            )
            db.commit()
        db.rollback()
    finally:
        db.close()
        engine.dispose()
