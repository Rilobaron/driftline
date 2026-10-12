"""Deterministic risk management foundation (conservative defaults).

This is a basic allocation/guard layer, not a production risk system.
Every rejection carries explicit reasons.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True)
class RiskConfig:
    max_position_allocation: float = 0.95
    max_risk_per_trade: float = 0.02
    max_portfolio_drawdown: float = 0.20
    min_available_balance: float = 10.0
    max_stale_seconds: int = 7200  # 2h for 1h candles

    def __post_init__(self) -> None:
        if not 0 < self.max_position_allocation <= 1:
            raise ValueError("max_position_allocation must be in (0, 1]")
        if not 0 < self.max_risk_per_trade <= 1:
            raise ValueError("max_risk_per_trade must be in (0, 1]")
        if not 0 < self.max_portfolio_drawdown < 1:
            raise ValueError("max_portfolio_drawdown must be in (0, 1)")
        if self.min_available_balance < 0:
            raise ValueError("min_available_balance must be >= 0")


@dataclass(frozen=True)
class OrderIntent:
    side: str  # "buy" | "sell"
    quantity: float
    price: float
    timestamp: datetime
    stop_loss_price: float | None = None


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def __bool__(self) -> bool:
        return self.approved


class RiskManager:
    """Stateless pre-trade guard. Keeps no positions; callers supply state."""

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()

    def check(
        self,
        intent: OrderIntent,
        *,
        cash: float,
        position_qty: float,
        equity: float,
        peak_equity: float,
        now: datetime | None = None,
    ) -> RiskDecision:
        reasons: list[str] = []
        cfg = self.config

        if intent.side not in ("buy", "sell"):
            reasons.append(f"invalid side: {intent.side!r}")
        if not (intent.quantity > 0):
            reasons.append(f"invalid quantity: {intent.quantity}")
        if not (intent.price > 0):
            reasons.append(f"invalid price: {intent.price}")

        now = now or datetime.now(UTC)
        ts = intent.timestamp
        if ts.tzinfo is None:
            reasons.append("naive timestamp: market data must be timezone-aware UTC")
        else:
            age = (now - ts.astimezone(UTC)).total_seconds()
            if age < -60:
                reasons.append("timestamp is in the future: possible clock/data error")
            elif age > cfg.max_stale_seconds:
                reasons.append(
                    f"stale market data: {age:.0f}s old (limit {cfg.max_stale_seconds}s)"
                )

        if intent.side == "buy":
            notional = intent.quantity * intent.price
            if equity > 0 and notional > equity * cfg.max_position_allocation:
                reasons.append(
                    f"position exceeds max allocation "
                    f"({notional:.2f} > {cfg.max_position_allocation:.0%} of {equity:.2f})"
                )
            total_cost = notional  # fees checked separately by portfolio/engine
            if total_cost > cash:
                reasons.append(f"order exceeds available cash ({total_cost:.2f} > {cash:.2f})")
            # Only flag when the order would leave dust below the minimum AND
            # the order itself is not tiny relative to cash.
            if (
                cash - total_cost < cfg.min_available_balance
                and total_cost > 0
                and cash > cfg.min_available_balance
            ):
                reasons.append(
                    f"order would breach minimum balance {cfg.min_available_balance:.2f}"
                )
            if intent.stop_loss_price is not None and equity > 0:
                risk = abs(intent.price - intent.stop_loss_price) * intent.quantity
                if risk > equity * cfg.max_risk_per_trade:
                    reasons.append(
                        f"trade risk {risk:.2f} exceeds per-trade limit "
                        f"({cfg.max_risk_per_trade:.0%} of equity)"
                    )
        elif intent.side == "sell":
            if intent.quantity > position_qty + 1e-12:
                reasons.append(f"sell quantity {intent.quantity} exceeds position {position_qty}")

        if peak_equity > 0 and equity > 0:
            drawdown = (peak_equity - equity) / peak_equity
            if drawdown > cfg.max_portfolio_drawdown:
                reasons.append(
                    f"portfolio drawdown {drawdown:.2%} exceeds limit "
                    f"{cfg.max_portfolio_drawdown:.2%}: new entries blocked"
                )
                if intent.side == "buy":
                    pass  # reason already recorded; exits still allowed below

        # Drawdown blocks entries only; exits must always be allowed.
        if intent.side == "sell":
            reasons = [r for r in reasons if "new entries blocked" not in r]

        return RiskDecision(approved=not reasons, reasons=tuple(reasons))
