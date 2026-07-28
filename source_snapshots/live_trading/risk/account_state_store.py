"""Transactional, account-scoped persistence for account risk state.

The store is deliberately separate from ``core.state_manager``: strategy
state and account-risk state have different owners and failure semantics.
SQLite ships with Python, so this introduces no new runtime dependency.
"""

from __future__ import annotations

import json
import math
import random
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Optional

from portfolio_config import (
    ACCOUNT_STATE_BUSY_TIMEOUT_MS,
    ACCOUNT_STATE_DB_PATH,
    ACCOUNT_STATE_LOCK_RETRY_ATTEMPTS,
    ACCOUNT_STATE_LOCK_RETRY_BASE_SEC,
)


class AccountStateStoreError(RuntimeError):
    """A state read/write failed and execution must fail closed."""


_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SENSITIVE_RESET_REASON_RE = re.compile(
    r"(?:password|token|api[_-]?(?:key|secret)|secret)", re.I
)


class AccountStateStore:
    """SQLite store with short ``BEGIN IMMEDIATE`` state transitions.

    Every operation opens a short-lived connection. This is intentional:
    multiple bot processes share the same database but never share a Python
    connection. WAL gives readers a stable snapshot while an update commits.
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS account_state (
        account_id TEXT PRIMARY KEY,
        starting_equity REAL CHECK (starting_equity IS NULL OR starting_equity > 0),
        peak_equity REAL CHECK (peak_equity IS NULL OR peak_equity > 0),
        last_equity REAL CHECK (last_equity IS NULL OR last_equity > 0),
        halted INTEGER NOT NULL DEFAULT 0 CHECK (halted IN (0, 1)),
        halt_reason TEXT,
        last_breach_at REAL,
        last_reset_id TEXT,
        version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
        updated_at REAL NOT NULL
    );
    """

    _RESET_AUDIT_SCHEMA = """
    CREATE TABLE IF NOT EXISTS reset_audit (
        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT NOT NULL,
        reset_id TEXT NOT NULL,
        reset_reason TEXT NOT NULL,
        reset_at REAL NOT NULL,
        old_starting_equity REAL,
        old_peak_equity REAL,
        old_last_equity REAL,
        old_halted INTEGER,
        old_halt_reason TEXT,
        new_starting_equity REAL NOT NULL,
        new_peak_equity REAL NOT NULL,
        new_last_equity REAL NOT NULL,
        new_halted INTEGER NOT NULL CHECK (new_halted IN (0, 1)),
        new_halt_reason TEXT,
        UNIQUE (account_id, reset_id)
    );
    """

    def __init__(
        self,
        account_id: str,
        *,
        db_path: Optional[Path | str] = None,
        legacy_runtime_dir: Optional[Path | str] = None,
        busy_timeout_ms: int = ACCOUNT_STATE_BUSY_TIMEOUT_MS,
        lock_retry_attempts: int = ACCOUNT_STATE_LOCK_RETRY_ATTEMPTS,
        lock_retry_base_sec: float = ACCOUNT_STATE_LOCK_RETRY_BASE_SEC,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        if not _ACCOUNT_ID_RE.fullmatch(account_id):
            raise ValueError("account_id must contain only letters, digits, '_' or '-'")
        if busy_timeout_ms < 1 or lock_retry_attempts < 1 or lock_retry_base_sec < 0:
            raise ValueError("invalid SQLite retry configuration")

        self.account_id = account_id
        self.db_path = Path(db_path or ACCOUNT_STATE_DB_PATH).resolve()
        self.legacy_runtime_dir = Path(
            legacy_runtime_dir or self.db_path.parent
        ).resolve()
        self.busy_timeout_ms = busy_timeout_ms
        self.lock_retry_attempts = lock_retry_attempts
        self.lock_retry_base_sec = lock_retry_base_sec
        self._now = now_fn

    def read_state(self) -> Optional[dict[str, Any]]:
        """Return this account's state, migrate legacy JSON, or return None.

        ``None`` means a clean account has not yet recorded its first equity.
        Any inability to read a non-clean store raises instead of inventing a
        fresh state; callers must block new trading in that case.
        """
        return self._transaction(self._read_or_migrate)

    def record_equity(
        self, equity: float, configured_starting_equity: Optional[float] = None
    ) -> dict[str, Any]:
        """Persist an equity observation without ever lowering the peak."""
        equity = self._positive_number(equity, "equity")
        if configured_starting_equity is not None:
            configured_starting_equity = self._positive_number(
                configured_starting_equity, "configured_starting_equity"
            )

        def operation(conn: sqlite3.Connection) -> dict[str, Any]:
            existing = self._read_or_migrate(conn)
            now = self._now()
            if existing is None:
                starting = configured_starting_equity or equity
                conn.execute(
                    """
                    INSERT INTO account_state (
                        account_id, starting_equity, peak_equity, last_equity,
                        halted, halt_reason, last_breach_at, version, updated_at
                    ) VALUES (?, ?, ?, ?, 0, NULL, NULL, 1, ?)
                    """,
                    (self.account_id, starting, equity, equity, now),
                )
            else:
                # SQL performs the monotonic comparison while this transaction
                # owns the write lock, so a concurrent writer cannot lose a peak.
                conn.execute(
                    """
                    UPDATE account_state
                    SET peak_equity = CASE
                            WHEN peak_equity IS NULL OR ? > peak_equity THEN ?
                            ELSE peak_equity
                        END,
                        starting_equity = COALESCE(starting_equity, ?),
                        last_equity = ?,
                        version = version + 1,
                        updated_at = ?
                    WHERE account_id = ?
                    """,
                    (
                        equity,
                        equity,
                        configured_starting_equity or equity,
                        equity,
                        now,
                        self.account_id,
                    ),
                )
            return self._fetch_state(conn)

        return self._transaction(operation)

    def halt(self, reason: str) -> tuple[dict[str, Any], bool]:
        """Set a sticky halt and return ``(state, newly_halted)``.

        ``halted`` is never assigned ``0`` by an update. The first successful
        breach reason is retained, so simultaneous monitors cannot overwrite
        the safety decision or each independently flatten the account.
        """
        if not isinstance(reason, str) or not reason:
            raise ValueError("halt reason is required")

        def operation(conn: sqlite3.Connection) -> tuple[dict[str, Any], bool]:
            existing = self._read_or_migrate(conn)
            was_halted = bool(existing and existing["halted"])
            now = self._now()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO account_state (
                        account_id, starting_equity, peak_equity, last_equity,
                        halted, halt_reason, last_breach_at, version, updated_at
                    ) VALUES (?, NULL, NULL, NULL, 1, ?, ?, 1, ?)
                    """,
                    (self.account_id, reason, now, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE account_state
                    SET halted = 1,
                        halt_reason = CASE
                            WHEN halted = 1 THEN halt_reason ELSE ?
                        END,
                        last_breach_at = CASE
                            WHEN halted = 1 THEN last_breach_at ELSE ?
                        END,
                        version = version + 1,
                        updated_at = ?
                    WHERE account_id = ?
                    """,
                    (reason, now, now, self.account_id),
                )
            return self._fetch_state(conn), not was_halted

        return self._transaction(operation)

    def reset_if_new(
        self,
        reset_id: str,
        reset_reason: str,
        current_equity: float,
        *,
        configured_starting_equity: Optional[float] = None,
        broker_balance: Optional[float] = None,
    ) -> tuple[dict[str, Any], bool]:
        """Reset risk state once for a new operator-issued reset ID.

        A previously audited ID is an idempotent no-op, even if it is no
        longer the most recent reset.  The caller is responsible for proving
        that the hub is flat immediately before this transition.
        """
        reset_id = self._reset_text(reset_id, "reset_id", max_length=128)
        reset_reason = self._reset_text(reset_reason, "reset_reason", max_length=512)
        current_equity = self._positive_number(current_equity, "current_equity")
        if configured_starting_equity is not None:
            configured_starting_equity = self._positive_number(
                configured_starting_equity, "configured_starting_equity"
            )
        if broker_balance is not None:
            broker_balance = self._positive_number(broker_balance, "broker_balance")
        starting_equity = (
            configured_starting_equity or broker_balance or current_equity
        )

        def operation(conn: sqlite3.Connection) -> tuple[dict[str, Any], bool]:
            existing = self._read_or_migrate(conn)
            duplicate = conn.execute(
                "SELECT 1 FROM reset_audit WHERE account_id = ? AND reset_id = ?",
                (self.account_id, reset_id),
            ).fetchone()
            if duplicate is not None:
                state = self._fetch_state(conn)
                if state is None:
                    raise AccountStateStoreError("reset audit exists without account state")
                return state, False

            old = existing or {}
            now = self._now()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO account_state (
                        account_id, starting_equity, peak_equity, last_equity,
                        halted, halt_reason, last_breach_at, last_reset_id, version, updated_at
                    ) VALUES (?, ?, ?, ?, 0, NULL, NULL, ?, 1, ?)
                    """,
                    (
                        self.account_id,
                        starting_equity,
                        current_equity,
                        current_equity,
                        reset_id,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE account_state
                    SET starting_equity = ?, peak_equity = ?, last_equity = ?,
                        halted = 0, halt_reason = NULL, last_breach_at = NULL,
                        last_reset_id = ?, version = version + 1, updated_at = ?
                    WHERE account_id = ?
                    """,
                    (
                        starting_equity,
                        current_equity,
                        current_equity,
                        reset_id,
                        now,
                        self.account_id,
                    ),
                )
            state = self._fetch_state(conn)
            assert state is not None
            conn.execute(
                """
                INSERT INTO reset_audit (
                    account_id, reset_id, reset_reason, reset_at,
                    old_starting_equity, old_peak_equity, old_last_equity,
                    old_halted, old_halt_reason,
                    new_starting_equity, new_peak_equity, new_last_equity,
                    new_halted, new_halt_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.account_id,
                    reset_id,
                    reset_reason,
                    now,
                    old.get("starting_equity"),
                    old.get("peak_equity"),
                    old.get("last_equity"),
                    int(old["halted"]) if existing is not None else None,
                    old.get("halt_reason"),
                    state["starting_equity"],
                    state["peak_equity"],
                    state["last_equity"],
                    int(state["halted"]),
                    state["halt_reason"],
                ),
            )
            return state, True

        return self._transaction(operation)

    def list_reset_audit(self) -> list[dict[str, Any]]:
        """Return retained reset audit events for this account in time order."""
        def operation(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT * FROM reset_audit WHERE account_id = ? ORDER BY audit_id",
                (self.account_id,),
            ).fetchall()
            return [dict(row) for row in rows]

        return self._transaction(operation)

    def _transaction(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        last_error: Optional[BaseException] = None
        for attempt in range(self.lock_retry_attempts):
            conn: Optional[sqlite3.Connection] = None
            try:
                conn = self._connect()
                conn.execute("BEGIN IMMEDIATE")
                result = operation(conn)
                conn.execute("COMMIT")
                return result
            except (sqlite3.DatabaseError, OSError) as error:
                if conn is not None:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.DatabaseError:
                        pass
                last_error = error
                if self._is_retryable_lock(error) and attempt + 1 < self.lock_retry_attempts:
                    time.sleep(
                        self.lock_retry_base_sec * (attempt + 1)
                        + random.uniform(0, self.lock_retry_base_sec)
                    )
                    continue
                break
            finally:
                if conn is not None:
                    conn.close()
        raise AccountStateStoreError(
            f"account state unavailable for {self.account_id}: {last_error}"
        ) from last_error

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.db_path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA trusted_schema = OFF")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(self._SCHEMA)
                columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(account_state)")
                }
                if "last_reset_id" not in columns:
                    conn.execute("ALTER TABLE account_state ADD COLUMN last_reset_id TEXT")
                conn.execute(self._RESET_AUDIT_SCHEMA)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS reset_audit_account_time "
                    "ON reset_audit (account_id, reset_at)"
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            return conn
        except BaseException:
            # On Windows an unsuccessful PRAGMA/schema setup can otherwise
            # retain the file handle and prevent both retry and safe cleanup.
            conn.close()
            raise

    def _read_or_migrate(self, conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        state = self._fetch_state(conn)
        if state is not None:
            return state

        legacy_state = self._load_legacy_state()
        if legacy_state is None:
            return None

        # Keep the original forever. A separate backup proves that migration
        # completed from a recoverable source before the SQL row is committed.
        legacy_path = self._legacy_path
        backup_path = legacy_path.with_suffix(legacy_path.suffix + ".bak")
        if not backup_path.exists():
            try:
                shutil.copy2(legacy_path, backup_path)
            except OSError as error:
                raise AccountStateStoreError(
                    f"legacy state backup failed: {backup_path}"
                ) from error

        conn.execute(
            """
            INSERT INTO account_state (
                account_id, starting_equity, peak_equity, last_equity,
                halted, halt_reason, last_breach_at, last_reset_id, version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 1, ?)
            """,
            (
                self.account_id,
                legacy_state["starting_equity"],
                legacy_state["peak_equity"],
                legacy_state["last_equity"],
                int(legacy_state["halted"]),
                legacy_state["halt_reason"],
                legacy_state["last_breach_at"],
                legacy_state["updated_at"],
            ),
        )
        return self._fetch_state(conn)

    @property
    def _legacy_path(self) -> Path:
        return self.legacy_runtime_dir / f"account_state_{self.account_id}.json"

    def _load_legacy_state(self) -> Optional[dict[str, Any]]:
        path = self._legacy_path
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as source:
                raw = json.load(source)
        except (OSError, json.JSONDecodeError) as error:
            raise AccountStateStoreError(f"legacy state is unreadable: {path}") from error
        if not isinstance(raw, dict):
            raise AccountStateStoreError(f"legacy state is not an object: {path}")

        last_breach = raw.get("last_breach")
        breach_at = raw.get("last_breach_at")
        if breach_at is None and isinstance(last_breach, dict):
            breach_at = last_breach.get("ts")
        return {
            "starting_equity": self._optional_positive(raw.get("starting_equity"), "starting_equity"),
            "peak_equity": self._optional_positive(raw.get("peak_equity"), "peak_equity"),
            "last_equity": self._optional_positive(raw.get("last_equity"), "last_equity"),
            "halted": self._strict_bool(raw.get("halted", False), "halted"),
            "halt_reason": self._optional_text(raw.get("halt_reason"), "halt_reason"),
            "last_breach_at": self._optional_timestamp(breach_at),
            "updated_at": self._optional_timestamp(raw.get("updated_at")) or self._now(),
        }

    def _fetch_state(self, conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        row = conn.execute(
            "SELECT account_id, starting_equity, peak_equity, last_equity, halted, "
            "halt_reason, last_breach_at, last_reset_id, version, updated_at "
            "FROM account_state WHERE account_id = ?",
            (self.account_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "account_id": row["account_id"],
            "starting_equity": row["starting_equity"],
            "peak_equity": row["peak_equity"],
            "last_equity": row["last_equity"],
            "halted": bool(row["halted"]),
            "halt_reason": row["halt_reason"],
            "last_breach_at": row["last_breach_at"],
            "last_reset_id": row["last_reset_id"],
            "version": row["version"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _is_retryable_lock(error: BaseException) -> bool:
        return isinstance(error, sqlite3.OperationalError) and any(
            token in str(error).lower() for token in ("locked", "busy")
        )

    @staticmethod
    def _positive_number(value: Any, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AccountStateStoreError(f"invalid {field} in account state")
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise AccountStateStoreError(f"invalid {field} in account state")
        return value

    @classmethod
    def _optional_positive(cls, value: Any, field: str) -> Optional[float]:
        return None if value is None else cls._positive_number(value, field)

    @staticmethod
    def _strict_bool(value: Any, field: str) -> bool:
        if not isinstance(value, bool):
            raise AccountStateStoreError(f"invalid {field} in account state")
        return value

    @staticmethod
    def _optional_text(value: Any, field: str) -> Optional[str]:
        if value is not None and not isinstance(value, str):
            raise AccountStateStoreError(f"invalid {field} in account state")
        return value

    @staticmethod
    def _reset_text(value: Any, field: str, *, max_length: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > max_length:
            raise AccountStateStoreError(f"invalid {field} in account reset")
        value = value.strip()
        if field == "reset_reason" and _SENSITIVE_RESET_REASON_RE.search(value):
            raise AccountStateStoreError("reset_reason must not contain credentials or secrets")
        return value

    @staticmethod
    def _optional_timestamp(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AccountStateStoreError("invalid timestamp in account state")
        value = float(value)
        if not math.isfinite(value):
            raise AccountStateStoreError("invalid timestamp in account state")
        return value
