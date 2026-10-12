"""Deterministic market-data scheduler (UTC candle boundaries, single owner).

The scheduler never lives inside ordinary FastAPI request handling: it runs
only in the dedicated worker process (``DRIFTLINE_SCHEDULER_ENABLED=1``) or in
tests with a controlled clock. Cross-process safety comes from DB-backed
per-stream leases; same-process overlap is prevented with an in-memory guard.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from driftline.core.logging import get_logger
from driftline.market_data.clock import ensure_utc, expected_last_completed
from driftline.market_data.streams import MarketStream
from driftline.market_data.sync import SyncReport, sync_stream
from driftline.market_data.validation import timeframe_to_seconds
from driftline.persistence import repository as repo

log = get_logger("market_data.scheduler")


@dataclass(frozen=True)
class SchedulerConfig:
    sync_delay_seconds: int = 90
    poll_interval_seconds: int = 60
    lease_seconds: int = 600
    owner_id: str = ""
    lookback_months: int = 12
    total_limit_per_run: int = 2000
    page_limit: int = 1000
    max_retries: int = 5
    bootstrap_target: int = 8760
    stale_after_seconds: int = 8100

    def owner(self) -> str:
        if self.owner_id:
            return self.owner_id
        return f"{socket.gethostname()}:{__import__('os').getpid()}"


def next_due_at(
    last_completed: datetime | None,
    timeframe: str,
    now: datetime,
    *,
    delay_seconds: int,
) -> datetime:
    """When the next sync for this stream should fire (UTC, deterministic)."""
    ref = ensure_utc(now)
    interval = timeframe_to_seconds(timeframe)
    expected = expected_last_completed(ref, timeframe)
    if last_completed is None:
        return ref  # bootstrap immediately
    last = ensure_utc(last_completed)
    if last < expected - timedelta(seconds=interval):
        return ref  # multiple candles behind: recover immediately
    if last < expected:
        # One candle behind: wait for publication latency, then sync.
        return expected + timedelta(seconds=interval + max(0, delay_seconds))
    # Up to date: shortly after the next candle closes.
    boundary = expected + timedelta(seconds=interval)
    return boundary + timedelta(seconds=max(0, delay_seconds))


def failure_cooldown_seconds(consecutive_failures: int) -> int:
    if consecutive_failures <= 0:
        return 0
    return int(min(300 * (2 ** (consecutive_failures - 1)), 3600))


def is_stream_due(
    *,
    enabled: bool,
    last_completed: datetime | None,
    last_attempt_at: datetime | None,
    consecutive_failures: int,
    unresolved_count: int,
    timeframe: str,
    now: datetime,
    delay_seconds: int,
) -> bool:
    """Pure due-check used by the scheduler loop and unit tests."""
    if not enabled:
        return False
    ref = ensure_utc(now)
    if last_completed is None:
        return True
    if unresolved_count > 0:
        return True
    if consecutive_failures > 0 and last_attempt_at is not None:
        attempt = ensure_utc(last_attempt_at)
        return not (ref - attempt).total_seconds() < failure_cooldown_seconds(consecutive_failures)
    return ref >= next_due_at(last_completed, timeframe, ref, delay_seconds=delay_seconds)


ProviderFactory = Callable[[str], object]
SessionFactory = Callable[[], Session]


class MarketScheduler:
    """Owns periodic sync for all enabled streams (worker process only)."""

    def __init__(
        self,
        session_factory: SessionFactory,
        provider_factory: ProviderFactory,
        config: SchedulerConfig,
        *,
        ensure_streams: list[MarketStream] | None = None,
    ) -> None:
        self._sessions = session_factory
        self._providers = provider_factory
        self.config = config
        self._preset = list(ensure_streams or [])
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._local_running: set[str] = set()

    # -- introspection ----------------------------------------------------

    def due_streams(self, session: Session, now: datetime) -> list[int]:
        """IDs of streams due at ``now`` (pure read, no locking)."""
        out: list[int] = []
        for row in repo.list_streams(session):
            if is_stream_due(
                enabled=bool(row.enabled),
                last_completed=row.last_completed_ts,
                last_attempt_at=row.last_attempt_at,
                consecutive_failures=int(row.consecutive_failures or 0),
                unresolved_count=len(row.unresolved_gaps or []),
                timeframe=row.timeframe,
                now=now,
                delay_seconds=self.config.sync_delay_seconds,
            ):
                out.append(int(row.id))
        return out

    # -- execution --------------------------------------------------------

    def run_once(self, now: datetime | None = None) -> list[SyncReport]:
        """Sync every due stream once. Never overlaps the same stream."""

        ref = ensure_utc(now) if now is not None else datetime.now(UTC)
        session = self._sessions()
        try:
            for preset in self._preset:
                repo.get_or_create_stream(
                    session,
                    exchange=preset.exchange,
                    symbol=preset.symbol,
                    timeframe=preset.timeframe,
                    enabled=preset.enabled,
                    bootstrap_target=self.config.bootstrap_target,
                )
            # Disabled presets stay disabled on re-declaration.
            for preset in self._preset:
                existing = repo.find_stream(
                    session,
                    exchange=preset.exchange,
                    symbol=preset.symbol,
                    timeframe=preset.timeframe,
                )
                if existing is not None and existing.enabled != preset.enabled:
                    existing.enabled = preset.enabled
                    session.commit()
            due_ids = self.due_streams(session, ref)
        finally:
            session.close()

        reports: list[SyncReport] = []
        for stream_id in due_ids:
            session = self._sessions()
            try:
                row = repo.get_stream(session, stream_id)
                if row is None or not row.enabled:
                    continue
                key = f"{row.exchange}:{row.symbol}:{row.timeframe}"
                if key in self._local_running:
                    log.info("%s: skipped (sync already running here)", key)
                    continue
                if not repo.acquire_stream_lock(
                    session,
                    row,
                    owner=self.config.owner(),
                    lease_seconds=self.config.lease_seconds,
                    now=ref,
                ):
                    log.info("%s: skipped (locked by another worker)", key)
                    continue
                self._local_running.add(key)
                try:
                    provider = self._providers(row.exchange)
                    log.info(
                        "%s: starting %s sync",
                        key,
                        "bootstrap" if not row.bootstrap_done else "incremental",
                    )
                    report = sync_stream(
                        session,
                        exchange=row.exchange,
                        symbol=row.symbol,
                        timeframe=row.timeframe,
                        provider=provider,  # type: ignore[arg-type]
                        now=ref,
                        kind="bootstrap" if not row.bootstrap_done else "incremental",
                        lookback_months=self.config.lookback_months,
                        total_limit=(
                            self.config.bootstrap_target
                            if not row.bootstrap_done
                            else self.config.total_limit_per_run
                        ),
                        page_limit=self.config.page_limit,
                        max_retries=self.config.max_retries,
                        bootstrap_target=self.config.bootstrap_target,
                    )
                    reports.append(report)
                    log.info(
                        "%s: %s (new=%d missing=%d)",
                        key,
                        "ok" if report.success else f"incomplete: {report.error}",
                        report.stored_new,
                        report.missing,
                    )
                except Exception as exc:
                    log.warning("%s: scheduler caught error: %s", key, str(exc)[:300])
                finally:
                    self._local_running.discard(key)
                    try:
                        fresh = repo.get_stream(session, stream_id)
                        if fresh is not None:
                            repo.release_stream_lock(session, fresh, owner=self.config.owner())
                    except Exception:
                        pass
            finally:
                session.close()
        return reports

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="driftline-scheduler", daemon=True)
        self._thread.start()
        log.info("scheduler started (owner=%s)", self.config.owner())

    def _loop(self) -> None:
        # Startup recovery: catch up immediately, then follow candle boundaries.
        try:
            self.run_once()
        except Exception as exc:
            log.warning("startup recovery failed: %s", str(exc)[:300])
        while not self._stop.wait(self.config.poll_interval_seconds):
            try:
                self.run_once()
            except Exception as exc:
                log.warning("scheduled run failed: %s", str(exc)[:300])

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        log.info("scheduler stopped (graceful)")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
