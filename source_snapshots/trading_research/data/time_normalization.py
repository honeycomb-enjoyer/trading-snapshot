"""Convert MT5 broker-server wall-clock timestamps to canonical UTC."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

from data.schema import DataContractError


@dataclass(frozen=True)
class BrokerTimeProfile:
    name: str
    mode: str
    standard_utc_offset_hours: float = 0.0
    dst_utc_offset_hours: float | None = None
    dst_reference_timezone: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object] | None) -> "BrokerTimeProfile":
        if not value:
            raise DataContractError(
                "MT5 downloads require an explicit broker_time_profile; "
                "choose mode='utc' or mode='server_wall_clock'"
            )
        profile = cls(
            name=str(value.get("name", "unnamed")),
            mode=str(value.get("mode", "")).lower(),
            standard_utc_offset_hours=float(value.get("standard_utc_offset_hours", 0.0)),
            dst_utc_offset_hours=(
                None if value.get("dst_utc_offset_hours") is None
                else float(value["dst_utc_offset_hours"])
            ),
            dst_reference_timezone=(
                None if value.get("dst_reference_timezone") is None
                else str(value["dst_reference_timezone"])
            ),
        )
        profile.validate()
        return profile

    def validate(self) -> None:
        if self.mode not in {"utc", "server_wall_clock"}:
            raise DataContractError("broker_time_profile mode must be 'utc' or 'server_wall_clock'")
        offsets = [self.standard_utc_offset_hours]
        if self.dst_utc_offset_hours is not None:
            offsets.append(self.dst_utc_offset_hours)
        if any(abs(value) > 14 for value in offsets):
            raise DataContractError("broker UTC offset must be within +/-14 hours")
        if self.mode == "server_wall_clock":
            has_dst_offset = self.dst_utc_offset_hours is not None
            has_dst_zone = self.dst_reference_timezone is not None
            if has_dst_offset != has_dst_zone:
                raise DataContractError(
                    "DST normalization requires both dst_utc_offset_hours and "
                    "dst_reference_timezone"
                )
            if has_dst_zone:
                try:
                    ZoneInfo(self.dst_reference_timezone)
                except ZoneInfoNotFoundError as exc:
                    raise DataContractError(
                        f"Unknown DST reference timezone: {self.dst_reference_timezone}"
                    ) from exc

    def metadata(self) -> dict:
        return {key: value for key, value in asdict(self).items() if value is not None}


def normalize_mt5_timestamps(values, profile: BrokerTimeProfile) -> pd.Series:
    """Return canonical UTC timestamps from MT5 raw epochs or exported labels."""
    series = pd.Series(values)
    if pd.api.types.is_numeric_dtype(series):
        encoded = pd.to_datetime(series, unit="s", errors="raise", utc=True)
    else:
        encoded = pd.to_datetime(series, errors="raise", utc=True)
    if profile.mode == "utc":
        return encoded

    server_naive = encoded.dt.tz_localize(None)
    offsets = pd.Series(profile.standard_utc_offset_hours, index=server_naive.index, dtype=float)
    if profile.dst_reference_timezone is not None:
        reference_noon = server_naive.dt.normalize() + pd.Timedelta(hours=12)
        localized = reference_noon.dt.tz_localize(
            profile.dst_reference_timezone,
            ambiguous="raise",
            nonexistent="raise",
        )
        dst_active = localized.map(lambda value: value.dst() != timedelta(0))
        offsets.loc[dst_active] = float(profile.dst_utc_offset_hours)
    canonical_naive = server_naive - pd.to_timedelta(offsets, unit="h")
    return canonical_naive.dt.tz_localize("UTC")


def normalize_mt5_frame(frame: pd.DataFrame, profile: BrokerTimeProfile) -> pd.DataFrame:
    if "timestamp" not in frame:
        raise DataContractError("Dataset missing timestamp for broker-time normalization")
    normalized = frame.copy()
    normalized["timestamp"] = normalize_mt5_timestamps(normalized["timestamp"], profile)
    return normalized
