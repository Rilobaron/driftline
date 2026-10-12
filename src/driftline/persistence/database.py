"""SQLAlchemy engine/session management (PostgreSQL primary, SQLite fallback)."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from driftline.core.config import get_settings
from driftline.persistence.models import Base

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine(url: str | None = None) -> Engine:
    global _engine, _session_factory
    db_url = url or get_settings().database_url
    if _engine is None:
        connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
        _engine = create_engine(db_url, pool_pre_ping=True, connect_args=connect_args)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def init_db(url: str | None = None) -> None:
    Base.metadata.create_all(get_engine(url))


def get_session(url: str | None = None) -> Generator[Session, None, None]:
    get_engine(url)
    assert _session_factory is not None
    session = _session_factory()
    try:
        yield session
    finally:
        session.close()
