"""Deterministic historical backtesting engine.

Execution model (anti-lookahead):
  * Strategy emits a signal on completed candle ``i`` (close known).
  * The fill happens at candle ``i+1``'s OPEN — a price unknowable at signal
    time — adjusted for slippage, minus fees.
  * Signals on the final candle cannot fill (no next open) and are reported
    as unfilled.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from driftline.backtesting.metrics import compute_metrics
from driftline.core.logging import get_logger
from driftline.domain.models import BacktestMetrics, Candle, EquityPoint, Signal, SignalType, Trade
from driftline.risk.manager import OrderIntent, RiskConfig, RiskManager

log = get_logger("backtesting")


@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: float = 10_000.0
    fee_rate: float = 0.001
    slippage_rate: float = 0.0005
    allocation: float = 1.0
    min_quantity: float = 0.0
    price_precision: int | None = None
    qty_precision: int | None = None

    def __post_init__(self) -> None:
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if not 0 <= self.fee_rate < 1:
            raise ValueError("fee_rate must be in [0, 1)")
        if not 0 <= self.slippage_rate < 1:
            raise ValueError("slippage_rate must be in [0, 1)")
        if not 0 < self.allocation <= 1:
            raise ValueError("allocation must be in (0, 1]")


@dataclass
class BacktestResult:
    metrics: BacktestMetrics
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[EquityPoint] = field(default_factory=list)
    unfilled_signals: int = 0


def _round_to(value: float, precision: int | None) -> float:
    if precision is None:
        return value
    factor = 10**precision
    return float(round(value * factor) / factor)


def run_backtest(
    candles: list[Candle],
    signals: list[Signal],
    config: BacktestConfig,
    *,
    strategy_name: str = "ema_crossover",
    timeframe: str = "1h",
    data_source: str = "exchange",
    risk_config: RiskConfig | None = None,
) -> BacktestResult:
    if len(candles) < 2:
        raise ValueError("backtest needs at least 2 candles")
    if config.initial_capital <= 0:
        raise ValueError("initial_capital must be positive")

    risk = RiskManager(risk_config) if risk_config is not None else None
    by_index: dict[int, list[Signal]] = {}
    for s in signals:
        by_index.setdefault(s.index, []).append(s)

    cash = config.initial_capital
    qty = 0.0
    entry_price = 0.0
    entry_index = 0
    entry_time = candles[0].ts
    entry_fee_paid = 0.0
    total_fees = 0.0
    peak_equity = config.initial_capital

    trades: list[Trade] = []
    equity_curve: list[EquityPoint] = []
    unfilled = 0
    bars_in_market = 0

    for i, candle in enumerate(candles):
        # 1. Fill signals generated on the PREVIOUS completed candle at this open.
        for sig in by_index.get(i - 1, []):
            if i >= len(candles):
                unfilled += 1
                continue
            fill_open = candles[i].open
            if sig.signal is SignalType.ENTRY and qty <= 1e-12:
                eff_price = fill_open * (1.0 + config.slippage_rate)
                notional = cash * config.allocation
                raw_qty = notional / (eff_price * (1.0 + config.fee_rate))
                raw_qty = _round_to(raw_qty, config.qty_precision)
                if raw_qty <= max(config.min_quantity, 0.0):
                    continue
                price = _round_to(eff_price, config.price_precision) or eff_price
                if risk is not None:
                    decision = risk.check(
                        OrderIntent(
                            side="buy",
                            quantity=raw_qty,
                            price=price,
                            timestamp=candle.ts,
                        ),
                        cash=cash,
                        position_qty=qty,
                        equity=cash + qty * fill_open,
                        peak_equity=peak_equity,
                        now=candle.ts,  # simulation clock: historical data is fresh at fill time
                    )
                    if not decision:
                        log.info("entry blocked by risk: %s", decision.reasons)
                        continue
                cost = raw_qty * price
                fee = cost * config.fee_rate
                if cost + fee > cash + 1e-9:
                    # Scale down to affordable size instead of overspending.
                    affordable = cash / (price * (1.0 + config.fee_rate))
                    affordable = _round_to(affordable, config.qty_precision)
                    if affordable <= max(config.min_quantity, 0.0):
                        continue
                    raw_qty, cost, fee = (
                        affordable,
                        affordable * price,
                        affordable * price * config.fee_rate,
                    )
                cash -= cost + fee
                total_fees += fee
                qty = raw_qty
                entry_price = price
                entry_index = i
                entry_time = candle.ts
                entry_fee_paid = fee
            elif sig.signal is SignalType.EXIT and qty > 1e-12:
                eff_price = fill_open * (1.0 - config.slippage_rate)
                price = _round_to(eff_price, config.price_precision) or eff_price
                if risk is not None:
                    decision = risk.check(
                        OrderIntent(side="sell", quantity=qty, price=price, timestamp=candle.ts),
                        cash=cash,
                        position_qty=qty,
                        equity=cash + qty * fill_open,
                        peak_equity=peak_equity,
                        now=candle.ts,
                    )
                    if not decision:
                        log.info("exit blocked by risk: %s", decision.reasons)
                        continue
                proceeds = qty * price
                fee = proceeds * config.fee_rate
                cash += proceeds - fee
                total_fees += fee
                pnl = (price - entry_price) * qty - fee - entry_fee_paid
                cost_basis = entry_price * qty + entry_fee_paid
                trades.append(
                    Trade(
                        entry_index=entry_index,
                        exit_index=i,
                        entry_time=entry_time,
                        exit_time=candle.ts,
                        entry_price=entry_price,
                        exit_price=price,
                        quantity=qty,
                        entry_fee=entry_fee_paid,
                        exit_fee=fee,
                        realized_pnl=pnl,
                        return_pct=(pnl / cost_basis * 100.0) if cost_basis else 0.0,
                    )
                )
                qty, entry_price, entry_fee_paid = 0.0, 0.0, 0.0

        # 2. Mark equity at this candle's close (consistent valuation convention).
        equity = cash + qty * candle.close
        peak_equity = max(peak_equity, equity)
        if qty > 1e-12:
            bars_in_market += 1
        equity_curve.append(
            EquityPoint(
                timestamp=candle.ts,
                equity=equity,
                cash=cash,
                position_qty=qty,
                close=candle.close,
            )
        )

    # Signals on the last candle can never fill (no next open).
    if by_index.get(len(candles) - 1):
        unfilled += len(by_index[len(candles) - 1])

    # Forced liquidation is NOT assumed: an open position at the end is left
    # open and valued at the last close (documented; no fabricated exit fill).
    metrics = compute_metrics(
        initial_capital=config.initial_capital,
        equity_curve=equity_curve,
        trades=trades,
        total_fees=total_fees,
        closes=[c.close for c in candles],
        bars_in_market=bars_in_market,
        total_bars=len(candles),
        data_source=data_source,
        strategy_name=strategy_name,
        timeframe=timeframe,
    )
    if qty > 1e-12:
        metrics.assumptions.append(
            "Open position at end of history is marked to last close, not liquidated."
        )
    if risk is None:
        metrics.assumptions.append(
            "No pre-trade risk veto in this run; sizing by allocation with "
            "cash-sufficiency scaling (pass risk_config to enforce guards)."
        )
    else:
        metrics.assumptions.append(
            "Pre-trade risk veto active (custom risk_config), evaluated on the simulation clock."
        )
    return BacktestResult(
        metrics=metrics, trades=trades, equity_curve=equity_curve, unfilled_signals=unfilled
    )
