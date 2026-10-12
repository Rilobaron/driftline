"""Central strategy registry: discovery, validation, instantiation."""

from __future__ import annotations

from typing import Any

from driftline.strategies.base import Strategy
from driftline.strategies.donchian_breakout import DonchianBreakoutStrategy
from driftline.strategies.ema_crossover import EmaCrossoverStrategy
from driftline.strategies.rsi_mean_reversion import RsiMeanReversionStrategy
from driftline.strategies.specs import ParameterSpec, StrategySpec, validate_against_schema

StrategyClass = EmaCrossoverStrategy | RsiMeanReversionStrategy | DonchianBreakoutStrategy


class StrategyRegistry:
    """Registry of independent strategies with schema-validated instantiation."""

    def __init__(self) -> None:
        self._classes: dict[str, type[StrategyClass]] = {}

    def register(self, cls: type[StrategyClass]) -> type[StrategyClass]:
        strategy_id: str = cls.strategy_id
        if strategy_id in self._classes:
            raise ValueError(f"strategy already registered: {strategy_id}")
        self._classes[strategy_id] = cls
        return cls

    def ids(self) -> list[str]:
        return sorted(self._classes)

    def _require(self, strategy_id: str) -> type[StrategyClass]:
        try:
            return self._classes[strategy_id]
        except KeyError:
            known = ", ".join(self.ids())
            raise ValueError(f"unknown strategy: {strategy_id!r} (known: {known})") from None

    def spec(self, strategy_id: str, params: dict[str, Any] | None = None) -> StrategySpec:
        cls = self._require(strategy_id)
        validated = validate_against_schema(cls.parameter_schema(), params)
        built: Any = cls.build_params(validated)
        instance = cls(built)
        return StrategySpec(
            strategy_id=strategy_id,
            name=cls.name,
            description=cls.description,
            version=cls.version,
            warmup_bars=instance.warmup_bars,
            required_indicators=list(cls.required_indicators),
            default_params=cls.default_params(),
            parameter_schema=cls.parameter_schema(),
        )

    def list_specs(self) -> list[StrategySpec]:
        return [self.spec(strategy_id) for strategy_id in self.ids()]

    def validate_params(self, strategy_id: str, params: dict[str, Any] | None) -> dict[str, Any]:
        """Return normalized params (defaults filled); raise on unknown/invalid."""
        cls = self._require(strategy_id)
        validated = validate_against_schema(cls.parameter_schema(), params)
        cls.build_params(validated)  # semantic validation (ordering, bounds)
        return validated

    def create(self, strategy_id: str, params: dict[str, Any] | None = None) -> StrategyClass:
        cls = self._require(strategy_id)
        validated = validate_against_schema(cls.parameter_schema(), params)
        built: Any = cls.build_params(validated)
        return cls(built)


_REGISTRY = StrategyRegistry()
_REGISTRY.register(EmaCrossoverStrategy)
_REGISTRY.register(RsiMeanReversionStrategy)
_REGISTRY.register(DonchianBreakoutStrategy)


def default_registry() -> StrategyRegistry:
    return _REGISTRY


def list_strategies() -> list[dict[str, object]]:
    """Legacy listing (v0.1 shape) plus version for transparency."""
    out: list[dict[str, object]] = []
    for spec in _REGISTRY.list_specs():
        out.append(
            {
                "name": spec.strategy_id,
                "description": spec.description,
                "default_params": spec.default_params,
                "version": spec.version,
            }
        )
    return out


def create_strategy(name: str, params: dict[str, Any] | None = None) -> StrategyClass:
    return _REGISTRY.create(name, params)


__all__ = [
    "ParameterSpec",
    "Strategy",
    "StrategyClass",
    "StrategyRegistry",
    "StrategySpec",
    "create_strategy",
    "default_registry",
    "list_strategies",
]
