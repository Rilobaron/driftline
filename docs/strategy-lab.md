# Strategy Lab (v0.2a)

Multi-strategy quantitative research on top of the v0.1 engine. All research
runs are long-only, spot-style, with next-candle-open fills, fees, and
slippage. Nothing here predicts the future.

## Strategies

| ID | Name | Defaults | Warmup |
|---|---|---|---|
| `ema_crossover` | EMA Crossover (baseline, unchanged) | fast 20, slow 50, ATR 14 | slow period |
| `rsi_mean_reversion` | RSI Mean Reversion | RSI 14, oversold 30, exit 50 | RSI period + 1 |
| `donchian_breakout` | Donchian Breakout | entry 20, exit 10 | max(entry, exit) |

**EMA crossover** (v1.0.0): entry when fast EMA crosses above slow EMA on a
completed candle; exit on the reverse cross. Behavior is identical to v0.1.

**RSI mean reversion** (v1.0.0): entry when RSI *crosses below* the oversold
threshold; exit when RSI *crosses above* the exit threshold. Crossing (not
level-holding) semantics plus a position-state machine guarantee at most one
entry per excursion and strict ENTRY/EXIT alternation. Requires
`0 < oversold < exit_threshold < 100`. Mean reversion can fail persistently in
trending markets.

**Donchian breakout** (v1.0.0): entry when a completed candle closes above the
highest high of the previous N completed candles; exit when it closes below
the lowest low of the previous M. The evaluated candle is **never** part of
its own channel (`highs[i-lb:i]`). Including it would let the candle's own
range suppress genuine breakouts — the test suite pins the correct behavior
with a case that only fires under exclusion.

## Registry

`StrategyRegistry` maps IDs to strategy classes with explicit parameter
schemas (`ParameterSpec`: kind, default, min/max). It fills defaults, rejects
unknown keys, range-checks values, runs each strategy's semantic validation
(e.g. fast < slow), and instantiates. Legacy `create_strategy` /
`list_strategies` keep working for all three strategies.

## Comparison

`compare_strategies` runs N strategies on the **identical** candle snapshot
with identical capital, fees, slippage, execution timing, and buy-and-hold
benchmark, and reports per-strategy metrics plus warnings (zero/small-sample
trade counts, open positions, selection bias, overfitting). No winner score;
`sort_entries` orders by any single metric for dashboard use.

## Splits (in-sample / out-of-sample)

`run_split_test` cuts chronologically (default 70/30, never shuffled). Each
partition receives pre-period warmup history for indicator seeding, but only
signals inside the partition can fill. **Capital is reset**: every window
starts flat with initial capital. IS and OOS results are reported separately;
re-selecting on OOS and calling it unbiased is explicitly disallowed.

## Walk-forward

`run_walk_forward` advances fixed train/test windows by `step`
(`step >= test_window` enforced, so test bars never double-count). Per fold:
evaluate the grid on training data only, freeze the argmax (grid order breaks
ties), evaluate once on the unseen test window. Candidates below `min_trades`
are excluded; empty selections skip the fold with a recorded warning. Grid
size is capped at 64 combinations.

**Capital convention**: carry-forward — each test window starts with the
previous window's final equity; open positions are marked to close at
boundaries (frictionless-flattening assumption, documented in the report).
The aggregate out-of-sample result is computed from the concatenated
test-window ledger (equity curve + trades), never by averaging per-fold
returns.

## Reproducibility

Every run records a SHA-256 dataset fingerprint (exchange, symbol, timeframe,
full OHLCV), strategy ID/version/params, backtest and risk configs, code
version, git commit, methodology, results, and warnings (`ExperimentRecord`,
persisted in the `experiments` table and retrievable via the API).

## Example run (real data, 2026-10-12)

Dataset: 8,640 BTC/USDT 1h candles, fingerprint
`b7221690…47cad`, buy-and-hold −22.57%.

- EMA crossover: −21.18%, 77 trades, PF 0.71, Sharpe −0.72.
- RSI mean reversion: −27.65%, 68 trades, PF 0.57, Sharpe −1.28.
- Donchian breakout: −34.05%, 122 trades, PF 0.62, Sharpe −1.53.
- 70/30 splits: all IS deeply negative; OOS positive (+6% to +12%) but far
  below OOS buy-and-hold (+38.76%) — textbook in-sample/out-of-sample gap.
- Walk-forward (EMA grid, 12 folds): aggregate OOS +2.48% vs test-span
  buy-and-hold −10.42%; includes one large marked-open position — see
  `docs/research-methodology.md` on open-position treatment.

No profitability is claimed; several baselines lose money, as reported.
