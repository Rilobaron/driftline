"""Portfolio accounting invariants, fees, PnL, and error paths."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from driftline.core.exceptions import InsufficientFundsError, ValidationError
from driftline.portfolio.portfolio import Portfolio

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def test_buy_sell_roundtrip_accounting() -> None:
    p = Portfolio(cash=10_000.0)
    p.buy(price=100.0, quantity=10.0, timestamp=T0, fee_rate=0.001)
    assert p.cash == pytest.approx(10_000.0 - 1000.0 - 1.0)
    assert p.avg_entry_price == pytest.approx(100.0)
    assert p.equity(110.0) == pytest.approx(p.cash + 10.0 * 110.0)
    assert p.unrealized_pnl(110.0) == pytest.approx(100.0)
    p.sell(price=110.0, quantity=10.0, timestamp=T0, fee_rate=0.001)
    # realized = (110-100)*10 - exit fee 1.1
    assert p.realized_pnl == pytest.approx(100.0 - 1.1)
    assert p.position_qty == 0.0
    assert p.avg_entry_price == 0.0
    assert p.total_fees == pytest.approx(2.1)


def test_invariants_hold_after_each_op() -> None:
    p = Portfolio(cash=1_000.0)
    p.buy(price=50.0, quantity=4.0, timestamp=T0)
    p.check_invariants(mark_price=55.0)
    p.sell(price=55.0, quantity=2.0, timestamp=T0)
    p.check_invariants(mark_price=55.0)
    assert p.cash >= 0 and p.position_qty >= 0


def test_insufficient_funds_rejected() -> None:
    p = Portfolio(cash=100.0)
    with pytest.raises(InsufficientFundsError):
        p.buy(price=100.0, quantity=10.0, timestamp=T0)


def test_oversell_rejected() -> None:
    p = Portfolio(cash=1_000.0)
    p.buy(price=100.0, quantity=1.0, timestamp=T0)
    with pytest.raises(ValidationError):
        p.sell(price=100.0, quantity=2.0, timestamp=T0)


def test_invalid_inputs_rejected() -> None:
    p = Portfolio()
    with pytest.raises(ValidationError):
        p.buy(price=-1.0, quantity=1.0, timestamp=T0)
    with pytest.raises(ValidationError):
        p.buy(price=1.0, quantity=0.0, timestamp=T0)
    with pytest.raises(ValidationError):
        p.equity(-5.0)
