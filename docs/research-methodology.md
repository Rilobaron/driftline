# Research methodology

How Driftline v0.2a produces historical statistics without fooling itself
(or you). Descriptive statistics describe the past; they are not evidence of
predictive performance.

## Execution assumptions (inherited from v0.1, unchanged)

- Signals form on completed candle close; fills occur at the **next** candle
  open, plus slippage, minus per-leg fees.
- Long-only spot, single active position; entries while invested are ignored.
- Equity convention: `cash + qty * candle close`, fees realized on fills.
- Open positions at the end of an evaluation are **marked to last close**,
  never fabricated into exits. Reports warn whenever this applies.
- Default runs apply no pre-trade risk veto (allocation sizing with
  cash-sufficiency scaling); passing `risk_config` enables vetoes evaluated
  on the simulation clock.

## Metric definitions and edge cases

- **Total / annualized return**: annualized only for spans ≥ 30 days
  (compounded over 365.25 days), else `null`.
- **Sharpe / Sortino**: bar-return based, annualized (8760 for hourly data).
  `null` when fewer than 2 returns or zero (downside) deviation. Sortino uses
  downside deviation of sub-zero bar returns.
- **Profit factor**: `null` for zero trades **and** for zero-loss histories
  (v0.1 reported `Infinity`; v0.2a reports `null` — a finite sample without
  losers has no measurable loss scale). This is an explicit, documented
  reporting change; all other baseline assumptions are untouched.
- **Win rate / expectancy**: `null` with zero trades.
- **Exposure**: share of evaluation bars held long (warmup prefix excluded).

## Anti-overfitting apparatus

1. **Fixed baselines first**: strategies and defaults are declared before
   evaluation; defaults are not tuned to the dataset in this phase.
2. **Chronological splits**: no shuffling, no OOS-informed selection, capital
   reset per window, IS/OOS reported separately.
3. **Walk-forward**: parameters frozen on past training windows; test windows
   unseen; non-overlapping tests; deterministic tie-breaking; candidate caps;
   aggregate from the OOS ledger.
4. **Warnings everywhere**: zero/small trade counts, open positions, gaps,
   selection bias (multiple comparisons), and the overfitting caveat that
   optimized history does not imply future performance.
5. **No significance theater**: no p-values, confidence scores, or winner
   badges. Sample sizes here (tens of trades) cannot support them.

## Known limitations

- One ~12-month BTC/USDT 1h regime: results are regime-specific.
- Indicator seeding inside windows uses window+warmup data, which can differ
  marginally from full-history seeding at window starts.
- Walk-forward carry-forward treats marked-open value as cash at boundaries.
- Fees/slippage are flat-rate approximations, not order-book simulation.
- No transaction sequencing beyond one position; no partial fills.

## Why optimization does not guarantee future performance

Markets are non-stationary and adversarial: patterns decay once exploited,
regimes shift, and costs/frictions differ live. Any parameter set is a
historical best-fit with estimation error; selecting the max over many
candidates harvests noise (selection bias); small samples produce wide,
unreported variance. Treat every number here as a backtest of a hypothesis,
never as a forecast.
