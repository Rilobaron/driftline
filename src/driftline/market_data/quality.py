"""Deterministic data-quality assessment per market stream."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from driftline.market_data.clock import ensure_utc, expected_last_completed
from driftline.persistence import repository as repo
from driftline.persistence.models import MarketStreamRow, StreamStatus


@dataclass(frozen=True)
class QualityReport:
    exchange: str
    symbol: str
    timeframe: str
    status: str
    last_completed_ts: datetime | None
    last_success_at: datetime | None
    expected_last_completed: datetime
    freshness_seconds: float | None
    total_stored: int
    missing_count: int
    duplicates: int
    invalid: int
    unresolved_gaps: list[str] = field(default_factory=list)
    last_duration_ms: int = 0
    last_error: str | None = None
    provider_available: bool | None = None

    @property
    def stream_key(self) -> str:
        return f"{self.exchange}:{self.symbol}:{self.timeframe}"


def determine_status(
    *,
    enabled: bool,
    bootstrap_done: bool,
    consecutive_failures: int,
    unresolved_count: int,
    last_completed: datetime | None,
    now: datetime,
    stale_after_seconds: int,
) -> str:
    """Deterministic status ladder (first match wins).

    DISABLED → ERROR(>=3 consecutive failures) →
    DEGRADED(recent failure or unresolved gaps) → BOOTSTRAPPING(initial load
    still in progress) → STALE → HEALTHY.

    Gaps and failures outrank BOOTSTRAPPING so a stalled bootstrap with real
    problems is never masked as merely "in progress".
    """
    if not enabled:
        return StreamStatus.DISABLED
    if consecutive_failures >= 3:
        return StreamStatus.ERROR
    if consecutive_failures >= 1 or unresolved_count > 0:
        return StreamStatus.DEGRADED
    if not bootstrap_done:
        return StreamStatus.BOOTSTRAPPING
    if last_completed is None:
        return StreamStatus.STALE
    age = (ensure_utc(now) - ensure_utc(last_completed)).total_seconds()
    if age > stale_after_seconds:
        return StreamStatus.STALE
    return StreamStatus.HEALTHY


def compute_quality(
    session: Session,
    row: MarketStreamRow,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = 8100,
    provider_available: bool | None = None,
) -> QualityReport:
    """Assess one stream. Never HEALTHY on success alone with critical gaps."""
    ref = ensure_utc(now) if now is not None else datetime.now(UTC)
    total = repo.count_candles(session, row.symbol, row.timeframe, exchange=row.exchange)
    last_completed = repo.latest_candle_ts(
        session, row.symbol, row.timeframe, exchange=row.exchange
    )
    gaps: list[str] = [str(t) for t in row.unresolved_gaps or []]
    expected = expected_last_completed(ref, row.timeframe)
    freshness: float | None = None
    if last_completed is not None:
        freshness = (ref - ensure_utc(last_completed)).total_seconds()
    # Bootstrap is complete once the target history depth is stored.
    bootstrap_done = bool(row.bootstrap_done) or total >= (row.bootstrap_target or 8760)
    status = determine_status(
        enabled=bool(row.enabled),
        bootstrap_done=bootstrap_done,
        consecutive_failures=int(row.consecutive_failures or 0),
        unresolved_count=len(gaps),
        last_completed=last_completed,
        now=ref,
        stale_after_seconds=stale_after_seconds,
    )
    last_success = row.last_success_at
    if last_success is not None and last_success.tzinfo is None:
        last_success = last_success.replace(tzinfo=UTC)
    return QualityReport(
        exchange=row.exchange,
        symbol=row.symbol,
        timeframe=row.timeframe,
        status=status,
        last_completed_ts=last_completed,
        last_success_at=last_success,
        expected_last_completed=expected,
        freshness_seconds=freshness,
        total_stored=total,
        missing_count=len(gaps),
        duplicates=0,
        invalid=0,
        unresolved_gaps=gaps,
        last_duration_ms=int(row.last_duration_ms or 0),
        last_error=row.last_error,
        provider_available=provider_available,
    )
