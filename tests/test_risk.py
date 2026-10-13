"""Risk manager: allocation, cash, stale data, drawdown, invalid orders."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from driftline.risk.manager import OrderIntent, RiskConfig, RiskManager

NOW = datetime.now(UTC)


def _intent(**kw: object) -> OrderIntent:
    base: dict[str, object] = {
        "side": "buy",
        "quantity": 1.0,
        "price": 100.0,
        "timestamp": NOW,
    }
    base.update(kw)
    return OrderIntent(**base)  # type: ignore[arg-type]


def test_valid_order_approved() -> None:
    rm = RiskManager(RiskConfig())
    d = rm.check(_intent(), cash=10_000.0, position_qty=0.0, equity=10_000.0, peak_equity=10_000.0)
    assert d.approved and d.reasons == ()


def test_allocation_breach_rejected() -> None:
    rm = RiskManager(RiskConfig(max_position_allocation=0.5))
    d = rm.check(
        _intent(quantity=100.0, price=100.0),
        cash=10_000.0,
        position_qty=0.0,
        equity=10_000.0,
        peak_equity=10_000.0,
    )
    assert not d and any("allocation" in r for r in d.reasons)


def test_insufficient_cash_rejected() -> None:
    rm = RiskManager()
    d = rm.check(
        _intent(quantity=10.0, price=100.0),
        cash=50.0,
        position_qty=0.0,
        equity=10_000.0,
        peak_equity=10_000.0,
    )
    assert not d and any("cash" in r for r in d.reasons)


def test_invalid_quantity_and_price_rejected() -> None:
    rm = RiskManager()
    d = rm.check(
        _intent(quantity=0.0), cash=1000.0, position_qty=0.0, equity=1000.0, peak_equity=1000.0
    )
    assert not d and any("quantity" in r for r in d.reasons)
    d2 = rm.check(
        _intent(price=-5.0), cash=1000.0, position_qty=0.0, equity=1000.0, peak_equity=1000.0
    )
    assert not d2 and any("price" in r for r in d2.reasons)


def test_stale_data_rejected() -> None:
    rm = RiskManager(RiskConfig(max_stale_seconds=3600))
    stale = NOW - timedelta(hours=5)
    d = rm.check(
        _intent(timestamp=stale), cash=1000.0, position_qty=0.0, equity=1000.0, peak_equity=1000.0
    )
    assert not d and any("stale" in r for r in d.reasons)


def test_drawdown_blocks_entries_but_not_exits() -> None:
    rm = RiskManager(RiskConfig(max_portfolio_drawdown=0.10))
    entry = rm.check(
        _intent(side="buy"), cash=1000.0, position_qty=0.0, equity=800.0, peak_equity=1000.0
    )
    assert not entry and any("drawdown" in r for r in entry.reasons)
    sell = rm.check(
        _intent(side="sell", quantity=0.5, price=100.0),
        cash=100.0,
        position_qty=1.0,
        equity=800.0,
        peak_equity=1000.0,
    )
    assert sell.approved


def test_stop_loss_risk_limit() -> None:
    rm = RiskManager(RiskConfig(max_risk_per_trade=0.01))
    d = rm.check(
        _intent(quantity=10.0, price=100.0, stop_loss_price=50.0),
        cash=10_000.0,
        position_qty=0.0,
        equity=10_000.0,
        peak_equity=10_000.0,
    )
    assert not d and any("per-trade" in r for r in d.reasons)


def test_nan_caller_state_rejected() -> None:
    rm = RiskManager()
    nan = float("nan")
    d = rm.check(_intent(), cash=nan, position_qty=0.0, equity=10_000.0, peak_equity=10_000.0)
    assert not d and any("cash" in r for r in d.reasons)
    d2 = rm.check(_intent(), cash=1000.0, position_qty=0.0, equity=nan, peak_equity=1000.0)
    assert not d2 and any("equity" in r for r in d2.reasons)
    d3 = rm.check(
        _intent(side="sell", quantity=0.5, price=100.0),
        cash=100.0,
        position_qty=nan,
        equity=800.0,
        peak_equity=1000.0,
    )
    assert not d3 and any("position" in r for r in d3.reasons)
