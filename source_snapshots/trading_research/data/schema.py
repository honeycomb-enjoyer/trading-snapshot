"""Canonical contract for historical OHLCV data used by research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Mapping
import re
import warnings

import pandas as pd


REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close")
DATA_KINDS = frozenset({"raw", "cleaned", "derived"})
TIMEFRAME_DELTAS = {
    "M1": timedelta(minutes=1),
    "M5": timedelta(minutes=5),
    "M15": timedelta(minutes=15),
    "M30": timedelta(minutes=30),
    "H1": timedelta(hours=1),
    "H4": timedelta(hours=4),
    "D1": timedelta(days=1),
    "W1": timedelta(weeks=1),
}
DEFAULT_CLOSURE_DATES_BY_SYMBOL = {
    "DE40": (
        "2024-05-01", "2024-12-24", "2024-12-25", "2024-12-26", "2024-12-31",
        "2025-01-01", "2025-05-01", "2025-12-24", "2025-12-25", "2025-12-26",
        "2025-12-31", "2026-01-01",
    ),
}


class DataContractError(ValueError):
    """Raised when a dataset does not satisfy the research data contract."""


class DataContractWarning(RuntimeWarning):
    """Raised for suspicious but non-fatal research-data conditions."""


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_project_path(path: str | Path) -> Path:
    """Resolve a data path relative to the repository, never the process CWD."""
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate.resolve()
    return (project_root() / candidate).resolve()


def parse_utc(value: object, *, field_name: str = "timestamp") -> pd.Timestamp:
    """Parse a timestamp into the canonical UTC-aware representation.

    Legacy naive values are explicitly interpreted as UTC. New MT5 downloads
    emit RFC3339 UTC values, so they do not depend on this compatibility rule.
    """
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise DataContractError(f"Invalid {field_name}: {value!r}") from exc
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


@dataclass(frozen=True)
class DatasetContract:
    symbol: str
    timeframe: str
    source: str
    venue: str
    timezone: str = "UTC"
    data_kind: str = "raw"
    max_non_trading_gap: timedelta = timedelta(hours=72)
    known_closure_dates: tuple[str, ...] = ()
    gap_policy: str = "warn"

    @classmethod
    def from_mapping(cls, config: Mapping[str, object]) -> "DatasetContract":
        filename = Path(str(config.get("path", ""))).name
        match = re.match(r"(?P<symbol>.+)_(?P<timeframe>M1|M5|M15|M30|H1|H4|D1|W1)_", filename, flags=re.IGNORECASE)
        inferred_symbol = match.group("symbol") if match else None
        inferred_timeframe = match.group("timeframe") if match else None
        symbol = str(config.get("symbol") or inferred_symbol or "UNKNOWN")
        timeframe = str(config.get("timeframe") or inferred_timeframe or "").upper()
        known_closures = config.get("known_closure_dates", DEFAULT_CLOSURE_DATES_BY_SYMBOL.get(symbol.upper(), ()))
        default_gap_hours = 144 if known_closures else 72
        if timeframe in TIMEFRAME_DELTAS:
            default_gap_hours = max(
                default_gap_hours,
                TIMEFRAME_DELTAS[timeframe].total_seconds() / 3600 * 2,
            )
        try:
            contract = cls(
                symbol=symbol,
                timeframe=timeframe,
                source=str(config.get("source", "legacy_csv")),
                venue=str(config.get("venue", "unknown")),
                timezone=str(config.get("timezone", "UTC")).upper(),
                data_kind=str(config.get("data_kind", "raw")).lower(),
                max_non_trading_gap=timedelta(hours=float(config.get("max_non_trading_gap_hours", default_gap_hours))),
                known_closure_dates=tuple(str(day) for day in known_closures),
                gap_policy=str(config.get("gap_policy", "warn")).lower(),
            )
        except (TypeError, ValueError) as exc:
            raise DataContractError("DATA_CONFIG contains an invalid data-contract value") from exc
        contract.validate()
        return contract

    @property
    def interval(self) -> timedelta:
        return TIMEFRAME_DELTAS[self.timeframe]

    def validate(self) -> None:
        if not self.symbol:
            raise DataContractError("Dataset symbol must be non-empty")
        if self.timeframe not in TIMEFRAME_DELTAS:
            raise DataContractError(f"Unsupported timeframe: {self.timeframe}")
        if not self.source or not self.venue:
            raise DataContractError("Dataset source and venue must be non-empty")
        if self.timezone != "UTC":
            raise DataContractError("Research timestamps must use UTC")
        if self.data_kind not in DATA_KINDS:
            raise DataContractError(f"Unsupported data_kind: {self.data_kind}")
        if self.max_non_trading_gap < self.interval:
            raise DataContractError("max_non_trading_gap_hours must be at least one timeframe interval")
        if self.gap_policy not in {"warn", "error"}:
            raise DataContractError("gap_policy must be either 'warn' or 'error'")
        for day in self.known_closure_dates:
            try:
                pd.Timestamp(day).date()
            except (TypeError, ValueError) as exc:
                raise DataContractError(f"Invalid known closure date: {day!r}") from exc


def _crosses_weekend(start: pd.Timestamp, end: pd.Timestamp) -> bool:
    # This intentionally permits normal FX/CFD weekend closures but not an
    # arbitrary long outage. Exchange-specific holiday calendars are a future
    # venue-catalog concern, not something that can be inferred from a CSV.
    return any(day.dayofweek >= 5 for day in pd.date_range(start.normalize(), end.normalize(), freq="D"))


def _is_configured_market_closure(start: pd.Timestamp, end: pd.Timestamp, contract: DatasetContract) -> bool:
    dates = pd.date_range(start.normalize() + pd.Timedelta(days=1), end.normalize() - pd.Timedelta(days=1), freq="D")
    if dates.empty:
        return False
    known_dates = set(contract.known_closure_dates)
    return all(day.dayofweek >= 5 or day.date().isoformat() in known_dates for day in dates)


def normalize_and_validate(frame: pd.DataFrame, contract: DatasetContract) -> pd.DataFrame:
    """Return a UTC-normalized frame or fail before it reaches a backtest."""
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise DataContractError(f"Dataset missing required columns: {', '.join(missing)}")
    if frame.empty:
        raise DataContractError("Dataset is empty")

    df = frame.copy()
    try:
        timestamps = pd.to_datetime(df["timestamp"], errors="raise", utc=True)
    except (TypeError, ValueError) as exc:
        raise DataContractError("Dataset contains an invalid timestamp") from exc
    df["timestamp"] = timestamps

    if df["timestamp"].duplicated().any():
        duplicate = df.loc[df["timestamp"].duplicated(), "timestamp"].iloc[0]
        raise DataContractError(f"Duplicate timestamp: {duplicate.isoformat()}")
    if not df["timestamp"].is_monotonic_increasing:
        raise DataContractError("Timestamps must be strictly increasing; rows are not reordered")

    for column in REQUIRED_COLUMNS[1:]:
        values = pd.to_numeric(df[column], errors="coerce")
        if values.isna().any():
            raise DataContractError(f"Column {column} contains missing or non-numeric values")
        if (values <= 0).any():
            raise DataContractError(f"Column {column} must contain only positive prices")
        df[column] = values.astype(float)

    if (df["high"] < df[["open", "close", "low"]].max(axis=1)).any():
        raise DataContractError("OHLC violation: high must be at least open, close, and low")
    if (df["low"] > df[["open", "close", "high"]].min(axis=1)).any():
        raise DataContractError("OHLC violation: low must be at most open, close, and high")

    suspicious_gaps = []
    gaps = df["timestamp"].diff()
    candidate_positions = (gaps > pd.Timedelta(contract.interval)).to_numpy().nonzero()[0]
    for position in candidate_positions:
        previous = df["timestamp"].iloc[position - 1]
        current = df["timestamp"].iloc[position]
        gap = gaps.iloc[position]
        is_closure = _crosses_weekend(previous, current) or _is_configured_market_closure(previous, current, contract)
        if not (is_closure and gap <= pd.Timedelta(contract.max_non_trading_gap)):
            suspicious_gaps.append(
                "Suspicious gap from "
                f"{previous.isoformat()} to {current.isoformat()} ({gap})"
            )
    if suspicious_gaps:
        message = f"{len(suspicious_gaps)} suspicious data gap(s); first: {suspicious_gaps[0]}"
        if contract.gap_policy == "error":
            raise DataContractError(message)
        warnings.warn(message, DataContractWarning, stacklevel=2)
    return df
