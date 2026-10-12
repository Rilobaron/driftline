# Driftline architecture (v0.1 + v0.2a Strategy Lab + v0.2b Market Automation)

## Data flow

```mermaid
flowchart LR
    subgraph ingestion["Market data (v0.2b automation)"]
        CFG[market streams: exchange/symbol/timeframe] --> SCHED[worker scheduler: UTC boundaries + delay]
        SCHED --> EX[CCXT public OHLCV]
        EX --> VAL[validation + normalize]
        VAL --> DEDUP[dedup / gaps / drop forming candle]
        DEDUP --> DB[(PostgreSQL / SQLite)]
        DB --> Q[quality engine: HEALTHY/STALE/DEGRADED/ERROR/BOOTSTRAPPING]
        Q --> API[FastAPI v1: streams/health/sync/repair/sync-history]
    end
    DB --> IND[indica­tors: EMA/RSI/ATR]
    IND --> STRAT[strategies: EMA crossover, RSI mean reversion, Donchian]
    STRAT -->|signals @ close i| BT[backtest engine: fill @ open i+1]
    BT --> METRICS[metrics + equity curve + trade ledger]
    BT --> LAB[Strategy Lab: compare / split / walk-forward + experiments]
    LAB --> API
    RISK[risk manager] -. guards .-> BT
    PF[virtual portfolio] -. accounting .-> BT
```

## Anti-lookahead rule

Signals form on completed candle `i`. Fills occur at candle `i+1` open plus
slippage/fees. The engine never fills at the signal candle's own close.

## Equity convention

`equity = cash + position_qty * candle_close`, with fees realized on fills.
An open position at the end of history is marked to the last close, never
fabricated into an exit fill.

## Market automation (v0.2b)

- One dedicated worker owns the scheduler; the API never schedules.
- Per-stream DB leases prevent duplicate jobs across processes.
- Candles are namespaced by `(exchange, symbol, timeframe)`; provenance flows
  into dataset fingerprints, so research snapshots are reproducible and
  exchange-scoped. See [market-automation](market-automation.md) and
  [data-quality](data-quality.md).

## Strategy Lab (v0.2a)

- `StrategyRegistry` validates and instantiates versioned strategies from
  explicit parameter schemas; all strategies emit engine-compatible signals.
- `research/comparison.py` evaluates N strategies on one identical snapshot
  (same candles, capital, costs, timing, benchmark); no winner score.
- `research/splits.py` cuts chronologically (default 70/30) with warmup
  prefixes and per-window capital reset; OOS never informs selection.
- `research/walk_forward.py` freezes grid-search winners on past training
  windows and scores them once on unseen, non-overlapping test windows;
  the aggregate comes from the concatenated OOS ledger with carry-forward
  capital. See [strategy-lab](strategy-lab.md).
- `experiments` table + `ExperimentRecord` (fingerprint, configs, code
  version, warnings) make every run reproducible and dashboard-queryable.
