"""Provider failure classification and bounded retry with backoff + jitter.

Permanent errors (unsupported symbol/timeframe, geo-restriction wording that
is clearly an access denial, bad requests) are never retried indefinitely.
Recoverable errors (timeouts, network blips, rate limits, HTTP 5xx,
exchange maintenance) use bounded exponential backoff with jitter.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from driftline.core.exceptions import (
    InaccessibleExchangeError,
    MarketDataError,
    ValidationError,
)
from driftline.core.logging import get_logger

log = get_logger("market_data.resilience")

_PERMANENT_HINTS = (
    "bad symbol",
    "unknown symbol",
    "invalid symbol",
    "market not found",
    "no market",
    "unsupported",
    "invalid timeframe",
    "bad request",
    "does not support",
)
# Geo-restriction / access denials are permanent for this deployment: retrying
# will not help, so surface them immediately with a provider hint.
_GEO_HINTS = ("restricted", "451", "geographic", "geo-block", "unavailable for", "forbidden")
_RETRYABLE_HINTS = (
    "timeout",
    "timed out",
    "network",
    "connection",
    "reset by peer",
    "temporar",
    "rate limit",
    "rate-limit",
    "ratelimit",
    "429",
    "too many requests",
    "500",
    "502",
    "503",
    "504",
    "overload",
    "maintenance",
    "unavailable",
    "exchange not available",
    "request failed",
)


@dataclass(frozen=True)
class RetryConfig:
    max_retries: int = 5
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")


def classify_error(exc: Exception) -> str:
    """Return ``'permanent'``, ``'geo_blocked'`` or ``'retryable'``."""
    message = str(exc).lower()
    if isinstance(exc, InaccessibleExchangeError):
        return "geo_blocked"
    if isinstance(exc, ValidationError):
        return "permanent"
    if any(h in message for h in _GEO_HINTS):
        return "geo_blocked"
    if any(h in message for h in _PERMANENT_HINTS):
        return "permanent"
    if isinstance(exc, MarketDataError):
        # Unknown market-data failures default to retryable-but-bounded so
        # transient exchange issues recover; permanent-looking text above wins.
        if any(h in message for h in _RETRYABLE_HINTS):
            return "retryable"
        return "retryable"
    return "retryable"


def backoff_delay(attempt: int, config: RetryConfig) -> float:
    """Exponential backoff with ±25% jitter, capped at max_delay."""
    raw = min(config.base_delay_seconds * (2.0**attempt), config.max_delay_seconds)
    jitter = raw * 0.25 * (random.random() * 2.0 - 1.0)
    return max(0.0, raw + jitter)


_T = TypeVar("_T")


def run_with_retry[T](
    operation: Callable[[], T],
    config: RetryConfig,
    *,
    stream_label: str = "",
) -> T:
    """Call zero-arg ``operation`` with bounded retries.

    Raises the last error after ``max_retries`` retries. Permanent and
    geo-blocked errors are raised immediately without retry.
    """
    last: Exception | None = None
    for attempt in range(config.max_retries + 1):
        try:
            return operation()
        except Exception as exc:
            kind = classify_error(exc)
            last = exc
            if kind in ("permanent", "geo_blocked") or attempt >= config.max_retries:
                raise
            delay = backoff_delay(attempt, config)
            # Never log credentials: only the stream label + error text.
            log.warning(
                "provider call failed for %s (attempt %d/%d, %s): %s; retrying in %.1fs",
                stream_label or "stream",
                attempt + 1,
                config.max_retries + 1,
                kind,
                exc,
                delay,
            )
            time.sleep(delay)
    assert last is not None
    raise last
