from datetime import datetime, timezone
import pytest

from core.broker_clock import BrokerClock, BrokerClockError


def test_broker_wall_clock_offset_is_removed_from_tick():
    actual_utc = datetime(2026, 7, 13, 4, 0, 1, tzinfo=timezone.utc).timestamp()
    broker_encoded = actual_utc + 3 * 60 * 60
    clock = BrokerClock(time_fn=lambda: actual_utc)
    normalized = clock.observe(broker_encoded)
    assert normalized == datetime(2026, 7, 13, 4, 0, 1, tzinfo=timezone.utc)
    assert normalized.timestamp() == actual_utc
    assert clock.offset_seconds == 3 * 60 * 60


def test_clock_recalibrates_when_broker_dst_offset_changes():
    clock = BrokerClock(time_fn=lambda: 1_000_000)
    clock.observe(1_000_000 + 2 * 60 * 60)
    assert clock.offset_seconds == 2 * 60 * 60
    clock.observe(1_000_000 + 3 * 60 * 60)
    assert clock.offset_seconds == 3 * 60 * 60


def test_stale_tick_cannot_guess_an_offset():
    clock = BrokerClock(time_fn=lambda: 1_000_000)
    with pytest.raises(BrokerClockError, match="fresh tick"):
        clock.observe(1_000_000 + 3 * 60 * 60 - 10 * 60)


def test_calibrated_clock_keeps_offset_during_repeated_nightly_tick():
    actual = 1_000_000
    raw = actual + 3 * 60 * 60
    clock = BrokerClock(time_fn=lambda: actual)
    clock.observe(raw)

    normalized = clock.normalize_live_tick(
        raw,
        previous_raw_epoch=raw,
        observed_epoch=actual + 60 * 60,
    )

    assert normalized.timestamp() == actual
    assert clock.offset_seconds == 3 * 60 * 60


def test_new_tick_can_recalibrate_dst_after_market_reopens():
    actual = 1_000_000
    old_raw = actual + 2 * 60 * 60
    clock = BrokerClock(time_fn=lambda: actual)
    clock.observe(old_raw)
    reopened = actual + 3 * 24 * 60 * 60
    new_raw = reopened + 3 * 60 * 60

    normalized = clock.normalize_live_tick(
        new_raw,
        previous_raw_epoch=old_raw,
        observed_epoch=reopened,
    )

    assert normalized.timestamp() == reopened
    assert clock.offset_seconds == 3 * 60 * 60
