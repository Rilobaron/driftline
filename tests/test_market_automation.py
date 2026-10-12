"""Driftline v0.2b market automation tests (mock providers — no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from driftline.api import main as api_main
from driftline.core.exceptions import MarketDataError, ValidationError
from driftline.domain.models import Candle
from driftline.market_data import resilience as resilience_mod
from driftline.market_data.ccxt_provider import CcxtMarketDataProvider
from driftline.market_data.clock import (
    expected_last_completed,
    floor_to_candle,
    next_run_at,
)
from driftline.market_data.gaps import find_missing, group_intervals
from driftline.market_data.quality import compute_quality, determine_status
from driftline.market_data.resilience import RetryConfig, backoff_delay, classify_error
from driftline.market_data.scheduler import (
    MarketScheduler,
    SchedulerConfig,
    is_stream_due,
    next_due_at,
)
from driftline.market_data.streams import (
    MarketStream,
    parse_market_streams,
    validate_symbol,
    validate_timeframe,
)
from driftline.market_data.sync import (
    bootstrap_stream,
    repair_stream_gaps,
    sync_stream,
)
from driftline.persistence import repository as repo
from driftline.persistence.models import Base, StreamStatus

NOW = datetime(2024, 6, 1, tzinfo=UTC)


def hourly(start: datetime, n: int, *, price: float = 100.0) -> list[Candle]:
    out: list[Candle] = []
    for i in range(n):
        c = price + i * 0.1
        o = c - 0.05
        out.append(
            Candle(
                ts=start + timedelta(hours=i),
                open=o,
                high=max(o, c) + 0.1,
                low=min(o, c) - 0.1,
                close=c,
                volume=10.0,
            )
        )
    return out


class DictProvider:
    """Deterministic in-memory provider for tests."""

    name = "mockex"

    def __init__(self, candles: list[Candle], name: str = "mockex") -> None:
        self._candles = sorted(candles, key=lambda c: c.ts)
        self.name = name

    def _slice(self, since_ms: int | None, limit: int) -> list[Candle]:
        out = [
            c for c in self._candles if since_ms is None or int(c.ts.timestamp() * 1000) >= since_ms
        ]
        return out[:limit]

    def fetch_history(
        self,
        symbol: str,
        timeframe: str,
        *,
        total_limit: int = 8760,
        since_ms: int | None = None,
        page_limit: int = 1000,
        max_retries: int = 5,
    ) -> list[Candle]:
        return self._slice(since_ms, total_limit)

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        *,
        since_ms: int | None = None,
        limit: int = 1000,
    ) -> list[Candle]:
        return self._slice(since_ms, limit)


def fresh_db() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


# --- configuration -----------------------------------------------------------


def test_default_streams_cover_three_markets() -> None:
    streams = parse_market_streams("binance:BTC/USDT:1h,binance:ETH/USDT:1h,binance:SOL/USDT:1h")
    assert [(s.symbol, s.timeframe) for s in streams] == [
        ("BTC/USDT", "1h"),
        ("ETH/USDT", "1h"),
        ("SOL/USDT", "1h"),
    ]
    assert all(s.enabled for s in streams)


def test_stream_disable_flag() -> None:
    (a, b) = parse_market_streams("binance:BTC/USDT:1h,!binance:SOL/USDT:1h")
    assert a.enabled and not b.enabled


def test_unsupported_markets_rejected() -> None:
    with pytest.raises(ValidationError):
        validate_symbol("BTC/USDT:USDT")  # perpetual future
    with pytest.raises(ValidationError):
        validate_symbol("BTCUSDT")  # missing separator
    with pytest.raises(ValidationError):
        validate_timeframe("3fortnights")
    with pytest.raises(ValidationError):
        parse_market_streams("binance:BTC/USDT")[0]  # missing timeframe part


# --- deterministic clock -----------------------------------------------------


def test_candle_boundary_math() -> None:
    ts = datetime(2024, 6, 1, 12, 34, 56, tzinfo=UTC)
    assert floor_to_candle(ts, "1h") == datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    # At 12:00 exactly the 11:00 candle just completed.
    assert expected_last_completed(datetime(2024, 6, 1, 12, 0, tzinfo=UTC), "1h") == datetime(
        2024, 6, 1, 11, 0, tzinfo=UTC
    )
    # Mid-hour: previous full hour is the last completed.
    assert expected_last_completed(ts, "1h") == datetime(2024, 6, 1, 11, 0, tzinfo=UTC)
    nxt = next_run_at(ts, "1h", delay_seconds=90)
    assert nxt == datetime(2024, 6, 1, 13, 0, tzinfo=UTC) + timedelta(seconds=90)


# --- bootstrap + incremental sync --------------------------------------------


def test_bootstrap_then_incremental_is_idempotent() -> None:
    factory = fresh_db()
    session = factory()
    candles = hourly(NOW - timedelta(hours=48), 48)
    provider = DictProvider(candles)
    first = bootstrap_stream(
        session,
        exchange="mockex",
        symbol="BTC/USDT",
        timeframe="1h",
        provider=provider,
        now=NOW,
        lookback_months=1,
        bootstrap_target=48,
    )
    assert first.success and first.stored_new == 48 and first.total_stored == 48
    assert first.data_source == "exchange:mockex"
    second = sync_stream(
        session,
        exchange="mockex",
        symbol="BTC/USDT",
        timeframe="1h",
        provider=provider,
        now=NOW + timedelta(minutes=30),
    )
    assert second.success and second.stored_new == 0 and second.total_stored == 48
    session.close()


def test_forming_candle_excluded() -> None:
    factory = fresh_db()
    session = factory()
    # 48 complete + 1 still forming at NOW.
    candles = hourly(NOW - timedelta(hours=48), 49)
    provider = DictProvider(candles)
    report = bootstrap_stream(
        session,
        exchange="mockex",
        symbol="BTC/USDT",
        timeframe="1h",
        provider=provider,
        now=NOW,
        lookback_months=1,
        bootstrap_target=48,
    )
    assert report.success and report.total_stored == 48
    session.close()


def test_restart_recovery_fills_missed_hours() -> None:
    factory = fresh_db()
    session = factory()
    full = hourly(NOW - timedelta(hours=48), 48)
    early = DictProvider(full[:40])
    r1 = bootstrap_stream(
        session,
        exchange="mockex",
        symbol="BTC/USDT",
        timeframe="1h",
        provider=early,
        now=NOW - timedelta(hours=8),
        lookback_months=1,
        bootstrap_target=48,
    )
    assert r1.total_stored == 40
    # "Restart" 8h later with full history available: recover, don't rescan all.
    late = DictProvider(full)
    r2 = sync_stream(
        session,
        exchange="mockex",
        symbol="BTC/USDT",
        timeframe="1h",
        provider=late,
        now=NOW,
    )
    assert r2.success and r2.stored_new == 8 and r2.total_stored == 48
    assert r2.missing == 0
    session.close()


def test_sync_reports_unresolved_gaps_honestly() -> None:
    factory = fresh_db()
    session = factory()
    full = hourly(NOW - timedelta(hours=24), 24)
    gapped = [c for i, c in enumerate(full) if i not in (5, 6)]
    provider = DictProvider(gapped)
    report = bootstrap_stream(
        session,
        exchange="mockex",
        symbol="BTC/USDT",
        timeframe="1h",
        provider=provider,
        now=NOW,
        lookback_months=1,
        bootstrap_target=24,
    )
    assert not report.success
    assert report.missing == 2
    assert report.error is not None and "gaps preserved" in report.error
    # Gaps persisted for operators; nothing fabricated.
    stored = repo.load_candles(session, "BTC/USDT", "1h", limit=100, exchange="mockex")
    assert len(stored) == 22
    session.close()


# --- gap repair --------------------------------------------------------------


def test_gap_grouping_and_repair() -> None:
    full = hourly(NOW - timedelta(hours=24), 24)
    missing = [full[5].ts, full[6].ts, full[10].ts]
    groups = group_intervals(missing, "1h")
    assert len(groups) == 2  # {5,6} adjacent + {10} alone

    factory = fresh_db()
    session = factory()
    partial = [c for i, c in enumerate(full) if i not in (5, 6, 10)]
    seed = DictProvider(partial)
    r1 = bootstrap_stream(
        session,
        exchange="mockex",
        symbol="BTC/USDT",
        timeframe="1h",
        provider=seed,
        now=NOW,
        lookback_months=1,
        bootstrap_target=24,
    )
    assert not r1.success
    fix = DictProvider(full)
    r2 = repair_stream_gaps(
        session,
        exchange="mockex",
        symbol="BTC/USDT",
        timeframe="1h",
        provider=fix,
        now=NOW,
    )
    assert r2.success and r2.missing == 0
    assert repo.count_candles(session, "BTC/USDT", "1h", exchange="mockex") == 24
    session.close()


def test_unresolved_gaps_preserved_without_fabrication() -> None:
    factory = fresh_db()
    session = factory()
    full = hourly(NOW - timedelta(hours=24), 24)
    partial = [c for i, c in enumerate(full) if i != 7]
    seed = DictProvider(partial)
    bootstrap_stream(
        session,
        exchange="mockex",
        symbol="BTC/USDT",
        timeframe="1h",
        provider=seed,
        now=NOW,
        lookback_months=1,
        bootstrap_target=24,
    )
    # Repair provider also lacks the candle: gap stays, nothing invented.
    r2 = repair_stream_gaps(
        session,
        exchange="mockex",
        symbol="BTC/USDT",
        timeframe="1h",
        provider=seed,
        now=NOW,
    )
    assert not r2.success and r2.missing == 1
    assert repo.count_candles(session, "BTC/USDT", "1h", exchange="mockex") == 23
    session.close()


def test_find_missing_empty_when_continuous() -> None:
    candles = hourly(NOW - timedelta(hours=10), 10)
    missing = find_missing(
        [c.ts for c in candles], start=candles[0].ts, end=candles[-1].ts, timeframe="1h"
    )
    assert missing == []


# --- provider resilience -----------------------------------------------------


def test_retry_recovers_from_transient_failures() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise MarketDataError("connection timeout, reset by peer")
        return "ok"

    assert resilience_mod.run_with_retry(flaky, RetryConfig(max_retries=5)) == "ok"
    assert calls["n"] == 3


def test_permanent_and_geo_errors_not_retried() -> None:
    calls = {"n": 0}

    def bad_symbol() -> str:
        calls["n"] += 1
        raise MarketDataError("bad symbol: UNKNOWN/USDT")

    with pytest.raises(MarketDataError):
        resilience_mod.run_with_retry(bad_symbol, RetryConfig(max_retries=5))
    assert calls["n"] == 1
    assert classify_error(MarketDataError("exchange restricted in your area")) == "geo_blocked"

    cfg = RetryConfig(max_retries=3)
    assert 0.0 <= backoff_delay(0, cfg) <= cfg.max_delay_seconds


def test_ccxt_provider_retries_pages_then_succeeds(monkeypatch: Any) -> None:
    provider = CcxtMarketDataProvider("binance")
    candles = hourly(NOW - timedelta(hours=3), 3)
    attempts = {"n": 0}

    def fake_fetch(symbol: str, timeframe: str, **kwargs: Any) -> list[Candle]:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise MarketDataError("503 service unavailable (maintenance)")
        return list(candles)

    monkeypatch.setattr(provider, "fetch_ohlcv", fake_fetch)
    out = provider.fetch_history("BTC/USDT", "1h", total_limit=10, max_retries=5)
    assert len(out) == 3 and attempts["n"] == 3


def test_sync_failure_records_error_and_counts() -> None:
    factory = fresh_db()
    session = factory()

    class DeadProvider:
        name = "dead"

        def fetch_history(self, *a: Any, **k: Any) -> list[Candle]:
            raise MarketDataError("connection timeout")

        def fetch_ohlcv(self, *a: Any, **k: Any) -> list[Candle]:
            raise MarketDataError("connection timeout")

    report = sync_stream(
        session,
        exchange="dead",
        symbol="BTC/USDT",
        timeframe="1h",
        provider=DeadProvider(),
        now=NOW,
    )
    assert not report.success and report.error
    row = repo.find_stream(session, exchange="dead", symbol="BTC/USDT", timeframe="1h")
    assert row is not None and row.consecutive_failures == 1 and row.last_error
    runs, total = repo.list_sync_runs(session)
    assert total == 1 and not runs[0].success
    session.close()


# --- quality statuses --------------------------------------------------------


def test_health_status_ladder() -> None:
    now = NOW
    assert (
        determine_status(
            enabled=False,
            bootstrap_done=True,
            consecutive_failures=0,
            unresolved_count=0,
            last_completed=now,
            now=now,
            stale_after_seconds=8100,
        )
        == StreamStatus.DISABLED
    )
    assert (
        determine_status(
            enabled=True,
            bootstrap_done=True,
            consecutive_failures=3,
            unresolved_count=0,
            last_completed=now,
            now=now,
            stale_after_seconds=8100,
        )
        == StreamStatus.ERROR
    )
    assert (
        determine_status(
            enabled=True,
            bootstrap_done=False,
            consecutive_failures=0,
            unresolved_count=0,
            last_completed=now,
            now=now,
            stale_after_seconds=8100,
        )
        == StreamStatus.BOOTSTRAPPING
    )
    assert (
        determine_status(
            enabled=True,
            bootstrap_done=True,
            consecutive_failures=0,
            unresolved_count=2,
            last_completed=now,
            now=now,
            stale_after_seconds=8100,
        )
        == StreamStatus.DEGRADED
    )
    assert (
        determine_status(
            enabled=True,
            bootstrap_done=True,
            consecutive_failures=0,
            unresolved_count=0,
            last_completed=now - timedelta(hours=5),
            now=now,
            stale_after_seconds=8100,
        )
        == StreamStatus.STALE
    )
    assert (
        determine_status(
            enabled=True,
            bootstrap_done=True,
            consecutive_failures=0,
            unresolved_count=0,
            last_completed=now - timedelta(minutes=30),
            now=now,
            stale_after_seconds=8100,
        )
        == StreamStatus.HEALTHY
    )


def test_quality_report_marks_degraded_despite_success_with_gaps() -> None:
    factory = fresh_db()
    session = factory()
    full = hourly(NOW - timedelta(hours=24), 24)
    gapped = [c for i, c in enumerate(full) if i != 9]
    bootstrap_stream(
        session,
        exchange="mockex",
        symbol="BTC/USDT",
        timeframe="1h",
        provider=DictProvider(gapped),
        now=NOW,
        lookback_months=1,
        bootstrap_target=24,
    )
    row = repo.find_stream(session, exchange="mockex", symbol="BTC/USDT", timeframe="1h")
    assert row is not None
    q = compute_quality(session, row, now=NOW)
    assert q.status == StreamStatus.DEGRADED
    assert q.missing_count == 1 and q.total_stored == 23
    assert q.expected_last_completed == expected_last_completed(NOW, "1h")
    session.close()


# --- locks / scheduler -------------------------------------------------------


def test_stream_lock_single_owner() -> None:
    factory = fresh_db()
    session = factory()
    row = repo.get_or_create_stream(session, exchange="mockex", symbol="BTC/USDT", timeframe="1h")
    assert repo.acquire_stream_lock(session, row, owner="worker-a", now=NOW)
    assert not repo.acquire_stream_lock(session, row, owner="worker-b", now=NOW)
    repo.release_stream_lock(session, row, owner="worker-a")
    assert repo.acquire_stream_lock(session, row, owner="worker-b", now=NOW)
    # Expired leases are stolen.
    row.locked_until = NOW - timedelta(seconds=1)
    session.commit()
    assert repo.acquire_stream_lock(session, row, owner="worker-a", now=NOW)
    session.close()


def test_scheduler_due_logic() -> None:
    # Up to date: next run after close + delay.
    due_at = next_due_at(NOW - timedelta(hours=1), "1h", NOW, delay_seconds=90)
    assert due_at > NOW
    assert (
        is_stream_due(
            enabled=True,
            last_completed=NOW - timedelta(hours=1),
            last_attempt_at=NOW - timedelta(hours=1),
            consecutive_failures=0,
            unresolved_count=0,
            timeframe="1h",
            now=NOW,
            delay_seconds=90,
        )
        is False
    )
    # Never synced / gaps / disabled.
    assert (
        is_stream_due(
            enabled=True,
            last_completed=None,
            last_attempt_at=None,
            consecutive_failures=0,
            unresolved_count=0,
            timeframe="1h",
            now=NOW,
            delay_seconds=90,
        )
        is True
    )
    assert (
        is_stream_due(
            enabled=False,
            last_completed=None,
            last_attempt_at=None,
            consecutive_failures=0,
            unresolved_count=0,
            timeframe="1h",
            now=NOW,
            delay_seconds=90,
        )
        is False
    )
    # Failure cooldown suppresses hot retries.
    assert (
        is_stream_due(
            enabled=True,
            last_completed=NOW - timedelta(hours=5),
            last_attempt_at=NOW,
            consecutive_failures=2,
            unresolved_count=0,
            timeframe="1h",
            now=NOW,
            delay_seconds=90,
        )
        is False
    )


def test_scheduler_run_once_end_to_end() -> None:
    factory = fresh_db()
    candles = hourly(NOW - timedelta(hours=24), 24)
    state = {"candles": list(candles)}

    def providers(exchange: str) -> DictProvider:
        return DictProvider(state["candles"], name=exchange)

    sched = MarketScheduler(
        factory,
        providers,  # type: ignore[arg-type]
        SchedulerConfig(bootstrap_target=24),
        ensure_streams=[MarketStream("mockex", "BTC/USDT", "1h")],
    )
    reports = sched.run_once(now=NOW)
    assert len(reports) == 1 and reports[0].success
    session = factory()
    assert repo.count_candles(session, "BTC/USDT", "1h", exchange="mockex") == 24
    # Before the next candle closes (+delay), nothing is due.
    assert sched.run_once(now=NOW + timedelta(seconds=30)) == []
    assert repo.count_candles(session, "BTC/USDT", "1h", exchange="mockex") == 24
    # After the due time the scheduler fires a cheap no-op (idempotent).
    noop = sched.run_once(now=NOW + timedelta(minutes=30))
    assert len(noop) == 1 and noop[0].success and noop[0].stored_new == 0
    assert repo.count_candles(session, "BTC/USDT", "1h", exchange="mockex") == 24
    session.close()


# --- operational API ---------------------------------------------------------


def test_market_ops_endpoints(client: TestClient, monkeypatch: Any) -> None:
    candles = hourly(datetime.now(UTC) - timedelta(hours=30), 30)
    monkeypatch.setattr(
        CcxtMarketDataProvider, "fetch_history", lambda self, *a, **k: list(candles)
    )
    monkeypatch.setattr(CcxtMarketDataProvider, "fetch_ohlcv", lambda self, *a, **k: list(candles))
    r = client.get("/api/v1/market/streams")
    assert r.status_code == 200
    symbols = {s["symbol"] for s in r.json()}
    assert {"BTC/USDT", "ETH/USDT", "SOL/USDT"} <= symbols

    first = r.json()[0]
    r2 = client.get(f"/api/v1/market/streams/{first['id']}")
    assert r2.status_code == 200
    assert client.get("/api/v1/market/streams/999999").status_code == 404

    health = client.get("/api/v1/market/health")
    assert health.status_code == 200
    assert health.json()["overall"] in list(StreamStatus.ALL)

    sync = client.post(
        "/api/v1/market/sync",
        json={"exchange": "binance", "symbol": "BTC/USDT", "timeframe": "1h"},
    )
    assert sync.status_code == 200, sync.text
    assert sync.json()["data_source"] == "exchange:binance"

    repair = client.post(
        "/api/v1/market/repair",
        json={"exchange": "binance", "symbol": "BTC/USDT", "timeframe": "1h"},
    )
    assert repair.status_code == 200, repair.text

    hist = client.get("/api/v1/market/sync-history", params={"limit": 10})
    assert hist.status_code == 200
    assert hist.json()["total"] >= 2

    bad = client.post("/api/v1/market/sync", json={"symbol": "BTC/USDT:USDT"})
    assert bad.status_code == 422


def test_scheduler_start_stop_graceful() -> None:
    factory = fresh_db()
    seed = hourly(datetime.now(UTC) - timedelta(hours=6), 5)

    def providers(exchange: str) -> DictProvider:
        return DictProvider(seed, name=exchange)

    sched = MarketScheduler(
        factory,
        providers,  # type: ignore[arg-type]
        SchedulerConfig(poll_interval_seconds=1, bootstrap_target=5),
        ensure_streams=[MarketStream("mockex", "BTC/USDT", "1h")],
    )
    sched.start()
    assert sched.running
    sched.stop()
    assert not sched.running
    session = factory()
    assert repo.count_candles(session, "BTC/USDT", "1h", exchange="mockex") == 5
    session.close()


def test_ops_endpoints_require_token_when_configured(client: TestClient, monkeypatch: Any) -> None:
    monkeypatch.setattr(api_main.settings, "ops_token", "secret-token")
    r = client.post("/api/v1/market/sync", json={"symbol": "BTC/USDT"})
    assert r.status_code == 401
    r2 = client.post(
        "/api/v1/market/sync",
        json={"symbol": "BTC/USDT"},
        headers={"X-Driftline-Ops-Token": "secret-token"},
    )
    # Auth passes; provider may fail offline — but never 401.
    assert r2.status_code in (200, 400, 422, 500)


def test_research_exchange_provenance_and_coverage(client: TestClient) -> None:
    r = client.post(
        "/api/v1/backtests",
        json={"symbol": "BTC/USDT", "timeframe": "1h", "exchange": "kraken", "max_candles": 300},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data_source"] == "synthetic_fixture"
    assert body["dataset_fingerprint"]
    assert any("synthetic" in w for w in body["data_warnings"])

    short = client.post(
        "/api/v1/backtests",
        json={"symbol": "BTC/USDT", "max_candles": 300, "min_candles": 5000},
    )
    assert short.status_code == 422

    from driftline.research.fingerprints import dataset_fingerprint

    candles = hourly(NOW - timedelta(hours=10), 10)
    assert dataset_fingerprint(candles, "BTC/USDT", "1h", exchange="a") != dataset_fingerprint(
        candles, "BTC/USDT", "1h", exchange="b"
    )
