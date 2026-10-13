# Driftline execution model (paper trading and backtesting)

Both engines use the same deterministic **next-candle-open** model so paper
results stay comparable with research backtests. All prices below are
simulated fills from stored candles — never real executions.

## Timing rule

A signal forming on completed candle `S` (close known at end of `S`) may only
fill at the **open of candle `S+1`**, a price unknowable when the signal was
generated. Consequences:

- Signals on the latest evaluated candle stay `PENDING` until the next
  completed candle exists.
- If the next open is unavailable (gap, stale feed), the order stays pending
  or expires with an explicit reason — it is never filled early, never
  interpolated, never fabricated.
- After a data gap, a pending order fills at the *first available* completed
  open after its signal; the fill assumptions record both timestamps.
- Each completed candle is evaluated at most once; evaluation requires a
  contiguous window (cursor continuity + no holes), otherwise the tick waits.

## Fill math (MARKET-style, long-only spot)

Entry fill at open `O` for quantity `q`:

```text
price = round(O × (1 + slippage), price_precision)
fee   = round(q × price × fee_rate, 8dp)
cash  -= q × price + fee
```

Exit fill mirrors with `(1 − slippage)`; proceeds minus fee return to cash.
Realized PnL on a closed round trip:

```text
pnl = (exit_price − avg_entry_price) × q − exit_fee − entry_fee
return_pct = pnl / (entry_price × q + entry_fee) × 100
```

## Sizing and affordability

Entry quantity targets `allocation × equity` at the signal close (ask-adjusted
for slippage and fees), floored to `qty_precision`. If the fill open moves
against the estimate so the full size is unaffordable, the fill **scales down**
to the affordable quantity (same rule as the backtester) and records
`scaled_to_affordable: true` in its assumptions. Below `min_quantity` the
order expires with reason `insufficient cash at fill` instead of overspending.
Cash can never go negative — a hard Decimal invariant aborts the tick
otherwise.

## Recorded assumptions

Every fill stores: `model: next-candle-open`, `execution: simulated`,
`signal_ts`, `fill_ts`, `fee_rate`, `slippage_rate`, `scaled_to_affordable`,
`data_source` (exchange provenance, e.g. `exchange:binance`). Paper trades
additionally carry entry/exit legs with per-leg fees.

## What is *not* modelled

Latency, bid/ask spread beyond slippage, partial fills, market impact,
funding, and exchange downtime during the fill candle. Paper equity therefore
overstates real-world achievability; treat it as an execution-logic check,
not a profitability claim.
