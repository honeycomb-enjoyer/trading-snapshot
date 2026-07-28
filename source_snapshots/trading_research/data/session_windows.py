"""Resolve local market-clock anchors to causal H1 UTC boundaries."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd


def resolve_bar_endpoint(
    date: pd.Timestamp, endpoint: list | tuple, *, alignment: str,
    bar_interval: pd.Timedelta,
) -> pd.Timestamp:
    if len(endpoint) != 2:
        raise ValueError("Session endpoint must be [timezone, 'HH:MM']")
    zone = ZoneInfo(str(endpoint[0]))
    try:
        hour, minute = map(int, str(endpoint[1]).split(":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Session endpoint time must use HH:MM") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("Session endpoint time is outside 00:00..23:59")
    if alignment not in {"floor", "ceil"}:
        raise ValueError("Endpoint alignment must be 'floor' or 'ceil'")

    local = datetime(
        date.year, date.month, date.day, hour, minute, tzinfo=zone,
    )
    utc = pd.Timestamp(local.astimezone(timezone.utc))
    interval = pd.Timedelta(bar_interval)
    if interval <= pd.Timedelta(0):
        raise ValueError("bar_interval must be positive")
    return utc.floor(interval) if alignment == "floor" else utc.ceil(interval)


def resolve_bar_window(
    date: pd.Timestamp, window: dict, bar_interval: pd.Timedelta,
) -> tuple[pd.Timestamp, ...]:
    start = resolve_bar_endpoint(
        date, window["start"], alignment="floor", bar_interval=bar_interval,
    )
    boundary = resolve_bar_endpoint(
        date, window["boundary"], alignment="floor", bar_interval=bar_interval,
    )
    end = resolve_bar_endpoint(
        date, window["end"], alignment="ceil", bar_interval=bar_interval,
    )
    if not start < boundary < end:
        raise ValueError("Resolved session boundaries must be strictly chronological")
    return start, boundary, end


def resolve_h1_window(date: pd.Timestamp, window: dict) -> tuple[pd.Timestamp, ...]:
    """Compatibility wrapper for the H1 research report."""
    return resolve_bar_window(date, window, pd.Timedelta(hours=1))
