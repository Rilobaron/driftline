"""Paper-trading worker: exactly one executor per session (simulated funds only).

Runs as a distinct process (``python -m driftline.paper.worker`` or the
``paper`` Compose service) — never inside FastAPI request handling.
Coordination is database-backed (per-session leases); no Redis/Kafka.
"""

from __future__ import annotations

import contextlib
import signal
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from driftline.core.config import get_settings
from driftline.core.logging import configure_logging, get_logger
from driftline.market_data.clock import ensure_utc
from driftline.paper import store
from driftline.paper.models import PaperSessionRow, PaperSessionStatus
from driftline.paper.service import (
    PaperServiceConfig,
    evaluate_session,
    service_config_from_settings,
)
from driftline.persistence import database as db

log = get_logger("paper.worker")


@dataclass(frozen=True)
class PaperWorkerConfig:
    poll_interval_seconds: int = 60
    lease_seconds: int = 600
    owner_id: str = ""
    failure_cooldown_cap_seconds: int = 3600

    def owner(self) -> str:
        if self.owner_id:
            return self.owner_id
        return f"{socket.gethostname()}:{__import__('os').getpid()}"


SessionFactory = Callable[[], Session]


def failure_cooldown_seconds(consecutive_errors: int, *, cap: int = 3600) -> int:
    if consecutive_errors <= 0:
        return 0
    return int(min(300 * (2 ** (consecutive_errors - 1)), cap))


def session_due(row: PaperSessionRow, now: datetime, *, cooldown_cap: int = 3600) -> bool:
    """A RUNNING session is due when past its error cooldown (data decides the rest)."""
    if row.status != PaperSessionStatus.RUNNING:
        return False
    ref = ensure_utc(now)
    last = row.last_execution_at
    if last is not None:
        elapsed = (ref - ensure_utc(last)).total_seconds()
        if elapsed < 0:
            return True  # clock skew (last run stamped in the future): never block on it
        if elapsed < failure_cooldown_seconds(int(row.consecutive_errors or 0), cap=cooldown_cap):
            return False
    return True


class PaperWorker:
    """Owns periodic evaluation of all RUNNING paper sessions."""

    def __init__(
        self,
        session_factory: SessionFactory,
        config: PaperWorkerConfig,
        *,
        service_config: PaperServiceConfig | None = None,
    ) -> None:
        self._sessions = session_factory
        self.config = config
        self.service_config = service_config or PaperServiceConfig()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._local_running: set[str] = set()

    def due_sessions(self, db_session: Session, now: datetime) -> list[str]:
        rows = list(
            db_session.scalars(
                select(PaperSessionRow).where(PaperSessionRow.status == PaperSessionStatus.RUNNING)
            ).all()
        )
        return [
            r.id
            for r in rows
            if session_due(r, now, cooldown_cap=self.config.failure_cooldown_cap_seconds)
        ]

    def run_once(self, now: datetime | None = None) -> list[str]:
        """Evaluate every due session once. Returns evaluated session ids."""
        ref = ensure_utc(now) if now is not None else datetime.now(UTC)
        db_session = self._sessions()
        try:
            due_ids = self.due_sessions(db_session, ref)
        finally:
            db_session.close()

        done: list[str] = []
        for session_id in due_ids:
            db_session = self._sessions()
            try:
                row = store.lock_session_row(db_session, session_id)
                if row is None or row.status != PaperSessionStatus.RUNNING:
                    continue
                if session_id in self._local_running:
                    log.info("paper session %s: skipped (already running here)", session_id)
                    continue
                if not store.acquire_paper_lock(
                    db_session,
                    row,
                    owner=self.config.owner(),
                    lease_seconds=self.config.lease_seconds,
                    now=ref,
                ):
                    log.info("paper session %s: skipped (locked by another worker)", session_id)
                    continue
                self._local_running.add(session_id)
                try:
                    report = evaluate_session(
                        db_session, session_id, now=ref, service_config=self.service_config
                    )
                    done.append(session_id)
                    if report.error:
                        log.warning("paper session %s: %s", session_id, report.error[:200])
                    elif report.skipped_reason:
                        log.info("paper session %s: %s", session_id, report.skipped_reason)
                    else:
                        log.info(
                            "paper session %s: %d candles, %d fills",
                            session_id,
                            report.evaluated_candles,
                            report.fills,
                        )
                except Exception as exc:
                    log.warning("paper session %s worker error: %s", session_id, str(exc)[:300])
                finally:
                    self._local_running.discard(session_id)
                    try:
                        fresh = store.lock_session_row(db_session, session_id)
                        if fresh is not None:
                            store.release_paper_lock(db_session, fresh, owner=self.config.owner())
                    except Exception:
                        pass
            finally:
                db_session.close()
        return done

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="driftline-paper", daemon=True)
        self._thread.start()
        log.info("paper worker started (owner=%s)", self.config.owner())

    def _loop(self) -> None:
        try:
            self.run_once()
        except Exception as exc:
            log.warning("paper startup recovery failed: %s", str(exc)[:300])
        while not self._stop.wait(self.config.poll_interval_seconds):
            try:
                self.run_once()
            except Exception as exc:
                log.warning("paper run failed: %s", str(exc)[:300])

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        log.info("paper worker stopped (graceful)")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


def main() -> int:
    from driftline.worker import wait_for_db

    settings = get_settings()
    configure_logging(settings.log_level)
    log.info("driftline paper worker starting (db ready check)")
    wait_for_db(settings.database_url)

    from sqlalchemy.orm import sessionmaker

    engine = db.get_engine(settings.database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    worker = PaperWorker(
        factory,
        PaperWorkerConfig(poll_interval_seconds=settings.paper_interval_seconds),
        service_config=service_config_from_settings(
            settings,
            history_limit=500,
            max_new_candles=settings.paper_max_new_candles,
        ),
    )
    stop_event = threading.Event()

    def _handle(signum: int, _frame: object) -> None:
        log.info("paper worker received signal %d; shutting down", signum)
        stop_event.set()
        worker.stop()

    for _sig_name in ("SIGTERM", "SIGHUP", "SIGINT"):
        _sig = getattr(signal, _sig_name, None)
        if _sig is None:
            continue
        with contextlib.suppress(ValueError, OSError, RuntimeError):
            signal.signal(_sig, _handle)

    worker.start()
    log.info("paper worker running; press Ctrl+C to stop")
    while not stop_event.is_set():
        time.sleep(1.0)
        if not worker.running:
            log.warning("paper scheduler thread exited; stopping worker")
            break
    worker.stop()
    db.reset_engine()
    log.info("paper worker stopped cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
