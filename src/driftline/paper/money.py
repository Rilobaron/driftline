"""Precision-safe monetary arithmetic for paper trading.

All persisted balances, quantities, prices, fees and PnL use
:class:`~decimal.Decimal` with explicit quantization. Floats are accepted at
API boundaries and converted immediately — never accumulated.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, InvalidOperation

CASH_PLACES = Decimal("0.00000001")  # 8dp for USDT balances/fees
QTY_PLACES = Decimal("0.00000001")  # 8dp for BTC quantities
PRICE_PLACES = Decimal("0.00000001")  # 8dp for prices


def D(value: object) -> Decimal:
    """Convert ``value`` to Decimal without inheriting binary float error."""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid monetary value: {value!r}") from exc


def quantize_cash(value: Decimal) -> Decimal:
    return value.quantize(CASH_PLACES)


def quantize_qty(value: Decimal, *, precision: int = 8) -> Decimal:
    if precision < 0 or precision > 18:
        raise ValueError("qty precision must be within [0, 18]")
    step = Decimal(1).scaleb(-precision)
    return value.quantize(step, rounding=ROUND_DOWN)


def quantize_price(value: Decimal, *, precision: int = 8) -> Decimal:
    if precision < 0 or precision > 18:
        raise ValueError("price precision must be within [0, 18]")
    step = Decimal(1).scaleb(-precision)
    return value.quantize(step)
