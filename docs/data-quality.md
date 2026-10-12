# Driftline data quality (v0.2b)

Every market stream carries an explicit, deterministic health status computed
from persisted state — never from a single successful HTTP request.

## Status ladder (first match wins)

| Status | Condition |
|---|---|
| `DISABLED` | Stream disabled in configuration. |
| `ERROR` | ≥3 consecutive sync failures. Needs operator attention. |
| `DEGRADED` | ≥1 recent failure **or** ≥1 unresolved gap. Data exists but has known holes. |
| `BOOTSTRAPPING` | Initial history load still in progress (stored < target, no failures/gaps). |
| `STALE` | No failures/gaps, but the last completed candle is older than `DRIFTLINE_STALE_AFTER_SECONDS` (default 8100s ≈ 2.25h for hourly streams). |
| `HEALTHY` | Fresh, complete, no recent failures. |

Rationale: a stream that fetched fine but still has holes is `DEGRADED`, not
`HEALTHY`; a stream failing repeatedly during bootstrap surfaces as
`DEGRADED`/`ERROR` rather than hiding behind `BOOTSTRAPPING`.

## Metrics per stream (`GET /api/v1/market/health`)

- `last_completed_ts` — newest persisted **completed** candle (forming candles
  are never stored, so research can never consume them).
- `last_success_at` / `last_attempt_at` — sync bookkeeping in UTC.
- `expected_last_completed` — newest candle that *should* exist right now.
- `freshness_seconds` — `now − last_completed_ts` (null when empty).
- `total_stored` — candle count under this stream's exchange provenance.
- `missing_count` + `unresolved_gaps` — preserved holes (ISO timestamps,
  capped at 100 in state; full detail via `sync-history`).
- `last_duration_ms`, `last_error`, `provider_available` (when known).

The market-level `overall` is the worst stream status (`ERROR` > `STALE` >
`DEGRADED` > `BOOTSTRAPPING` > `HEALTHY`; `DISABLED` streams don't affect it).

## Provenance and research snapshots

- Every candle is namespaced by `(exchange, symbol, timeframe)` with a matching
  uniqueness constraint: two exchanges never merge into one dataset.
- Research selects `exchange` explicitly (backtest `exchange` field, default
  `binance`) and loads only that provenance.
- Each backtest returns a `dataset_fingerprint` (SHA-256 over exchange, symbol,
  timeframe, and full OHLCV content): any later mutation of the underlying
  data changes the fingerprint, so experiments pin their exact dataset.
- `data_warnings` accompany results: missing-candle counts and an explicit
  `synthetic_fixture` label when no stored data exists.
- `min_candles` lets callers reject thin datasets outright (HTTP 422) instead
  of silently backtesting them.
- Running synchronization only appends completed candles; it never mutates a
  materialized research snapshot (snapshots are loaded per request from
  immutable history plus the fingerprint).
