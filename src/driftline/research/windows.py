"""Windowed evaluation helper shared by splits and walk-forward.

Convention (documented, deterministic):
  * Indicators are computed on ``candles[eval_start - warmup_prefix : eval_end]``.
  * Only signals with ``eval_start <= global_index < eval_end`` can fill.
  * Each window starts flat with ``initial_capital`` cash (capital is RESET
    between windows; walk-forward explicitly opts into carry-forward instead).
  * Trades are re-indexed to GLOBAL candle indices for meaningful ledgers.
  * Buy-and-hold and exposure are measured on the evaluation window only,
    not on the warmup prefix.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from driftline.backtesting.engine import BacktestConfig, BacktestResult, run_backtest
from driftline.domain.models import BacktestMetrics, Candle, EquityPoint, Signal, Trade
from driftline.market_data.validation import analyze_gaps
from driftline.risk.manager import RiskConfig
from driftline.strategies.base import Strategy


@dataclass
class WindowEvaluation:
    eval_start: int
    eval_end: int
    warmup_prefix: int
    metrics: BacktestMetrics
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[EquityPoint] = field(default_factory=list)
    num_signals: int = 0
    unfilled_signals: int = 0
    missing_candles: int = 0
    warnings: list[str] = field(default_factory=list)


def default_warmup_prefix(strategy: Strategy) -> int:
    """Generous warmup so indicator seeding converges before evaluation."""
    return max(strategy.warmup_bars, 100)


def evaluate_window(
    candles: list[Candle],
    strategy: Strategy,
    config: BacktestConfig,
    *,
    eval_start: int,
    eval_end: int,
    warmup_prefix: int | None = None,
    strategy_id: str = "",
    timeframe: str = "1h",
    data_source: str = "exchange",
    risk_config: RiskConfig | None = None,
    min_history_error: bool = True,
) -> WindowEvaluation:
    """Run one evaluation window with pre-period warmup and no pre-window trades."""
    n = len(candles)
    if not 0 <= eval_start < eval_end <= n:
        raise ValueError(f"invalid evaluation window [{eval_start}, {eval_end}) for {n} candles")
    prefix = warmup_prefix if warmup_prefix is not None else default_warmup_prefix(strategy)
    if eval_start < strategy.warmup_bars and min_history_error and eval_start > 0:
        # Some pre-history exists but less than the strategy minimum: unsafe.
        raise ValueError(
            f"insufficient pre-window history ({eval_start} bars < warmup {strategy.warmup_bars})"
        )
    if eval_start == 0 and min_history_error and strategy.warmup_bars > 0:
        # First window has no pre-history; indicator seeding uses window data only.
        pass
    s0 = max(0, eval_start - prefix)
    window = candles[s0:eval_end]
    raw_signals = strategy.generate(window)

    kept: list[Signal] = []
    for s in raw_signals:
        global_idx = s.index + s0
        if eval_start <= global_idx < eval_end:
            kept.append(s)  # s.index is already local to `window`
    result: BacktestResult = run_backtest(
        window,
        kept,
        config,
        strategy_name=strategy_id or getattr(strategy, "name", "strategy"),
        timeframe=timeframe,
        data_source=data_source,
        risk_config=risk_config,
    )
    # Re-index trades to global candle coordinates.
    global_trades = [
        t.model_copy(update={"entry_index": t.entry_index + s0, "exit_index": t.exit_index + s0})
        for t in result.trades
    ]
    # Window-basis buy-and-hold and exposure (exclude the warmup prefix).
    window_closes = [c.close for c in candles[eval_start:eval_end]]
    if window_closes and window_closes[0] > 0:
        result.metrics.buy_and_hold_return_pct = (
            (window_closes[-1] - window_closes[0]) / window_closes[0] * 100.0
        )
    eval_len = eval_end - eval_start
    bars_in_market = sum(
        1
        for p in result.equity_curve
        if p.position_qty > 1e-12 and p.timestamp >= candles[eval_start].ts
    )
    result.metrics.exposure_pct = (bars_in_market / eval_len * 100.0) if eval_len else 0.0

    gap_report = analyze_gaps(candles[eval_start:eval_end], timeframe)
    warnings: list[str] = []
    if gap_report.missing:
        warnings.append(
            f"window [{eval_start}, {eval_end}): {len(gap_report.missing)} missing "
            f"{timeframe} candles — fills bridge gaps at next available open; "
            "treat metrics with caution."
        )
    if s0 > 0 and (eval_start - s0) < strategy.warmup_bars:
        warnings.append(
            "warmup prefix shorter than strategy minimum "
            f"({eval_start - s0} < {strategy.warmup_bars}); indicator seeding is approximate."
        )
    if eval_start == 0:
        warnings.append(
            "evaluation starts at dataset beginning: no pre-history for warmup; "
            "early indicator values are seeded from window data only."
        )
    return WindowEvaluation(
        eval_start=eval_start,
        eval_end=eval_end,
        warmup_prefix=eval_start - s0,
        metrics=result.metrics,
        trades=global_trades,
        equity_curve=result.equity_curve,
        num_signals=len(kept),
        unfilled_signals=result.unfilled_signals,
        missing_candles=len(gap_report.missing),
        warnings=warnings,
    )
