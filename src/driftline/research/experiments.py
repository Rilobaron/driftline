"""Reproducible experiment records (lightweight research log)."""

from __future__ import annotations

import subprocess
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from driftline import __version__

Methodology = Literal["backtest", "compare", "split", "walk_forward"]


class ExperimentRecord(BaseModel):
    """Everything needed to reproduce a research run on a matching dataset."""

    experiment_id: str
    created_at: datetime
    methodology: Methodology
    strategy_id: str
    strategy_version: str = "1.0.0"
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    evaluation: dict[str, Any] = Field(default_factory=dict)
    symbol: str = ""
    timeframe: str = "1h"
    data_provider: str = ""
    data_start: datetime | None = None
    data_end: datetime | None = None
    num_candles: int = 0
    fingerprint: str = ""
    backtest_config: dict[str, Any] = Field(default_factory=dict)
    risk_config: dict[str, Any] | None = None
    seed: int | None = None
    code_version: str = __version__
    git_commit: str | None = None
    results: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        sha = out.stdout.strip()
        return sha or None
    except Exception:
        return None


def build_experiment(
    *,
    methodology: Methodology,
    strategy_id: str,
    strategy_version: str = "1.0.0",
    strategy_params: dict[str, Any] | None = None,
    evaluation: dict[str, Any] | None = None,
    symbol: str = "",
    timeframe: str = "1h",
    data_provider: str = "",
    data_start: datetime | None = None,
    data_end: datetime | None = None,
    num_candles: int = 0,
    fingerprint: str = "",
    backtest_config: dict[str, Any] | None = None,
    risk_config: dict[str, Any] | None = None,
    seed: int | None = None,
    results: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> ExperimentRecord:
    return ExperimentRecord(
        experiment_id=uuid.uuid4().hex[:12],
        created_at=datetime.now(UTC),
        methodology=methodology,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        strategy_params=dict(strategy_params or {}),
        evaluation=dict(evaluation or {}),
        symbol=symbol,
        timeframe=timeframe,
        data_provider=data_provider,
        data_start=data_start,
        data_end=data_end,
        num_candles=num_candles,
        fingerprint=fingerprint,
        backtest_config=dict(backtest_config or {}),
        risk_config=risk_config,
        seed=seed,
        code_version=__version__,
        git_commit=_git_commit(),
        results=dict(results or {}),
        warnings=list(warnings or []),
    )
