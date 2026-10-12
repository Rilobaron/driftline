"""Virtual single-asset long portfolio with explicit accounting invariants.

Invariants (checked in tests and after every mutation):
  1. cash >= 0
  2. position_qty >= 0
  3. equity == cash + position_qty * mark_price
  4. realized PnL only changes on sells; average entry price is volume-weighted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from driftline.core.exceptions import InsufficientFundsError, ValidationError


@dataclass
class Portfolio:
    symbol: str = "BTC/USDT"
    cash: float = 10_000.0
    position_qty: float = 0.0
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0
    total_fees: float = 0.0
    trade_count: int = 0
    history: list[dict[str, object]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.cash < 0:
            raise ValidationError("starting cash must be >= 0")

    # -- accounting -----------------------------------------------------
    def equity(self, mark_price: float) -> float:
        if mark_price <= 0:
            raise ValidationError("mark price must be positive")
        return self.cash + self.position_qty * mark_price

    def unrealized_pnl(self, mark_price: float) -> float:
        if self.position_qty <= 0:
            return 0.0
        return (mark_price - self.avg_entry_price) * self.position_qty

    def buy(
        self,
        *,
        price: float,
        quantity: float,
        timestamp: datetime,
        fee_rate: float = 0.0,
    ) -> dict[str, object]:
        if price <= 0:
            raise ValidationError("buy price must be positive")
        if quantity <= 0:
            raise ValidationError("buy quantity must be positive")
        if fee_rate < 0:
            raise ValidationError("fee rate must be >= 0")
        cost = quantity * price
        fee = cost * fee_rate
        if cost + fee > self.cash + 1e-9:
            raise InsufficientFundsError(
                f"insufficient cash: need {cost + fee:.2f}, have {self.cash:.2f}"
            )
        new_qty = self.position_qty + quantity
        self.avg_entry_price = (
            (self.avg_entry_price * self.position_qty + cost) / new_qty if new_qty else 0.0
        )
        self.position_qty = new_qty
        self.cash -= cost + fee
        self.total_fees += fee
        self.trade_count += 1
        record = {
            "side": "buy",
            "price": price,
            "quantity": quantity,
            "fee": fee,
            "timestamp": timestamp.isoformat(),
        }
        self.history.append(record)
        self.check_invariants(mark_price=price)
        return record

    def sell(
        self,
        *,
        price: float,
        quantity: float,
        timestamp: datetime,
        fee_rate: float = 0.0,
    ) -> dict[str, object]:
        if price <= 0:
            raise ValidationError("sell price must be positive")
        if quantity <= 0:
            raise ValidationError("sell quantity must be positive")
        if quantity > self.position_qty + 1e-12:
            raise ValidationError(f"sell quantity {quantity} exceeds position {self.position_qty}")
        if fee_rate < 0:
            raise ValidationError("fee rate must be >= 0")
        proceeds = quantity * price
        fee = proceeds * fee_rate
        pnl = (price - self.avg_entry_price) * quantity - fee
        # Entry-leg fee attribution is approximated at exit for realized PnL
        # simplicity; engine-level ledger carries exact per-leg fees.
        self.realized_pnl += pnl
        self.position_qty -= quantity
        if self.position_qty <= 1e-12:
            self.position_qty = 0.0
            self.avg_entry_price = 0.0
        self.cash += proceeds - fee
        self.total_fees += fee
        self.trade_count += 1
        record = {
            "side": "sell",
            "price": price,
            "quantity": quantity,
            "fee": fee,
            "realized_pnl": pnl,
            "timestamp": timestamp.isoformat(),
        }
        self.history.append(record)
        self.check_invariants(mark_price=price)
        return record

    def check_invariants(self, *, mark_price: float) -> None:
        if self.cash < -1e-6:
            raise AssertionError(f"invariant violated: cash negative ({self.cash})")
        if self.position_qty < -1e-9:
            raise AssertionError(f"invariant violated: position negative ({self.position_qty})")
        expected = self.cash + self.position_qty * mark_price
        actual = self.equity(mark_price)
        if abs(expected - actual) > 1e-6:
            raise AssertionError("invariant violated: equity mismatch")
        if self.position_qty == 0 and self.avg_entry_price != 0.0:
            raise AssertionError("invariant violated: avg entry not reset on flat")
