"""Strategy metadata: parameter schemas and strategy specifications."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ParameterSpec(BaseModel):
    """Explicit, validated parameter declaration for a strategy."""

    name: str
    kind: Literal["int", "float"] = "int"
    default: float | int
    min_value: float | None = None
    max_value: float | None = None
    description: str = ""


class StrategySpec(BaseModel):
    """Serializable strategy descriptor for registry listings and the API."""

    strategy_id: str
    name: str
    description: str
    version: str = "1.0.0"
    warmup_bars: int = Field(ge=0)
    required_indicators: list[str] = Field(default_factory=list)
    default_params: dict[str, Any] = Field(default_factory=dict)
    parameter_schema: list[ParameterSpec] = Field(default_factory=list)


def coerce_and_check(spec: ParameterSpec, raw: Any) -> int | float:
    """Coerce a user-supplied value to the spec kind and range-check it."""
    if isinstance(raw, bool):
        raise ValueError(f"parameter {spec.name!r} must be numeric, got boolean")
    if spec.kind == "int":
        if isinstance(raw, float):
            if not raw.is_integer():
                raise ValueError(f"parameter {spec.name!r} must be an integer, got {raw!r}")
            raw = int(raw)
        elif isinstance(raw, str):
            try:
                raw = int(raw)
            except ValueError as exc:
                raise ValueError(
                    f"parameter {spec.name!r} must be an integer, got {raw!r}"
                ) from exc
        elif not isinstance(raw, int):
            raise ValueError(f"parameter {spec.name!r} must be an integer, got {raw!r}")
    else:
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise ValueError(f"parameter {spec.name!r} must be numeric, got {raw!r}")
        try:
            raw = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"parameter {spec.name!r} must be numeric, got {raw!r}") from exc
    value: int | float = raw
    if spec.min_value is not None and value < spec.min_value:
        raise ValueError(f"parameter {spec.name!r}={value} below minimum {spec.min_value}")
    if spec.max_value is not None and value > spec.max_value:
        raise ValueError(f"parameter {spec.name!r}={value} above maximum {spec.max_value}")
    return value


def validate_against_schema(
    schema: list[ParameterSpec], params: dict[str, Any] | None
) -> dict[str, Any]:
    """Fill defaults and reject unknown/out-of-range parameters."""
    supplied = dict(params or {})
    known = {s.name: s for s in schema}
    unknown = sorted(set(supplied) - set(known))
    if unknown:
        raise ValueError(f"unknown parameters: {', '.join(unknown)}")
    out: dict[str, Any] = {}
    for spec in schema:
        raw = supplied.get(spec.name, spec.default)
        out[spec.name] = coerce_and_check(spec, raw)
    return out
