"""Versioned schema and recovery types for per-strategy runtime state.

The state file can affect order decisions after a restart.  Its schema is
therefore deliberately small, dependency-free, and conservative: a malformed
or structurally ambiguous state must be recovered explicitly, never replaced
with defaults silently.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


CURRENT_SCHEMA_VERSION = 2

DEFAULT_STATE: dict[str, Any] = {
    "schema_version": CURRENT_SCHEMA_VERSION,
    "engine": {
        "last_signal_bar_time": None,
        "pending_order": {
            "active": False,
            "side": None,
            "retry_after": None,
        },
    },
    "strategy": {
        "last_long_signal_bar": None,
        "last_short_signal_bar": None,
        "breakeven_done": False,
    },
    "execution_cache": {},
}


class StateRecoveryCode(str, Enum):
    MALFORMED_JSON = "malformed_json"
    INVALID_SCHEMA = "invalid_schema"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    IO_ERROR = "io_error"


class StateRecoveryError(RuntimeError):
    """Typed error that requires an operator recovery decision."""

    def __init__(self, code: StateRecoveryCode, message: str, *, cause: Exception | None = None):
        super().__init__(message)
        self.code = code
        self.cause = cause


class StateLoadStatus(str, Enum):
    CREATED = "created"
    LOADED = "loaded"
    MIGRATED = "migrated"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True)
class StateLoadResult:
    status: StateLoadStatus
    error: StateRecoveryError | None = None
    quarantine_path: str | None = None

    @property
    def ready(self) -> bool:
        return self.error is None


def build_default_state() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_STATE)


def recursive_defaults_merge(defaults: Any, value: Any) -> Any:
    """Merge missing nested defaults while preserving valid extension fields."""
    if not isinstance(defaults, Mapping):
        return copy.deepcopy(value)
    if not isinstance(value, Mapping):
        raise StateRecoveryError(
            StateRecoveryCode.INVALID_SCHEMA,
            "Expected an object where the state schema requires an object.",
        )

    merged = copy.deepcopy(value)
    for key, default_value in defaults.items():
        if key not in value:
            merged[key] = copy.deepcopy(default_value)
        elif isinstance(default_value, Mapping):
            merged[key] = recursive_defaults_merge(default_value, value[key])
    return merged


def migrate_state(raw_state: Any) -> tuple[dict[str, Any], bool]:
    """Migrate known historical state layouts to ``CURRENT_SCHEMA_VERSION``."""
    if not isinstance(raw_state, Mapping):
        raise StateRecoveryError(
            StateRecoveryCode.INVALID_SCHEMA,
            "State root must be a JSON object.",
        )

    state = copy.deepcopy(dict(raw_state))
    version = state.get("schema_version")
    migrated = False

    # The original flat state had no schema version.  The later engine/strategy
    # layout was also unversioned, so treat it as v1 for an explicit migration.
    if version is None:
        version = 1 if "engine" in state or "strategy" in state else 0
        migrated = True

    if isinstance(version, bool) or not isinstance(version, int):
        raise StateRecoveryError(
            StateRecoveryCode.INVALID_SCHEMA,
            "schema_version must be an integer.",
        )
    if version > CURRENT_SCHEMA_VERSION:
        raise StateRecoveryError(
            StateRecoveryCode.UNSUPPORTED_SCHEMA,
            f"State schema_version={version} is newer than supported version "
            f"{CURRENT_SCHEMA_VERSION}.",
        )
    if version < 0:
        raise StateRecoveryError(
            StateRecoveryCode.INVALID_SCHEMA,
            "schema_version must not be negative.",
        )

    if version == 0:
        state = {
            "schema_version": 1,
            "engine": {
                "last_signal_bar_time": state.get("last_h1_bar_time"),
                "pending_order": state.get("pending_order", {}),
            },
            "strategy": {
                "last_long_signal_bar": state.get("last_long_signal_bar"),
                "last_short_signal_bar": state.get("last_short_signal_bar"),
                "breakeven_done": state.get("breakeven_done", False),
            },
            "execution_cache": state.get("execution_cache", {}),
        }
        version = 1
        migrated = True

    if version == 1:
        # v1 is the former engine/strategy layout.  v2 records the schema
        # explicitly and is otherwise backward compatible.
        state["schema_version"] = CURRENT_SCHEMA_VERSION
        version = CURRENT_SCHEMA_VERSION
        migrated = True

    if version != CURRENT_SCHEMA_VERSION:
        raise StateRecoveryError(
            StateRecoveryCode.UNSUPPORTED_SCHEMA,
            f"No migration path for schema_version={version}.",
        )

    return recursive_defaults_merge(DEFAULT_STATE, state), migrated
