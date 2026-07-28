# core/state_manager.py

import json
import os
import copy
import shutil
import tempfile
import uuid
from pathlib import Path

from core.state_schema import (
    DEFAULT_STATE,
    StateLoadResult,
    StateLoadStatus,
    StateRecoveryCode,
    StateRecoveryError,
    build_default_state,
    migrate_state,
)

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / "runtime"
RUNTIME_DIR.mkdir(exist_ok=True)


class StateManager:
    def __init__(self, strategy_name, alerts=None, runtime_dir=None):
        if not strategy_name or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"  # pragma: allowlist secret
            for character in strategy_name
        ):
            raise ValueError("strategy_name must contain only letters, digits, '_' or '-'")
        self.state = None
        self.strategy_name = strategy_name
        self.alerts = alerts
        self._persisted_state = None

        self.state_file = (
            Path(runtime_dir) if runtime_dir is not None else RUNTIME_DIR
        ) / (
            f"state_{strategy_name}.json"
        )
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

    # =====================================
    # ALERT HELPER
    # =====================================

    def _log(self, message, critical=False):
        msg = f"[STATE] {message}"

        if self.alerts:
            if critical:
                self.alerts.send_critical(msg)
            else:
                self.alerts.send_info(msg)
        else:
            print(msg)
    # =====================================
    # LOAD
    # =====================================
    def load(self):
        if not self.state_file.exists():
            self.state = build_default_state()
            self.save()
            return StateLoadResult(StateLoadStatus.CREATED)

        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                raw_state = json.load(f)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
            code = (
                StateRecoveryCode.MALFORMED_JSON
                if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError))
                else StateRecoveryCode.IO_ERROR
            )
            return self._recovery_required(code, str(error), error, quarantine=True)

        try:
            self.state, migrated = migrate_state(raw_state)
        except StateRecoveryError as error:
            return self._recovery_required(
                error.code, str(error), error, quarantine=False
            )

        if migrated:
            self._log("State migrated to schema_version=2.")
            self._persisted_state = None
            self.save()
            return StateLoadResult(StateLoadStatus.MIGRATED)
        self._persisted_state = copy.deepcopy(self.state)
        return StateLoadResult(StateLoadStatus.LOADED)

    def _recovery_required(self, code, message, cause, *, quarantine):
        quarantine_path = None
        if quarantine:
            try:
                quarantine_path = self._quarantine_corrupt_file()
            except OSError as quarantine_error:
                message = f"{message}; quarantine failed: {quarantine_error}"
        error = StateRecoveryError(code, message, cause=cause)
        self.state = None
        self._persisted_state = None
        self._log(
            f"State recovery required ({code.value}); trading remains blocked. "
            f"Original state was not overwritten. Error: {message}",
            critical=True,
        )
        return StateLoadResult(
            StateLoadStatus.RECOVERY_REQUIRED,
            error=error,
            quarantine_path=str(quarantine_path) if quarantine_path else None,
        )

    def _quarantine_corrupt_file(self):
        """Copy malformed input aside while retaining it as the recovery gate."""
        quarantine_path = self.state_file.with_name(
            f"{self.state_file.name}.corrupt.{uuid.uuid4().hex}.json"
        )
        shutil.copy2(self.state_file, quarantine_path)
        return quarantine_path

    # =====================================
    # SAVE
    # =====================================
    def save(self):
        if self.state is None:
            raise RuntimeError("State not loaded")
        if self._persisted_state == self.state and self.state_file.exists():
            return False

        # The temp path is unique and resides on the same volume, so replace
        # is atomic and concurrent processes never share a .tmp filename.
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{self.state_file.name}.",
            suffix=".tmp",
            dir=self.state_file.parent,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(self.state, file, indent=4, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp_path, self.state_file)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            raise
        self._persisted_state = copy.deepcopy(self.state)
        return True

    # =====================================
    # RESET
    # =====================================
    def reset(self):
        self.state = build_default_state()
        self.save()

    # =====================================
    # FULL SNAPSHOT
    # =====================================
    def snapshot(self):
        return copy.deepcopy(self.state)

    # =====================================
    # ENGINE HELPERS
    # =====================================
    def get_engine(self):
        return self.state["engine"]

    def set_last_signal_bar(self, bar_time):
        self.state["engine"]["last_signal_bar_time"] = bar_time
        self.save()

    def get_last_signal_bar(self):
        return self.state["engine"]["last_signal_bar_time"]

    # =====================================
    # PENDING ORDER HELPERS
    # =====================================
    def set_pending_order(self, active, side=None, retry_after=None):
        self.state["engine"]["pending_order"] = {
            "active": active,
            "side": side,
            "retry_after": retry_after
        }
        self.save()

    def get_pending_order(self):
        return self.state["engine"]["pending_order"]

    def clear_pending_order(self):
        self.state["engine"]["pending_order"] = {
            "active": False,
            "side": None,
            "retry_after": None
        }
        self.save()

    # =====================================
    # STRATEGY HELPERS
    # =====================================
    def get_strategy(self):
        return self.state["strategy"]

    def set_strategy_value(self, key, value):
        self.state["strategy"][key] = value
        self.save()

    def get_strategy_value(self, key, default=None):
        return self.state["strategy"].get(key, default)

    # =====================================
    # EXECUTION CACHE HELPERS
    # =====================================
    def set_execution_cache(self, position_id, payload):
        self.state["execution_cache"][str(position_id)] = payload
        self.save()

    def get_execution_cache(self, position_id):
        return self.state["execution_cache"].get(str(position_id))

    def clear_execution_cache(self, position_id):
        pid = str(position_id)

        if pid in self.state["execution_cache"]:
            del self.state["execution_cache"][pid]
            self.save()

