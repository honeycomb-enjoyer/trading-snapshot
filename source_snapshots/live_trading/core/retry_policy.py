"""Validated, config-driven retry settings for broker operations.

Reconnect scheduling is deliberately owned by ``Broker`` instead of a retry
decorator: the runner must keep servicing its loop while a terminal is down.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    reconnect_attempts: int = 3
    reconnect_initial_backoff_sec: float = 1.0
    reconnect_backoff_multiplier: float = 2.0
    reconnect_max_backoff_sec: float = 15.0
    reconnect_circuit_cooldown_sec: float = 60.0
    operation_attempts: int = 3
    operation_backoff_sec: float = 0.2
    history_attempts: int = 2
    history_backoff_sec: float = 0.2
    position_visibility_poll_sec: float = 0.2
    intent_history_lookback_sec: float = 7 * 24 * 60 * 60

    def __post_init__(self) -> None:
        if self.reconnect_attempts < 1 or self.operation_attempts < 1 or self.history_attempts < 1:
            raise ValueError("retry attempts must be at least one")
        if any(value < 0 for value in (
            self.reconnect_initial_backoff_sec,
            self.reconnect_max_backoff_sec,
            self.reconnect_circuit_cooldown_sec,
            self.operation_backoff_sec,
            self.history_backoff_sec,
            self.position_visibility_poll_sec,
            self.intent_history_lookback_sec,
        )):
            raise ValueError("retry durations must not be negative")
        if self.reconnect_backoff_multiplier < 1:
            raise ValueError("reconnect_backoff_multiplier must be at least one")

    def reconnect_delay(self, failed_attempt: int) -> float:
        """Return the delay after a 1-based failed reconnect attempt."""
        return min(
            self.reconnect_initial_backoff_sec * (
                self.reconnect_backoff_multiplier ** max(0, failed_attempt - 1)
            ),
            self.reconnect_max_backoff_sec,
        )

    @classmethod
    def from_config(cls, config) -> "RetryPolicy":
        return cls(
            reconnect_attempts=config.MAX_RECONNECT_ATTEMPTS,
            reconnect_initial_backoff_sec=config.RECONNECT_INITIAL_BACKOFF_SEC,
            reconnect_backoff_multiplier=config.RECONNECT_BACKOFF_MULTIPLIER,
            reconnect_max_backoff_sec=config.RECONNECT_MAX_BACKOFF_SEC,
            reconnect_circuit_cooldown_sec=config.RECONNECT_CIRCUIT_COOLDOWN_SEC,
            operation_attempts=config.BROKER_OPERATION_RETRY_ATTEMPTS,
            operation_backoff_sec=config.BROKER_OPERATION_RETRY_BACKOFF_SEC,
            history_attempts=config.BROKER_HISTORY_RETRY_ATTEMPTS,
            history_backoff_sec=config.BROKER_HISTORY_RETRY_BACKOFF_SEC,
            position_visibility_poll_sec=config.BROKER_POSITION_VISIBILITY_POLL_SEC,
            intent_history_lookback_sec=config.INTENT_HISTORY_LOOKBACK_SEC,
        )
