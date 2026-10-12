# Driftline architecture (v0.1)

## Data flow

```mermaid
flowchart LR
    subgraph ingestion["Market data"]
        EX[CCXT public OHLCV] --> VAL[validation + normalize]
        VAL --> DEDUP[dedup / gaps / drop forming candle]
        DEDUP --> DB[(PostgreSQL / SQLite)]
    end
    DB --> IND[indica­tors: EMA/RSI/ATR]
    IND --> STRAT[EMA crossover strategy]
    STRAT -->|signals @ close i| BT[backtest engine: fill @ open i+1]
    BT --> METRICS[metrics + equity curve + trade ledger]
    BT --> API[FastAPI v1]
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
