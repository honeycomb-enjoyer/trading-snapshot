"""Instrument-aware execution costs converted to R at trade level."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time as clock_time
from typing import Any, Mapping

import pandas as pd


REQUIRED_PROFILE_FIELDS = {
    "price_unit",
    "spread_units",
    "slippage_units_per_side",
    "commission_units_round_turn",
    "swap_long_units_per_roll",
    "swap_short_units_per_roll",
}


def _clock(value: Any) -> clock_time:
    try:
        return clock_time.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("rollover_time must use HH:MM or HH:MM:SS") from exc


@dataclass(frozen=True)
class InstrumentCostModel:
    symbol: str
    profile_name: str
    unit_name: str
    price_unit: float
    spread_units: float
    slippage_units_per_side: float
    commission_units_round_turn: float
    swap_long_units_per_roll: float
    swap_short_units_per_roll: float
    rollover_timezone: str
    rollover_time: clock_time
    triple_swap_weekday: int

    @classmethod
    def from_config(
        cls,
        symbol: str,
        profile: Mapping[str, Any],
        model_config: Mapping[str, Any],
    ) -> "InstrumentCostModel":
        missing = REQUIRED_PROFILE_FIELDS.difference(profile)
        if missing:
            raise ValueError(
                f"Execution cost profile {symbol} is missing: {sorted(missing)}"
            )
        numeric = {name: float(profile[name]) for name in REQUIRED_PROFILE_FIELDS}
        if numeric["price_unit"] <= 0:
            raise ValueError("Execution cost price_unit must be positive")
        for name, value in numeric.items():
            if name != "price_unit" and name not in {
                "swap_long_units_per_roll", "swap_short_units_per_roll",
            } and value < 0:
                raise ValueError(f"Execution cost {name} cannot be negative")
        triple_weekday = int(model_config.get("triple_swap_weekday", 2))
        if triple_weekday not in range(7):
            raise ValueError("triple_swap_weekday must be between 0 and 6")
        return cls(
            symbol=symbol,
            profile_name=str(profile.get("name", symbol)),
            unit_name=str(profile.get("unit_name", "price unit")),
            rollover_timezone=str(
                model_config.get("rollover_timezone", "America/New_York")
            ),
            rollover_time=_clock(model_config.get("rollover_time", "17:00")),
            triple_swap_weekday=triple_weekday,
            **numeric,
        )

    def spread(self, _context) -> float:
        return self.spread_units * self.price_unit

    def slippage(self, _context) -> float:
        return self.slippage_units_per_side * self.price_unit

    def commission(self, context) -> float:
        return self.commission_units_round_turn * self.price_unit / context.position["risk"]

    def rollover_units(self, context) -> int:
        opened = pd.Timestamp(context.position["open_time"])
        closed = pd.Timestamp(context.timestamp)
        if opened.tzinfo is None:
            opened = opened.tz_localize("UTC")
        else:
            opened = opened.tz_convert("UTC")
        if closed.tzinfo is None:
            closed = closed.tz_localize("UTC")
        else:
            closed = closed.tz_convert("UTC")
        opened = opened.tz_convert(self.rollover_timezone)
        closed = closed.tz_convert(self.rollover_timezone)
        days = pd.date_range(opened.normalize(), closed.normalize(), freq="D")
        total = 0
        offset = pd.Timedelta(
            hours=self.rollover_time.hour,
            minutes=self.rollover_time.minute,
            seconds=self.rollover_time.second,
        )
        for day in days:
            rollover = day + offset
            if opened < rollover <= closed and day.weekday() <= 4:
                total += 3 if day.weekday() == self.triple_swap_weekday else 1
        return total

    def swap(self, context) -> float:
        units_per_roll = (
            self.swap_long_units_per_roll
            if context.side == "BUY"
            else self.swap_short_units_per_roll
        )
        return (
            units_per_roll * self.rollover_units(context) * self.price_unit
            / context.position["risk"]
        )

    def hooks(self) -> dict[str, Any]:
        return {
            "spread": self.spread,
            "slippage": self.slippage,
            "commission": self.commission,
            "swap": self.swap,
        }

    def metadata(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "symbol": self.symbol,
            "profile": self.profile_name,
            "unit_name": self.unit_name,
            "price_unit": self.price_unit,
            "rollover_timezone": self.rollover_timezone,
            "rollover_time": self.rollover_time.isoformat(timespec="minutes"),
            "triple_swap_weekday": self.triple_swap_weekday,
        }


def resolve_execution_costs(
    model_config: Mapping[str, Any] | None,
    symbol: str | None,
    explicit_costs: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], InstrumentCostModel | None]:
    """Select the dataset symbol profile and merge optional explicit hooks."""
    costs: dict[str, Any] = {}
    model = None
    config = dict(model_config or {})
    if config.get("enabled", False):
        if not symbol:
            raise ValueError(
                "Enabled execution_cost_model requires dataset manifest symbol"
            )
        profiles = config.get("profiles", {})
        normalized_profiles = {str(key).upper(): value for key, value in profiles.items()}
        symbol_key = str(symbol).upper()
        if symbol_key not in normalized_profiles:
            raise ValueError(
                f"No execution cost profile configured for symbol {symbol_key}"
            )
        model = InstrumentCostModel.from_config(
            symbol_key, normalized_profiles[symbol_key], config,
        )
        costs.update(model.hooks())
    costs.update(dict(explicit_costs or {}))
    return costs, model
