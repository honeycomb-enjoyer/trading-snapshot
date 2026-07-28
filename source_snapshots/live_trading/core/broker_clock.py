"""Normalize broker wall-clock epochs to canonical UTC.

Some MT5 servers encode their server-local wall clock as Unix seconds.  The
numeric value then looks like UTC, but is ahead of real UTC by the broker
offset.  Live calibration compares a fresh broker tick with the host epoch,
rounds the difference to a valid whole-hour offset, and fails closed when
the observation is ambiguous.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone


class BrokerClockError(RuntimeError):
    """Raised when a broker timestamp cannot be normalized safely."""


class BrokerClock:
    def __init__(
        self,
        *,
        time_fn=time.time,
        offset_step_sec: int = 60 * 60,
        max_observation_error_sec: int = 120,
        max_abs_offset_sec: int = 14 * 60 * 60,
    ):
        self._time = time_fn
        self.offset_step_sec = int(offset_step_sec)
        self.max_observation_error_sec = int(max_observation_error_sec)
        self.max_abs_offset_sec = int(max_abs_offset_sec)
        self.offset_seconds: int | None = None
        self.last_observed_at_utc: datetime | None = None

    def observe(self, raw_epoch: float, *, observed_epoch: float | None = None) -> datetime:
        """Calibrate from a fresh tick and return its real UTC timestamp."""
        raw = float(raw_epoch)
        observed = float(self._time() if observed_epoch is None else observed_epoch)
        difference = raw - observed
        candidate = int(round(difference / self.offset_step_sec) * self.offset_step_sec)
        error = abs(difference - candidate)
        if abs(candidate) > self.max_abs_offset_sec:
            raise BrokerClockError(f"broker UTC offset out of range: {candidate}s")
        if error > self.max_observation_error_sec:
            raise BrokerClockError(
                "broker clock calibration requires a fresh tick; "
                f"nearest offset error is {error:.1f}s"
            )
        self.offset_seconds = candidate
        self.last_observed_at_utc = datetime.fromtimestamp(observed, timezone.utc)
        return self.normalize_epoch(raw)

    def normalize_epoch(self, raw_epoch: float) -> datetime:
        if self.offset_seconds is None:
            raise BrokerClockError("broker clock is not calibrated")
        return datetime.fromtimestamp(
            float(raw_epoch) - self.offset_seconds,
            timezone.utc,
        )

    def normalize_live_tick(
        self,
        raw_epoch: float,
        *,
        previous_raw_epoch: float | None = None,
        observed_epoch: float | None = None,
    ) -> datetime:
        """Normalize a live tick without recalibrating from a frozen quote.

        Startup still calls :meth:`observe` and therefore requires a fresh
        tick.  Once an offset is known, a repeated/stale symbol quote is safe
        to normalize with that offset.  A changed fresh tick may recalibrate
        the offset, which is required when the broker changes DST.
        """
        raw = float(raw_epoch)
        observed = float(self._time() if observed_epoch is None else observed_epoch)
        if self.offset_seconds is None:
            return self.observe(raw, observed_epoch=observed)

        normalized = self.normalize_epoch(raw)
        age = observed - normalized.timestamp()
        if abs(age) <= self.max_observation_error_sec:
            return self.observe(raw, observed_epoch=observed)

        raw_changed = previous_raw_epoch is not None and raw != float(previous_raw_epoch)
        if raw_changed:
            difference = raw - observed
            candidate = int(round(difference / self.offset_step_sec) * self.offset_step_sec)
            error = abs(difference - candidate)
            if (
                candidate != self.offset_seconds
                and abs(candidate) <= self.max_abs_offset_sec
                and error <= self.max_observation_error_sec
            ):
                return self.observe(raw, observed_epoch=observed)

        return normalized

    def utc_now(self) -> datetime:
        return datetime.fromtimestamp(self._time(), timezone.utc)

    def status_snapshot(self) -> dict:
        return {
            "offset_seconds": self.offset_seconds,
            "offset_hours": (
                None if self.offset_seconds is None else self.offset_seconds / 3600
            ),
            "last_observed_at_utc": (
                None
                if self.last_observed_at_utc is None
                else self.last_observed_at_utc.isoformat()
            ),
        }
