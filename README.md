# Driftline

> An AI-assisted quantitative trading, backtesting and market research platform built with Python.

**Driftline v0.1** is a reproducible, testable quantitative-trading foundation:
real historical OHLCV ingestion (CCXT public data), a deterministic indicator
library, a baseline EMA-crossover strategy, a lookahead-free backtesting
engine, virtual-portfolio accounting, conservative risk guards, and a FastAPI
service. Simulated trading only — **no live trading, no real money**.

> **AI status:** AI assistance (strategy review, market commentary via
> OpenAI-compatible providers such as FreeLLMAPI) is planned for later phases
> and is **not implemented in v0.1**. The future AI layer will remain advisory
> only and will never bypass deterministic risk controls.

## Features (implemented in v0.1)

- **Market data engine** — paginated CCXT history (default Binance `BTC/USDT`
  `1h`), retries with backoff, rate-limit respect, UTC normalization, OHLCV
  validation, duplicate/gap detection, forming-candle exclusion, idempotent
  incremental sync into PostgreSQL (SQLite fallback for local dev).
- **Indicators** — transparent deterministic EMA, RSI (Wilder), ATR (Wilder).
- **Baseline strategy** — long-only EMA crossover (20/50, ATR 14 context),
  warmup-gated, structured signals, no lookahead.
- **Backtesting engine** — next-candle-open fills with fees + slippage,
  long-only single position, cash/position/equity tracking, trade ledger,
  realized/unrealized PnL, max drawdown, total return, win rate, profit
  factor, Sharpe (annualized, when meaningful), expectancy, buy-and-hold
  benchmark, explicit assumptions log.
- **Virtual portfolio** — 10,000 USDT default, average-entry tracking, fee
  accounting, enforced accounting invariants.
- **Risk layer** — max allocation, per-trade risk (with stop), max drawdown
  gate, minimum balance, stale-data and invalid-order rejection with reasons.
- **API** — `GET /health`, market candles/indicators, strategy registry,
  backtest CRUD + trades + equity (OpenAPI at `/docs`).
- **Quality** — pytest suite (deterministic fixtures, no network), Ruff,
  mypy strict, GitHub Actions CI, Docker Compose (API + Postgres), Alembic
  migrations.

## Tech stack

Python 3.12 · FastAPI · Pydantic v2 · pandas · NumPy · CCXT · PostgreSQL ·
SQLAlchemy 2.x · Alembic · uv · pytest · Ruff · mypy · Docker/Compose ·
GitHub Actions. Frontend-ready: the engine is UI-agnostic; a React/TypeScript
dashboard can consume the v1 API without engine changes.

## Local setup

```bash
cp .env.example .env            # adjust DATABASE_URL as needed
uv sync --group dev             # or: pip install -e ".[dev]"
```

With Postgres (recommended):

```bash
docker compose up -d db
alembic upgrade head            # DATABASE_URL from environment
uvicorn driftline.api.main:app --reload
```

Without Postgres (SQLite fallback):

```bash
set DATABASE_URL=sqlite:///./driftline.db   # Windows
uvicorn driftline.api.main:app --reload
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./driftline.db` | Postgres (`postgresql+psycopg://…`) or SQLite |
| `DRIFTLINE_EXCHANGE` | `binance` | CCXT exchange id |
| `DRIFTLINE_SYMBOL` | `BTC/USDT` | Market |
| `DRIFTLINE_TIMEFRAME` | `1h` | Candle timeframe |
| `DRIFTLINE_INITIAL_CAPITAL` | `10000.0` | Demo/backtest capital |
| `DRIFTLINE_FEE_RATE` | `0.001` | Per-leg fee |
| `DRIFTLINE_SLIPPAGE_RATE` | `0.0005` | Fill slippage |

## Running tests

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

## Running historical backtests

Fixture demo (no network, clearly labeled synthetic data):

```bash
uv run driftline-demo
```

Live data (requires network access to the exchange):

```bash
uv run driftline-demo --live --limit 2000
```

Via API:

```bash
curl -X POST localhost:8000/api/v1/backtests \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"BTC/USDT","timeframe":"1h","max_candles":500}'
```

## API documentation

Run the service and open `http://localhost:8000/docs` (OpenAPI/Swagger) or
`http://localhost:8000/redoc`. Key routes:

```text
GET  /health
GET  /api/v1/market/candles
GET  /api/v1/market/indicators
GET  /api/v1/strategies
POST /api/v1/backtests
GET  /api/v1/backtests/{id}
GET  /api/v1/backtests/{id}/trades
GET  /api/v1/backtests/{id}/equity
```

## Development roadmap

- **v0.2** — multi-market sync scheduler, more strategies, persistent
  paper-trading service, React dashboard.
- **v0.3** — strategy ensembles, execution monitoring/alerting.
- **Later** — ML research hooks, advisory LLM layer (OpenAI-compatible,
  e.g. FreeLLMAPI) strictly behind risk controls. Never autonomous order
  execution.

## Known limitations

- Single-asset, single long position; no leverage, shorting, or order types.
- Basic allocation-style risk limits, not a production risk system.
- Backtests bounded to 5,000 candles per request.
- Exchange geo-restrictions fail clearly; switch `DRIFTLINE_EXCHANGE`
  (e.g. `kraken`, `coinbase`) where needed.
- No profitability claims: the EMA crossover is a validation baseline.

## Financial risk disclaimer

Driftline is research software for education and experimentation. Backtested
or simulated performance does not predict future results. Do not trade real
money based on this software. No live-trading capability is included, and none
should be inferred.

## License

MIT — see [LICENSE](LICENSE).
