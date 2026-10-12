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
        # Zero-loss history: report None rather than a misleading infinity.
        # A finite sample without losers carries no measurable loss scale.
        profit_factor = None
    expectancy = (sum(t.realized_pnl for t in trades) / len(trades)) if trades else None

    closes_for_span = [p.timestamp for p in equity_curve]
    annualized: float | None = None
    if len(closes_for_span) >= 2 and initial_capital > 0 and final_equity > 0:
        span_days = (closes_for_span[-1] - closes_for_span[0]).total_seconds() / 86400.0
        if span_days >= 30.0:
            annualized = ((final_equity / initial_capital) ** (365.25 / span_days) - 1.0) * 100.0

    sharpe: float | None = None
    sortino: float | None = None
    if len(equities) >= 3:
        rets: list[float] = []
        for prev, cur in zip(equities[:-1], equities[1:], strict=False):
            if prev > 0:
                rets.append((cur - prev) / prev)
        if len(rets) >= 2:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            std = math.sqrt(var)
            # Annualize assuming hourly bars; documented assumption.
            periods_per_year = 8760.0 if timeframe.endswith("h") else 252.0
            if std > 1e-12:
                sharpe = (mean / std) * math.sqrt(periods_per_year)
            downside = [min(0.0, r) for r in rets]
            dd_var = sum(d * d for d in downside) / len(downside)
            if dd_var > 1e-24:
                sortino = (mean / math.sqrt(dd_var)) * math.sqrt(periods_per_year)

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
    if sortino is not None:
        assumptions.append(
            "Sortino uses downside deviation of sub-zero bar returns, same annualization."
        )
    if annualized is not None:
        assumptions.append("Annualized return compounds the window return over 365.25 days.")
    return BacktestMetrics(
        initial_capital=initial_capital,
        final_equity=final_equity,
        total_return_pct=total_return_pct,
        annualized_return_pct=annualized,
        buy_and_hold_return_pct=bh,
        max_drawdown_pct=drawdown_pct,
        num_trades=len(trades),
        win_rate_pct=win_rate,
        profit_factor=profit_factor,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        expectancy=expectancy,
        total_fees=total_fees,
        exposure_pct=exposure,
        assumptions=assumptions,
    )
