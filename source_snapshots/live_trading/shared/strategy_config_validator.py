"""Machine-checkable contract for live strategy ``config.py`` modules.

The registry remains the single source of identity.  This validator checks
that a config is a proxy for that identity and that every universal field
consumed outside of ``strategies/<id>/strategy.py`` is well-typed before a
runner can create a broker connection.
"""

from __future__ import annotations

import argparse
import importlib
import math
import re
import sys
from collections.abc import Mapping
from types import ModuleType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator, model_validator

from shared.registry import StrategyRegistry, registry as default_registry


STOP_LOSS_MODELS = frozenset({"CUSTOM", "ATR_MULTIPLIER", "FIXED_PRICE", "POINTS", "PERCENT"})
TAKE_PROFIT_MODELS = frozenset({"CUSTOM", "FIXED_PRICE", "POINTS", "PERCENT", "R_MULTIPLE", "NONE"})
# These are the only two models implemented by core.position_manager.
BREAK_EVEN_MODELS = frozenset({"FIXED_PRICE", "R_MULTIPLE"})

_PUBLIC_FIELD_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_KNOWN_FIELDS = frozenset(
    {
        "STRATEGY_NAME",
        "SYMBOL",
        "ASSET_CLASS",
        "MAGIC",
        "ACCOUNT",
        "SIGNAL_TIMEFRAME",
        "RISK_PER_TRADE_USD",
        "DAILY_SL_LIMIT_USD",
        "WEEKLY_SL_LIMIT_USD",
        "STOP_LOSS_MODEL",
        "STOP_LOSS",
        "TAKE_PROFIT_MODEL",
        "TAKE_PROFIT",
        "USE_BREAK_EVEN",
        "BREAK_EVEN_MODEL",
        "BREAK_EVEN_TRIGGER",
        "BREAK_EVEN_OFFSET",
        "MAX_SLIPPAGE_AS_STOP_FRACTION",
        "MAX_SPREAD_POINTS",
        "USE_PORTFOLIO_ENTRY_SESSION_GUARD",
        "USE_PORTFOLIO_FRIDAY_FORCE_CLOSE",
        "STRATEGY_MANAGED_EXIT_FORCE_CLOSE",
    }
)


class StrategyConfigValidationError(Exception):
    """Raised when a strategy config violates the live config contract."""


