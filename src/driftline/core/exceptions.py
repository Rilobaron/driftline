"""Shared core exceptions with structured error codes."""

from __future__ import annotations


class DriftlineError(Exception):
    """Base error carrying a machine-readable code."""

    code = "driftline_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class MarketDataError(DriftlineError):
    code = "market_data_error"


class InaccessibleExchangeError(MarketDataError):
    code = "exchange_inaccessible"


class ValidationError(DriftlineError):
    code = "validation_error"


class RiskRejectionError(DriftlineError):
    code = "risk_rejected"


class InsufficientFundsError(DriftlineError):
    code = "insufficient_funds"


class BacktestError(DriftlineError):
    code = "backtest_error"


class NotFoundError(DriftlineError):
    code = "not_found"
