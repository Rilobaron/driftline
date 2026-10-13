"""Incremental sync, bootstrap, and gap-repair engine.

Design (reliability first):
  * Idempotent: repeated runs insert nothing new and report honestly.
  * Bounded: every run caps fetched candles; pagination for larger gaps.
  * Provenance-explicit: every candle batch is tagged ``exchange:{name}`` and
    persisted under that exchange — never merged across exchanges.
  * Honest: a run with remaining unexplained gaps reports ``success=False``
    with the gaps preserved, never silent success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.orm import Session

from driftline.core.logging import get_logger
from driftline.domain.models import Candle
from driftline.market_data.clock import ensure_utc, expected_last_completed
from driftline.market_data.gaps import (
    find_missing,
    group_intervals,
    interval_limit,
    interval_since_ms,
)
from driftline.market_data.validation import (
    analyze_gaps,
    deduplicate,
    drop_incomplete_last_candle,
    timeframe_to_seconds,
)
from driftline.persistence import repository as repo
from driftline.persistence.models import StreamStatus

log = get_logger("market_data.sync")

MAX_STORED_GAPS = 100


class SyncProvider(Protocol):
    name: str

    def fetch_history(
        self,
        symbol: str,
        timeframe: str,
        *,
        total_limit: int = 8760,
        since_ms: int | None = None,
        page_limit: int = 1000,
        max_retries: int = 5,
    ) -> list[Candle]: ...

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        *,
        since_ms: int | None = None,
        limit: int = 1000,
    ) -> list[Candle]: ...


@dataclass(frozen=True)
class SyncReport:
    exchange: str
    symbol: str
    timeframe: str
    kind: str
    success: bool
    fetched: int
    stored_new: int
    total_stored: int
    duplicates: int
    invalid: int
    missing: int
    unresolved_gaps: list[datetime] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0
    data_source: str = ""

    @property
    def provenance(self) -> str:
        return self.data_source


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _short_error(exc: Exception) -> str:
    # Never include config/credentials — exception text only, truncated.
    return str(exc)[:400]


def sync_stream(
    session: Session,
    *,
    exchange: str,
    symbol: str,
    timeframe: str,
    provider: SyncProvider,
    now: datetime | None = None,
    kind: str = "incremental",
    lookback_months: int = 12,
    total_limit: int = 2000,
    page_limit: int = 1000,
    max_retries: int = 5,
    bootstrap_target: int = 8760,
    stale_after_seconds: int = 8100,
) -> SyncReport:
    """Run one bounded sync for a single stream. Idempotent and restart-safe."""
    ref = ensure_utc(now) if now is not None else _utcnow()
    started = _utcnow()
    interval = timeframe_to_seconds(timeframe)
    data_source = f"exchange:{provider.name}"
    stream = repo.get_or_create_stream(
        session,
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        bootstrap_target=bootstrap_target,
    )
    label = f"{exchange}:{symbol}:{timeframe}"
    stream.last_attempt_at = started
    session.commit()

    latest = repo.latest_candle_ts(session, symbol, timeframe, exchange=exchange)
    if latest is None:
        since = ref - timedelta(days=30 * max(lookback_months, 1))
        if kind == "incremental":
            kind = "bootstrap"
    else:
        since = ensure_utc(latest) + timedelta(seconds=interval)
    expected_end = expected_last_completed(ref, timeframe)

    def _finish(
        *,
        success: bool,
        fetched: int,
        stored_new: int,
        duplicates: int,
        invalid: int,
        missing: list[datetime],
        error: str | None,
        provider_exhausted: bool = False,
    ) -> SyncReport:
        finished = _utcnow()
        duration_ms = max(0, int((finished - started).total_seconds() * 1000))
        total = repo.count_candles(session, symbol, timeframe, exchange=exchange)
        newest = repo.latest_candle_ts(session, symbol, timeframe, exchange=exchange)
        stream.last_duration_ms = duration_ms
        stream.last_completed_ts = newest
        stream.unresolved_gaps = [t.isoformat() for t in missing[:MAX_STORED_GAPS]]
        stream.updated_at = finished
        if success:
            stream.last_success_at = finished
            stream.last_error = None
            stream.consecutive_failures = 0
            # Bootstrap completes only on evidence: we hold the target depth, or
            # the provider is exhausted (returned fewer than requested) with no
            # remaining gaps. A bounded partial pass must NOT flip the flag —
            # otherwise the scheduler would switch to incremental mode and the
            # older range would never be backfilled.
            target = stream.bootstrap_target or 0
            if total >= target or provider_exhausted:
                stream.bootstrap_done = True
        else:
            stream.last_error = (error or "sync incomplete")[:512]
            stream.consecutive_failures = (stream.consecutive_failures or 0) + 1
        # Status is derived deterministically; see quality module.
        from driftline.market_data.quality import compute_quality

        stream.status = compute_quality(
            session, stream, now=ref, stale_after_seconds=stale_after_seconds
        ).status
        session.commit()
        repo.record_sync_run(
            session,
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            kind=kind,
            started_at=started,
            finished_at=finished,
            success=success,
            fetched=fetched,
            stored_new=stored_new,
            duplicates=duplicates,
            invalid=invalid,
            missing=len(missing),
            error=error,
            duration_ms=duration_ms,
            data_source=data_source,
        )
        return SyncReport(
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            kind=kind,
            success=success,
            fetched=fetched,
            stored_new=stored_new,
            total_stored=total,
            duplicates=duplicates,
            invalid=invalid,
            missing=len(missing),
            unresolved_gaps=list(missing),
            error=error,
            duration_ms=duration_ms,
            data_source=data_source,
        )

    if since > expected_end:
        # Nothing new expected; still verify persisted continuity.
        persisted = repo.load_candles(
            session, symbol, timeframe, limit=total_limit, exchange=exchange
        )
        missing: list[datetime] = []
        if persisted:
            missing = find_missing(
                [c.ts for c in persisted],
                start=persisted[0].ts,
                end=expected_end,
                timeframe=timeframe,
            )
        if missing:
            log.warning("%s: %d unresolved gaps (no new candles expected)", label, len(missing))
            return _finish(
                success=False,
                fetched=0,
                stored_new=0,
                duplicates=0,
                invalid=0,
                missing=missing,
                error=f"{len(missing)} expected candles unavailable; gaps preserved",
            )
        log.info("%s: up to date (%s)", label, expected_end.isoformat())
        return _finish(
            success=True,
            fetched=0,
            stored_new=0,
            duplicates=0,
            invalid=0,
            missing=[],
            error=None,
        )

    needed = int((expected_end - ensure_utc(since)).total_seconds() // interval) + 1
    bounded = max(1, min(total_limit, needed))
    since_ms = int(ensure_utc(since).timestamp() * 1000)
    try:
        fetched_raw = provider.fetch_history(
            symbol,
            timeframe,
            total_limit=bounded,
            since_ms=since_ms,
            page_limit=min(page_limit, 1000),
            max_retries=max_retries,
        )
    except Exception as exc:
        from driftline.core.exceptions import ValidationError as _VE

        invalid = 1 if isinstance(exc, _VE) else 0
        error = _short_error(exc)
        log.warning("%s sync failed: %s", label, error)
        persisted_now = repo.load_candles(
            session, symbol, timeframe, limit=total_limit, exchange=exchange
        )
        missing_now: list[datetime] = []
        if persisted_now:
            missing_now = find_missing(
                [c.ts for c in persisted_now],
                start=persisted_now[0].ts,
                end=expected_end,
                timeframe=timeframe,
            )
        return _finish(
            success=False,
            fetched=0,
            stored_new=0,
            duplicates=0,
            invalid=invalid,
            missing=missing_now,
            error=error,
        )

    # Post-fetch validation: drop forming candle, dedup, gap-check.
    usable = drop_incomplete_last_candle(fetched_raw, timeframe, now=ref)
    dropped_forming = len(fetched_raw) - len(usable)
    if dropped_forming:
        log.info("%s: excluded %d forming candle(s)", label, dropped_forming)
    batch_report = analyze_gaps(usable, timeframe)
    usable = deduplicate(usable)
    try:
        stored_new = repo.upsert_candles(session, symbol, timeframe, usable, exchange=exchange)
    except Exception as exc:
        error = _short_error(exc)
        log.warning("%s persist failed: %s", label, error)
        return _finish(
            success=False,
            fetched=len(fetched_raw),
            stored_new=0,
            duplicates=len(batch_report.duplicates),
            invalid=0,
            missing=list(batch_report.missing),
            error=error,
        )

    persisted = repo.load_candles(session, symbol, timeframe, limit=total_limit, exchange=exchange)
    remaining: list[datetime] = []
    if persisted:
        # Expected continuity runs from the earliest candle the provider has
        # ever returned through the last completed candle. History older than
        # our earliest candle is "unavailable from provider", not missing.
        remaining = find_missing(
            [c.ts for c in persisted],
            start=persisted[0].ts,
            end=expected_end,
            timeframe=timeframe,
        )
    if remaining:
        # Honest failure: interval incomplete, gaps preserved (no fabrication).
        log.warning(
            "%s: %d expected candles still missing after fetch (%s..%s)",
            label,
            len(remaining),
            usable[0].ts if usable else since.isoformat(),
            expected_end.isoformat(),
        )
        return _finish(
            success=False,
            fetched=len(fetched_raw),
            stored_new=stored_new,
            duplicates=len(batch_report.duplicates),
            invalid=0,
            missing=remaining,
            error=f"{len(remaining)} expected candles unavailable from provider; gaps preserved",
        )
    log.info(
        "%s: synced %d new candles (fetched %d, total %d)",
        label,
        stored_new,
        len(fetched_raw),
        len(persisted),
    )
    _ = batch_report
    return _finish(
        success=True,
        fetched=len(fetched_raw),
        stored_new=stored_new,
        duplicates=len(batch_report.duplicates),
        invalid=0,
        missing=[],
        error=None,
        provider_exhausted=len(fetched_raw) < bounded,
    )


def bootstrap_stream(
    session: Session,
    *,
    exchange: str,
    symbol: str,
    timeframe: str,
    provider: SyncProvider,
    now: datetime | None = None,
    lookback_months: int = 12,
    bootstrap_target: int = 8760,
    page_limit: int = 1000,
    max_retries: int = 5,
) -> SyncReport:
    """Resumable bootstrap (~12 months hourly). Resumes from latest persisted."""
    return sync_stream(
        session,
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        provider=provider,
        now=now,
        kind="bootstrap",
        lookback_months=lookback_months,
        total_limit=bootstrap_target,
        page_limit=page_limit,
        max_retries=max_retries,
        bootstrap_target=bootstrap_target,
    )


def repair_stream_gaps(
    session: Session,
    *,
    exchange: str,
    symbol: str,
    timeframe: str,
    provider: SyncProvider,
    now: datetime | None = None,
    max_intervals: int = 10,
    max_candles_per_request: int = 1000,
    stale_after_seconds: int = 8100,
) -> SyncReport:
    """Re-request persisted unresolved gaps in bounded intervals (manual/auto)."""
    from driftline.market_data.clock import expected_last_completed as _expected

    ref = ensure_utc(now) if now is not None else _utcnow()
    started = _utcnow()
    stream = repo.get_or_create_stream(
        session, exchange=exchange, symbol=symbol, timeframe=timeframe
    )
    persisted = repo.load_candles(session, symbol, timeframe, limit=20000, exchange=exchange)
    if not persisted:
        return SyncReport(
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            kind="repair",
            success=False,
            fetched=0,
            stored_new=0,
            total_stored=0,
            duplicates=0,
            invalid=0,
            missing=0,
            unresolved_gaps=[],
            error="no persisted history to repair against",
            duration_ms=0,
            data_source=f"exchange:{provider.name}",
        )
    expected_end = _expected(ref, timeframe)
    missing = find_missing(
        [c.ts for c in persisted], start=persisted[0].ts, end=expected_end, timeframe=timeframe
    )
    # Exclude the forming candle window: never "repair" what isn't complete yet.
    missing = [t for t in missing if t <= expected_end]
    if not missing:
        return SyncReport(
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            kind="repair",
            success=True,
            fetched=0,
            stored_new=0,
            total_stored=len(persisted),
            duplicates=0,
            invalid=0,
            missing=0,
            unresolved_gaps=[],
            error=None,
            duration_ms=0,
            data_source=f"exchange:{provider.name}",
        )
    intervals = group_intervals(
        missing, timeframe, max_candles_per_request=max_candles_per_request
    )[: max(1, max_intervals)]
    fetched_total = 0
    stored_total = 0
    dup_total = 0
    errors: list[str] = []
    for gap in intervals:
        try:
            batch = provider.fetch_ohlcv(
                symbol,
                timeframe,
                since_ms=interval_since_ms(gap.start),
                limit=interval_limit(gap, timeframe),
            )
        except Exception as exc:
            errors.append(_short_error(exc))
            continue
        usable = drop_incomplete_last_candle(batch, timeframe, now=ref)
        usable = deduplicate(usable)
        dup_total += max(0, len(batch) - len(usable))
        # Only keep candles within the requested gap (never smuggle extras).
        wanted = set(missing)
        usable = [c for c in usable if ensure_utc(c.ts) in wanted]
        fetched_total += len(batch)
        stored_total += repo.upsert_candles(session, symbol, timeframe, usable, exchange=exchange)

    persisted_after = repo.load_candles(session, symbol, timeframe, limit=20000, exchange=exchange)
    still = find_missing(
        [c.ts for c in persisted_after],
        start=persisted_after[0].ts,
        end=expected_end,
        timeframe=timeframe,
    )
    finished = _utcnow()
    duration_ms = max(0, int((finished - started).total_seconds() * 1000))
    stream.last_attempt_at = started
    stream.last_duration_ms = duration_ms
    stream.last_completed_ts = repo.latest_candle_ts(session, symbol, timeframe, exchange=exchange)
    stream.unresolved_gaps = [t.isoformat() for t in still[:MAX_STORED_GAPS]]
    stream.updated_at = finished
    if still:
        stream.last_error = (
            "; ".join(errors)[:400]
            if errors
            else f"{len(still)} gaps unresolved; preserved without fabrication"
        )[:512]
        stream.consecutive_failures = (stream.consecutive_failures or 0) + 1
    else:
        stream.last_success_at = finished
        stream.last_error = None
        stream.consecutive_failures = 0
    from driftline.market_data.quality import compute_quality

    stream.status = compute_quality(
        session, stream, now=ref, stale_after_seconds=stale_after_seconds
    ).status
    session.commit()
    repo.record_sync_run(
        session,
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        kind="repair",
        started_at=started,
        finished_at=finished,
        success=not still,
        fetched=fetched_total,
        stored_new=stored_total,
        duplicates=dup_total,
        invalid=0,
        missing=len(still),
        error="; ".join(errors)[:512]
        if still and errors
        else (None if not still else f"{len(still)} gaps unresolved"),
        duration_ms=duration_ms,
        data_source=f"exchange:{provider.name}",
    )
    status = getattr(stream, "status", StreamStatus.DEGRADED)
    _ = status
    return SyncReport(
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        kind="repair",
        success=not still,
        fetched=fetched_total,
        stored_new=stored_total,
        total_stored=len(persisted_after),
        duplicates=dup_total,
        invalid=0,
        missing=len(still),
        unresolved_gaps=still,
        error=None if not still else f"{len(still)} gaps unresolved; preserved",
        duration_ms=duration_ms,
        data_source=f"exchange:{provider.name}",
    )
