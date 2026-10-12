"""Dedicated market-data worker: owns the sync scheduler (single owner).

Run locally with::

    uv run python -m driftline.worker

or via Docker Compose (``worker`` service). The FastAPI process never starts
the scheduler — ``DRIFTLINE_SCHEDULER_ENABLED`` gates ownership, and per-stream
DB leases protect against duplicate jobs if a second worker ever starts.

The worker tolerates database startup delays (retries), recovers missed
candles on boot, then follows UTC candle boundaries. SIGTERM/SIGINT shut it
down gracefully.
"""

from __future__ import annotations

import contextlib
import signal
import sys
import threading
import time

from driftline.core.config import get_settings
from driftline.core.logging import configure_logging, get_logger
from driftline.market_data.ccxt_provider import CcxtMarketDataProvider
from driftline.market_data.scheduler import MarketScheduler, SchedulerConfig
from driftline.market_data.streams import MarketStream, parse_market_streams
from driftline.persistence import database as db

log = get_logger("worker")


def wait_for_db(url: str, *, attempts: int = 30, pause_seconds: float = 2.0) -> None:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            db.reset_engine()
            db.init_db(url)
            return
        except Exception as exc:
            last = exc
            log.warning("database not ready (attempt %d/%d): %s", attempt, attempts, exc)
            time.sleep(pause_seconds)
    assert last is not None
    raise last


def build_streams() -> list[MarketStream]:
    settings = get_settings()
    streams = parse_market_streams(settings.market_streams)
    if streams:
        return streams
    # Preserve v0.1 single-market behaviour when the list is empty.
    return [
        MarketStream(
            exchange=settings.exchange,
            symbol=settings.symbol,
            timeframe=settings.timeframe,
            enabled=True,
        )
    ]


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    log.info("driftline worker starting (db ready check)")
    wait_for_db(settings.database_url)

    streams = build_streams()
    log.info("configured streams: %s", ", ".join(s.key for s in streams))

    from sqlalchemy.orm import sessionmaker

    engine = db.get_engine(settings.database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def provider_factory(exchange: str) -> CcxtMarketDataProvider:
        return CcxtMarketDataProvider(
            exchange,
            timeout_seconds=settings.fetch_timeout_seconds,
            max_retries=settings.fetch_max_retries,
        )

    config = SchedulerConfig(
        sync_delay_seconds=settings.sync_delay_seconds,
        poll_interval_seconds=settings.scheduler_interval_seconds,
        lookback_months=settings.bootstrap_lookback_months,
        bootstrap_target=max(1, settings.bootstrap_lookback_months * 744),
        stale_after_seconds=settings.stale_after_seconds,
        max_retries=settings.fetch_max_retries,
    )
    scheduler = MarketScheduler(
        factory,
        provider_factory,
        config,
        ensure_streams=streams,
    )
    stop_event = threading.Event()

    def _handle(signum: int, _frame: object) -> None:
        log.info("worker received signal %d; shutting down", signum)
        stop_event.set()
        scheduler.stop()

    for _sig_name in ("SIGTERM", "SIGHUP", "SIGINT"):
        _sig = getattr(signal, _sig_name, None)
        if _sig is None:
            continue
        if _sig_name != "SIGINT" and sys.platform == "win32" and _sig_name == "SIGHUP":
            continue
        with contextlib.suppress(ValueError, OSError, RuntimeError):
            signal.signal(_sig, _handle)

    scheduler.start()
    log.info("worker running; press Ctrl+C to stop")
    while not stop_event.is_set():
        time.sleep(1.0)
        if not scheduler.running:
            log.warning("scheduler thread exited; stopping worker")
            break
    scheduler.stop()
    db.reset_engine()
    log.info("worker stopped cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
