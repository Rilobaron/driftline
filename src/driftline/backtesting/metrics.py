"""Backtest performance metrics (pure, deterministic)."""

from __future__ import annotations

import math

from driftline.domain.models import BacktestMetrics, EquityPoint, Trade


def _max_drawdown(equities: list[float]) -> float:
    peak = -math.inf
    worst = 0.0
    for e in equities:
        peak = max(peak, e)
        if peak > 0:
            worst = max(worst, (peak - e) / peak)
    return worst


def compute_metrics(
    *,
    initial_capital: float,
    equity_curve: list[EquityPoint],
    trades: list[Trade],
    total_fees: float,
    closes: list[float],
    bars_in_market: int,
    total_bars: int,
    data_source: str,
    strategy_name: str,
    timeframe: str,
) -> BacktestMetrics:
    equities = [p.equity for p in equity_curve]
    final_equity = equities[-1] if equities else initial_capital
    total_return_pct = (
        (final_equity - initial_capital) / initial_capital * 100.0 if initial_capital else 0.0
    )
    bh = (closes[-1] - closes[0]) / closes[0] * 100.0 if closes and closes[0] > 0 else 0.0
    drawdown_pct = _max_drawdown(equities) * 100.0 if equities else 0.0

    wins = [t for t in trades if t.realized_pnl > 0]
    losses = [t for t in trades if t.realized_pnl <= 0]
    win_rate = (len(wins) / len(trades) * 100.0) if trades else None
    gross_profit = sum(t.realized_pnl for t in wins)
    gross_loss = abs(sum(t.realized_pnl for t in losses))
    if not trades:
        profit_factor = None
    elif gross_loss > 1e-12:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = None if not wins else float("inf")
    expectancy = (sum(t.realized_pnl for t in trades) / len(trades)) if trades else None

    sharpe: float | None = None
    if len(equities) >= 3:
        rets: list[float] = []
        for prev, cur in zip(equities[:-1], equities[1:], strict=False):
            if prev > 0:
                rets.append((cur - prev) / prev)
        if len(rets) >= 2:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            std = math.sqrt(var)
            if std > 1e-12:
                # Annualize assuming hourly bars; documented assumption.
                periods_per_year = 8760.0 if timeframe.endswith("h") else 252.0
                sharpe = (mean / std) * math.sqrt(periods_per_year)

    exposure = (bars_in_market / total_bars * 100.0) if total_bars else 0.0
    assumptions = [
        "Long-only spot simulation; no leverage, no shorting.",
        "Signals form on completed candle close; fills occur at the next candle open.",
        "Fill price includes slippage; fees apply per filled leg.",
        "Equity convention: cash + position_qty * candle close (fees realized on fills).",
        "Single active long position in v0.1; entries ignored while invested.",
        f"Data source: {data_source}.",
        f"Strategy: {strategy_name}; timeframe {timeframe}.",
    ]
    if data_source == "synthetic_fixture":
        assumptions.append(
            "WARNING: synthetic fixture data — results validate plumbing only, "
            "not market performance."
        )
    if sharpe is not None and timeframe.endswith("h"):
        assumptions.append("Sharpe annualized with 8760 hourly periods/year.")
    return BacktestMetrics(
        initial_capital=initial_capital,
        final_equity=final_equity,
        total_return_pct=total_return_pct,
        buy_and_hold_return_pct=bh,
        max_drawdown_pct=drawdown_pct,
        num_trades=len(trades),
        win_rate_pct=win_rate,
        profit_factor=profit_factor,
        sharpe_ratio=sharpe,
        expectancy=expectancy,
        total_fees=total_fees,
        exposure_pct=exposure,
        assumptions=assumptions,
    )
