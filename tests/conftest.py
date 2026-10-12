"""Shared deterministic fixtures and app overrides for tests (no network)."""

from __future__ import annotations

import os
from collections.abc import Generator

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_driftline.db")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from driftline.api import main as api_main
from driftline.market_data.fixtures import (
    make_oscillating_candles,
    make_trend_candles,
)
from driftline.persistence.models import Base


@pytest.fixture()
def candles_trend() -> list:  # type: ignore[type-arg]
    return make_trend_candles(n=120)


@pytest.fixture()
def candles_wave() -> list:  # type: ignore[type-arg]
    return make_oscillating_candles(n=200)


@pytest.fixture()
def db_session(tmp_path: object) -> Generator[Session, None, None]:
    from pathlib import Path

    db_file = Path(str(tmp_path)) / "test_driftline.db"
    engine = create_engine(
        f"sqlite:///{db_file.as_posix()}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db_session: Session) -> Generator[TestClient, None, None]:
    def _override() -> Generator[Session, None, None]:
        yield db_session

    api_main.app.dependency_overrides[api_main.get_db] = _override
    with TestClient(api_main.app) as c:
        yield c
    api_main.app.dependency_overrides.clear()
