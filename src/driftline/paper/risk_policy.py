"""Mandatory risk policy for paper trading (simulated funds only).

Every simulated order passes through :class:`PaperRiskPolicy`. The base
:class:`~driftline.risk.manager.RiskManager` guards (stale data, allocation,
cash, drawdown) always apply; the paper layer adds kill-switch, single-position
and UTC-day loss limits. Risk parameters are server-side configuration —
public API requests cannot disable or relax them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from driftline.paper.money import D
from driftline.risk.manager import OrderIntent, RiskConfig, RiskDecision, RiskManager


def _as_float(data: dict[str, object], key: str, default: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"invalid paper risk value for {key!r}: {value!r}")
    return float(value)


def _as_int(data: dict[str, object], key: str, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"invalid paper risk value for {key!r}: {value!r}")
    return int(value)


@dataclass(frozen=True)
class PaperRiskConfig:
    # Paper defaults differ from the base RiskConfig on purpose: a paper
    # session holds one full-equity long position with no cash floor, so the
    # allocation ceiling is 1.0 and the minimum balance is 0. The guards stay
    # fully enforced — raise them through server configuration, never the API.
    max_position_allocation: float = 1.0
    max_risk_per_trade: float = 0.02
    max_portfolio_drawdown: float = 0.20
    min_available_balance: float = 0.0
    max_stale_seconds: int = 7200
    max_daily_loss_pct: float = 0.05
    max_concurrent_positions: int = 1

    def __post_init__(self) -> None:
        # Reuse base bounds validation; daily-loss/positions validated here.
        RiskConfig(
            max_position_allocation=self.max_position_allocation,
            max_risk_per_trade=self.max_risk_per_trade,
            max_portfolio_drawdown=self.max_portfolio_drawdown,
            min_available_balance=self.min_available_balance,
            max_stale_seconds=self.max_stale_seconds,
        )
        if not 0 < self.max_daily_loss_pct < 1:
            raise ValueError("max_daily_loss_pct must be in (0, 1)")
        if self.max_concurrent_positions != 1:
            raise ValueError("paper trading supports exactly 1 concurrent position")

    def to_dict(self) -> dict[str, object]:
        return {
            "max_position_allocation": self.max_position_allocation,
            "max_risk_per_trade": self.max_risk_per_trade,
            "max_portfolio_drawdown": self.max_portfolio_drawdown,
            "min_available_balance": self.min_available_balance,
            "max_stale_seconds": self.max_stale_seconds,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "max_concurrent_positions": self.max_concurrent_positions,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> PaperRiskConfig:
        return cls(
            max_position_allocation=_as_float(data, "max_position_allocation", 1.0),
            max_risk_per_trade=_as_float(data, "max_risk_per_trade", 0.02),
            max_portfolio_drawdown=_as_float(data, "max_portfolio_drawdown", 0.20),
            min_available_balance=_as_float(data, "min_available_balance", 0.0),
            max_stale_seconds=_as_int(data, "max_stale_seconds", 7200),
            max_daily_loss_pct=_as_float(data, "max_daily_loss_pct", 0.05),
            max_concurrent_positions=_as_int(data, "max_concurrent_positions", 1),
        )


@dataclass(frozen=True)
class PaperRiskContext:
    cash: Decimal
    position_qty: Decimal
    equity: Decimal
    peak_equity: Decimal
    day_start_equity: Decimal
    kill_switch: bool = False
    now: datetime | None = None


class PaperRiskPolicy:
    """Mandatory pre-trade gate. Exits are never blocked by loss limits."""

    def __init__(self, config: PaperRiskConfig | None = None) -> None:
        self.config = config or PaperRiskConfig()
        self._base = RiskManager(
            RiskConfig(
                max_position_allocation=self.config.max_position_allocation,
                max_risk_per_trade=self.config.max_risk_per_trade,
                max_portfolio_drawdown=self.config.max_portfolio_drawdown,
                min_available_balance=self.config.min_available_balance,
                max_stale_seconds=self.config.max_stale_seconds,
            )
        )

    def check(self, intent: OrderIntent, ctx: PaperRiskContext) -> RiskDecision:
        reasons: list[str] = []
        cfg = self.config
        if intent.side == "buy":
            if ctx.kill_switch:
                reasons.append("kill switch engaged: new entries blocked")
            if ctx.position_qty > Decimal("0"):
                reasons.append("already holding a position: max 1 concurrent position (long-only)")
            day_start = D(ctx.day_start_equity)
            equity = D(ctx.equity)
            if day_start > Decimal("0") and equity > Decimal("0"):
                daily_loss = (day_start - equity) / day_start
                if daily_loss > D(str(cfg.max_daily_loss_pct)):
                    reasons.append(
                        f"simulated daily loss {float(daily_loss):.2%} exceeds limit "
                        f"{cfg.max_daily_loss_pct:.0%}: new entries blocked until next UTC day"
                    )
        base = self._base.check(
            intent,
            cash=float(ctx.cash),
            position_qty=float(ctx.position_qty),
            equity=float(ctx.equity),
            peak_equity=float(ctx.peak_equity),
            now=ctx.now,
        )
        reasons.extend(base.reasons)
        return RiskDecision(approved=not reasons, reasons=tuple(reasons))
