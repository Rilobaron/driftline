# Driftline

> An AI-assisted quantitative trading, backtesting and market research platform built with Python.

**Driftline v0.1** is a reproducible, testable quantitative-trading foundation:
real historical OHLCV ingestion (CCXT public data), a deterministic indicator
library, a baseline EMA-crossover strategy, a lookahead-free backtesting
engine, virtual-portfolio accounting, conservative risk guards, and a FastAPI
service. Simulated trading only — **no live trading, no real money**.

**Driftline v0.2a — Strategy Lab** adds a multi-strategy research layer:
a schema-validated strategy registry (EMA crossover, RSI mean reversion,
Donchian breakout), fair side-by-side comparison, chronological 70/30
in-sample/out-of-sample splits, deterministic walk-forward validation with
parameter search, reproducible experiment records, and research API endpoints.
See [docs/strategy-lab.md](docs/strategy-lab.md) and
[docs/research-methodology.md](docs/research-methodology.md).

**Driftline v0.2b** adds automated multi-market data operations: scheduled
BTC/USDT, ETH/USDT and SOL/USDT (`1h`) ingestion with bootstrap, incremental
sync, gap detection/repair, data-quality health, a dedicated worker process,
and operational APIs. See [docs/market-automation.md](docs/market-automation.md)
and [docs/data-quality.md](docs/data-quality.md).

> **AI status:** AI assistance (strategy review, market commentary via
> OpenAI-compatible providers such as FreeLLMAPI) is planned for later phases
> and is **not implemented in v0.1**. The future AI layer will remain advisory
> only and will never bypass deterministic risk controls.

## Features (implemented in v0.1)

- **Market data engine** — paginated CCXT history (default Binance `BTC/USDT`
  `1h`), retries with backoff, rate-limit respect, UTC normalization, OHLCV
  validation, duplicate/gap detection, forming-candle exclusion, idempotent
  incremental sync into PostgreSQL (SQLite fallback for local dev).
- **Market automation (v0.2b)** — multi-market scheduler (BTC/ETH/SOL `1h`
  spot) in a dedicated worker, UTC-boundary timing with sync delay, resumable
  ~12-month bootstrap, restart recovery, bounded gap repair (gaps preserved,
  never fabricated), exchange-scoped provenance, deterministic
  HEALTHY/STALE/DEGRADED/ERROR/BOOTSTRAPPING health, sync-run history, and
  ops-protected `POST /api/v1/market/sync|repair` endpoints.
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
- **Strategy Lab (v0.2a)** — strategy registry with parameter schemas;
  RSI mean-reversion and Donchian breakout baselines; fair multi-strategy
  comparison (annualized return, Sortino added); chronological in-sample/
  out-of-sample splits; walk-forward validation with bounded parameter search;
  reproducible experiment records (`GET /api/v1/research/experiments`).
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

Market automation worker (sole scheduler owner — run alongside the API):

```bash
alembic upgrade head
uvicorn driftline.api.main:app --reload
DRIFTLINE_SCHEDULER_ENABLED=1 uv run python -m driftline.worker
```

Or everything with Compose: `docker compose up --build` (starts `db`, `api`, `worker`).

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
| `DRIFTLINE_MARKET_STREAMS` | `binance:BTC/USDT:1h,binance:ETH/USDT:1h,binance:SOL/USDT:1h` | Market automation streams (`!`-prefix disables) |
| `DRIFTLINE_SYNC_DELAY_SECONDS` | `90` | Delay after UTC candle close before sync |
| `DRIFTLINE_BOOTSTRAP_MONTHS` | `12` | Bootstrap depth for new streams |
| `DRIFTLINE_SCHEDULER_ENABLED` | `0` | `1` only in the worker process |
| `DRIFTLINE_STALE_AFTER_SECONDS` | `8100` | Freshness threshold for health |
| `DRIFTLINE_FETCH_TIMEOUT` | `30.0` | Provider timeout (s) |
| `DRIFTLINE_FETCH_MAX_RETRIES` | `5` | Bounded provider retries |
| `DRIFTLINE_OPS_TOKEN` | _(empty)_ | Required token for sync/repair when set |

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

Strategy Lab research (see [docs/strategy-lab.md](docs/strategy-lab.md)):

```bash
uv run python scripts/research_demo.py   # comparison + splits + walk-forward
```

```text
GET  /api/v1/strategies/{strategy_id}
POST /api/v1/research/compare
POST /api/v1/research/split-test
POST /api/v1/research/walk-forward
GET  /api/v1/research/experiments
GET  /api/v1/research/experiments/{id}
```

## API documentation

Run the service and open `http://localhost:8000/docs` (OpenAPI/Swagger) or
`http://localhost:8000/redoc`. Key routes:

```text
GET  /health
GET  /api/v1/market/candles
GET  /api/v1/market/indicators
GET  /api/v1/market/streams
GET  /api/v1/market/streams/{stream_id}
GET  /api/v1/market/health
POST /api/v1/market/sync            # ops token when configured
POST /api/v1/market/repair          # ops token when configured
GET  /api/v1/market/sync-history
GET  /api/v1/strategies
POST /api/v1/backtests
GET  /api/v1/backtests/{id}
GET  /api/v1/backtests/{id}/trades
GET  /api/v1/backtests/{id}/equity
```

## Development roadmap

- **v0.2a (done)** — Strategy Lab: strategy registry, RSI/Donchian baselines,
  comparison engine, IS/OOS splits, walk-forward validation, experiment log.
- **v0.2** — multi-market sync scheduler ✅ (v0.2b), more strategies ✅ (v0.2a),
  persistent paper-trading service, React dashboard.
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