class StrategyConfigContract(BaseModel):
    """Universal fields available to the runner, core, guards, and risk.

    Extra UPPER_SNAKE_CASE fields are intentionally permitted: they belong to
    the individual strategy only and must never be read by core modules.
    """

    model_config = ConfigDict(extra="allow", strict=True)

    STRATEGY_NAME: str = Field(min_length=1)
    SYMBOL: str = Field(min_length=1)
    ASSET_CLASS: str = Field(min_length=1)
    MAGIC: int = Field(gt=0)
    ACCOUNT: str = Field(min_length=1)
    SIGNAL_TIMEFRAME: str = Field(min_length=1)

    RISK_PER_TRADE_USD: float = Field(gt=0)
    DAILY_SL_LIMIT_USD: float | None = Field(default=None, gt=0)
    WEEKLY_SL_LIMIT_USD: float | None = Field(default=None, gt=0)

    STOP_LOSS_MODEL: Literal["CUSTOM", "ATR_MULTIPLIER", "FIXED_PRICE", "POINTS", "PERCENT"]
    STOP_LOSS: float | None = None
    TAKE_PROFIT_MODEL: Literal["CUSTOM", "FIXED_PRICE", "POINTS", "PERCENT", "R_MULTIPLE", "NONE"]
    TAKE_PROFIT: float | None = None

    USE_BREAK_EVEN: StrictBool
    BREAK_EVEN_MODEL: Literal["FIXED_PRICE", "R_MULTIPLE"]
    BREAK_EVEN_TRIGGER: float = Field(ge=0)
    BREAK_EVEN_OFFSET: float = Field(ge=0)

    MAX_SLIPPAGE_AS_STOP_FRACTION: float = Field(gt=0, le=1)
    MAX_SPREAD_POINTS: float | None = Field(default=None, gt=0)
    USE_PORTFOLIO_ENTRY_SESSION_GUARD: StrictBool = True
    USE_PORTFOLIO_FRIDAY_FORCE_CLOSE: StrictBool = True
    STRATEGY_MANAGED_EXIT_FORCE_CLOSE: StrictBool = False

    @field_validator(
        "RISK_PER_TRADE_USD",
        "DAILY_SL_LIMIT_USD",
        "WEEKLY_SL_LIMIT_USD",
        "STOP_LOSS",
        "TAKE_PROFIT",
        "BREAK_EVEN_TRIGGER",
        "BREAK_EVEN_OFFSET",
        "MAX_SLIPPAGE_AS_STOP_FRACTION",
        "MAX_SPREAD_POINTS",
    )
    @classmethod
    def finite_number(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("must be finite")
        return value

    @model_validator(mode="after")
    def enabled_break_even_needs_positive_trigger(self) -> "StrategyConfigContract":
        if self.USE_BREAK_EVEN and self.BREAK_EVEN_TRIGGER <= 0:
            raise ValueError("BREAK_EVEN_TRIGGER must be > 0 when USE_BREAK_EVEN is true")
        return self

    @model_validator(mode="after")
    def stop_loss_matches_model(self) -> "StrategyConfigContract":
        if self.STOP_LOSS_MODEL == "CUSTOM":
            if self.STOP_LOSS is not None:
                raise ValueError("STOP_LOSS must be None when STOP_LOSS_MODEL is CUSTOM")
        elif self.STOP_LOSS is None or self.STOP_LOSS <= 0:
            raise ValueError("STOP_LOSS must be > 0 unless STOP_LOSS_MODEL is CUSTOM")
        return self

    @model_validator(mode="after")
    def take_profit_matches_model(self) -> "StrategyConfigContract":
        if self.TAKE_PROFIT_MODEL == "NONE":
            if self.TAKE_PROFIT is not None:
                raise ValueError("TAKE_PROFIT must be None when TAKE_PROFIT_MODEL is NONE")
        elif self.TAKE_PROFIT is None or self.TAKE_PROFIT <= 0:
            raise ValueError("TAKE_PROFIT must be > 0 unless TAKE_PROFIT_MODEL is NONE")
        return self


def _module_values(config: ModuleType | Mapping[str, Any] | Any) -> dict[str, Any]:
    """Extract public config values without treating private helpers as data."""
    if isinstance(config, Mapping):
        values = dict(config)
    else:
        values = dict(vars(config))
    return {
        name: value
        for name, value in values.items()
        if not name.startswith("__") and not callable(value)
    }


def validate_strategy_config(
    strategy_id: str,
    config: ModuleType | Mapping[str, Any] | Any,
    *,
    registry: StrategyRegistry = default_registry,
) -> StrategyConfigContract:
    """Validate one imported config and its registry-backed identity proxy."""
    values = _module_values(config)
    invalid_public_names = sorted(
        name for name in values if not name.startswith("_") and not _PUBLIC_FIELD_RE.fullmatch(name)
    )
    if invalid_public_names:
        raise StrategyConfigValidationError(
            f"strategy {strategy_id!r}: strategy-specific fields must use "
            f"UPPER_SNAKE_CASE; invalid: {', '.join(invalid_public_names)}"
        )

    try:
        contract = StrategyConfigContract.model_validate(
            {name: value for name, value in values.items() if not name.startswith("_")}
        )
    except ValidationError as exc:
        raise StrategyConfigValidationError(
            f"strategy {strategy_id!r}: invalid strategy config: "
            f"{exc.errors(include_url=False)}"
        ) from exc

    try:
        identity = registry.get_strategy(strategy_id)
    except KeyError as exc:
        raise StrategyConfigValidationError(
            f"strategy {strategy_id!r}: missing from the strategy registry"
        ) from exc

    mismatches: list[str] = []
    for config_field, registry_field in (
        ("SYMBOL", "symbol"),
        ("ASSET_CLASS", "asset_class"),
        ("MAGIC", "magic"),
        ("ACCOUNT", "account"),
    ):
        if getattr(contract, config_field) != identity[registry_field]:
            mismatches.append(
                f"{config_field}={getattr(contract, config_field)!r} != "
                f"registry.{registry_field}={identity[registry_field]!r}"
            )
    expected_name = strategy_id.upper()
    if contract.STRATEGY_NAME != expected_name:
        mismatches.append(
            f"STRATEGY_NAME={contract.STRATEGY_NAME!r} != "
            f"canonical {expected_name!r}"
        )
    if mismatches:
        raise StrategyConfigValidationError(
            f"strategy {strategy_id!r}: identity proxy mismatch: "
            + "; ".join(mismatches)
        )
    return contract


def import_and_validate_strategy_config(
    strategy_id: str,
    *,
    registry: StrategyRegistry = default_registry,
) -> ModuleType:
    """Import and validate one config before strategy logic or broker setup."""
    try:
        config = importlib.import_module(f"strategies.{strategy_id}.config")
    except (ImportError, SyntaxError) as exc:
        raise StrategyConfigValidationError(
            f"strategy {strategy_id!r}: cannot import config.py: {exc}"
        ) from exc
    validate_strategy_config(strategy_id, config, registry=registry)
    return config


def validate_all_strategy_configs(
    *, registry: StrategyRegistry = default_registry,
) -> tuple[str, ...]:
    """Validate every registry config; useful in CI and release checks."""
    validated: list[str] = []
    errors: list[str] = []
    for strategy_id in registry.list_strategies(enabled_only=False):
        try:
            import_and_validate_strategy_config(strategy_id, registry=registry)
        except StrategyConfigValidationError as exc:
            errors.append(str(exc))
        else:
            validated.append(strategy_id)
    if errors:
        raise StrategyConfigValidationError("\n".join(f"- {error}" for error in errors))
    return tuple(validated)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate all live strategy config contracts.")
    parser.parse_args(argv)
    try:
        strategy_ids = validate_all_strategy_configs()
    except StrategyConfigValidationError as exc:
        print("strategy configuration invalid", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 1
    print("strategy configuration valid")
    print(f"strategies: {', '.join(strategy_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
