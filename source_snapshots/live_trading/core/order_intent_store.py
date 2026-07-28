"""Durable, fail-closed journal for non-idempotent order submissions."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


class OrderIntentStoreError(RuntimeError):
    """The execution path must not submit when its durable journal is unavailable."""


class InvalidIntentTransition(OrderIntentStoreError):
    pass


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    account_id: str
    strategy_id: str
    signal_id: str
    symbol: str
    side: str
    requested_volume: float | None
    requested_sl: float | None
    requested_tp: float | None
    status: str
    client_reference: str
    broker_order_id: str | None
    broker_position_id: str | None
    filled_volume: float | None
    request: dict[str, Any] | None
    retcode: int | None
    broker_result: dict[str, Any] | None
    created_at: float
    submitted_at: float | None
    accepted_at: float | None
    filled_at: float | None
    updated_at: float
    last_error: str | None

    def __bool__(self) -> bool:
        """Keep the backward-compatible truthy-success contract without losing intent data."""
        return self.status in {"FILLED", "PARTIALLY_FILLED", "CLOSED"}


class OrderIntentStore:
    """SQLite-backed intent repository shared safely by legacy bot processes.

    SQLite is deliberately used directly, matching the transactional account-state
    store.  This keeps a critical execution path dependency-free and avoids adding
    an ORM migration surface for one small, single-host journal.
    """

    # Some MT5 servers apply a shorter practical comment limit than the
    # documented 31 characters.  A 64-bit token keeps the durable reference
    # compact (19 characters including ``oi-``) without relying on a broker
    # preserving a long strategy prefix.
    CLIENT_REFERENCE_HEX_LENGTH = 16

    @classmethod
    def _client_reference(cls, intent_id: str) -> str:
        return f"oi-{intent_id[:cls.CLIENT_REFERENCE_HEX_LENGTH]}"

    ACTIVE_STATUSES = frozenset(
        {"CREATED", "SUBMITTING", "ACCEPTED", "UNKNOWN", "RECONCILING"}
    )
    TERMINAL_STATUSES = frozenset({"REJECTED", "CANCELLED", "CLOSED"})
    _TRANSITIONS = {
        "CREATED": {"SUBMITTING", "REJECTED", "CANCELLED"},
        "SUBMITTING": {"ACCEPTED", "PARTIALLY_FILLED", "FILLED", "REJECTED", "UNKNOWN", "RECONCILING"},
        "ACCEPTED": {"PARTIALLY_FILLED", "FILLED", "RECONCILING", "UNKNOWN", "CLOSED"},
        "PARTIALLY_FILLED": {"FILLED", "RECONCILING", "UNKNOWN", "CLOSED"},
        "UNKNOWN": {"RECONCILING", "PARTIALLY_FILLED", "FILLED", "CLOSED"},
        "RECONCILING": {"PARTIALLY_FILLED", "FILLED", "CLOSED", "UNKNOWN"},
        "FILLED": {"CLOSED"},
        "REJECTED": {"SUBMITTING"},
        "CANCELLED": set(),
        "CLOSED": set(),
    }

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        busy_timeout_ms: int = 1_000,
        lock_retry_attempts: int = 4,
        lock_retry_base_sec: float = 0.05,
    ):
        root = Path(__file__).resolve().parents[1]
        self.db_path = Path(db_path) if db_path is not None else root / "runtime" / "order_intents.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = busy_timeout_ms
        self.lock_retry_attempts = lock_retry_attempts
        self.lock_retry_base_sec = lock_retry_base_sec
        self._initialize()

    def _connect(self):
        try:
            connection = sqlite3.connect(
                self.db_path, timeout=self.busy_timeout_ms / 1000, isolation_level=None
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
            return connection
        except sqlite3.DatabaseError as exc:
            raise OrderIntentStoreError(f"cannot open order intent store: {exc}") from exc

    def _initialize(self):
        def operation(connection):
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS order_intents (
                    intent_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    signal_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    requested_volume REAL,
                    requested_sl REAL,
                    requested_tp REAL,
                    status TEXT NOT NULL,
                    client_reference TEXT NOT NULL UNIQUE,
                    broker_order_id TEXT,
                    broker_position_id TEXT,
                    filled_volume REAL,
                    request_json TEXT,
                    retcode INTEGER,
                    broker_result_json TEXT,
                    created_at REAL NOT NULL,
                    submitted_at REAL,
                    accepted_at REAL,
                    filled_at REAL,
                    updated_at REAL NOT NULL,
                    last_error TEXT,
                    UNIQUE(account_id, strategy_id, signal_id),
                    UNIQUE(account_id, broker_order_id),
                    UNIQUE(account_id, broker_position_id)
                )
                """
            )
            self._migrate_broker_id_scope(connection)
            self._repair_invalid_zero_broker_ids(connection)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_order_intents_active "
                "ON order_intents(account_id, strategy_id, status)"
            )

        self._run(operation)

    @staticmethod
    def _migrate_broker_id_scope(connection):
        """Replace legacy globally-unique broker IDs with account-scoped IDs."""
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'order_intents'"
        ).fetchone()["sql"]
        if "broker_order_id TEXT UNIQUE" not in sql:
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("ALTER TABLE order_intents RENAME TO order_intents_legacy_scope")
            connection.execute(
                """
                CREATE TABLE order_intents (
                    intent_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    signal_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    requested_volume REAL,
                    requested_sl REAL,
                    requested_tp REAL,
                    status TEXT NOT NULL,
                    client_reference TEXT NOT NULL UNIQUE,
                    broker_order_id TEXT,
                    broker_position_id TEXT,
                    filled_volume REAL,
                    request_json TEXT,
                    retcode INTEGER,
                    broker_result_json TEXT,
                    created_at REAL NOT NULL,
                    submitted_at REAL,
                    accepted_at REAL,
                    filled_at REAL,
                    updated_at REAL NOT NULL,
                    last_error TEXT,
                    UNIQUE(account_id, strategy_id, signal_id),
                    UNIQUE(account_id, broker_order_id),
                    UNIQUE(account_id, broker_position_id)
                )
                """
            )
            connection.execute(
                "INSERT INTO order_intents SELECT * FROM order_intents_legacy_scope"
            )
            connection.execute("DROP TABLE order_intents_legacy_scope")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _repair_invalid_zero_broker_ids(connection):
        """Normalize MT5's zero placeholders and repair their failed transition.

        MT5 returns ``order=0`` / ``deal=0`` for rejected submissions.  Older
        versions persisted that placeholder as the real text ID ``"0"``.
        A later rejection on the same account then violated the account-scoped
        UNIQUE constraint and left the newer intent stuck in SUBMITTING because
        its REJECTED transition rolled back.

        The repair is deliberately narrow: only the first later SUBMITTING row
        for the same account+strategy, with no broker IDs or result recorded,
        is terminalized.  The legacy zero row is the durable evidence that this
        exact failed-transition pattern occurred; unrelated uncertain submits
        remain fail-closed and reconcilable.
        """
        connection.execute("BEGIN IMMEDIATE")
        try:
            invalid_rejections = connection.execute(
                """
                SELECT account_id, strategy_id, retcode, updated_at
                FROM order_intents
                WHERE status = 'REJECTED'
                  AND TRIM(COALESCE(broker_order_id, '')) = '0'
                ORDER BY updated_at
                """
            ).fetchall()
            for rejected in invalid_rejections:
                stuck = connection.execute(
                    """
                    SELECT intent_id
                    FROM order_intents
                    WHERE account_id = ?
                      AND strategy_id = ?
                      AND status = 'SUBMITTING'
                      AND retcode IS NULL
                      AND broker_order_id IS NULL
                      AND broker_position_id IS NULL
                      AND created_at >= ?
                    ORDER BY created_at
                    LIMIT 1
                    """,
                    (
                        rejected["account_id"],
                        rejected["strategy_id"],
                        rejected["updated_at"],
                    ),
                ).fetchone()
                if stuck is not None:
                    connection.execute(
                        """
                        UPDATE order_intents
                        SET status = 'REJECTED',
                            retcode = COALESCE(retcode, ?),
                            updated_at = ?,
                            last_error = ?
                        WHERE intent_id = ?
                        """,
                        (
                            rejected["retcode"],
                            time.time(),
                            "recovered invalid zero broker-order placeholder collision",
                            stuck["intent_id"],
                        ),
                    )

            connection.execute(
                """
                UPDATE order_intents
                SET broker_order_id = NULL
                WHERE TRIM(COALESCE(broker_order_id, '')) IN ('', '0')
                """
            )
            connection.execute(
                """
                UPDATE order_intents
                SET broker_position_id = NULL
                WHERE TRIM(COALESCE(broker_position_id, '')) IN ('', '0')
                """
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def get_or_create(
        self, *, account_id: str, strategy_id: str, signal_id: str, symbol: str, side: str
    ) -> tuple[OrderIntent, bool]:
        """Atomically allocate an intent; an existing unique row always wins."""
        now = time.time()
        intent_id = uuid.uuid4().hex
        client_reference = self._client_reference(intent_id)

        def operation(connection):
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO order_intents (
                        intent_id, account_id, strategy_id, signal_id, symbol, side,
                        status, client_reference, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'CREATED', ?, ?, ?)
                    """,
                    (intent_id, account_id, strategy_id, signal_id, symbol, side, client_reference, now, now),
                )
                row = connection.execute(
                    "SELECT * FROM order_intents WHERE account_id = ? AND strategy_id = ? AND signal_id = ?",
                    (account_id, strategy_id, signal_id),
                ).fetchone()
                connection.execute("COMMIT")
                return self._from_row(row), row["intent_id"] == intent_id
            except BaseException:
                connection.execute("ROLLBACK")
                raise

        return self._run(operation)

    def claim_signal(
        self, *, account_id: str, strategy_id: str, signal_id: str, symbol: str, side: str
    ) -> tuple[OrderIntent, bool]:
        """Atomically claim this strategy's only unresolved open intent.

        The unique signal key protects duplicate callers; the active-intent
        check protects a restarted worker that regenerates a fresh UUID before
        the previous non-idempotent submit has been reconciled.
        """
        now = time.time()
        intent_id = uuid.uuid4().hex
        client_reference = self._client_reference(intent_id)
        active_placeholders = ", ".join("?" for _ in self.ACTIVE_STATUSES)

        def operation(connection):
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM order_intents WHERE account_id = ? AND strategy_id = ? AND signal_id = ?",
                    (account_id, strategy_id, signal_id),
                ).fetchone()
                if row is not None:
                    connection.execute("COMMIT")
                    return self._from_row(row), False
                row = connection.execute(
                    f"SELECT * FROM order_intents WHERE account_id = ? AND strategy_id = ? "
                    f"AND status IN ({active_placeholders}) ORDER BY created_at LIMIT 1",
                    (account_id, strategy_id, *sorted(self.ACTIVE_STATUSES)),
                ).fetchone()
                if row is not None:
                    connection.execute("COMMIT")
                    return self._from_row(row), False
                connection.execute(
                    """
                    INSERT INTO order_intents (
                        intent_id, account_id, strategy_id, signal_id, symbol, side,
                        status, client_reference, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'CREATED', ?, ?, ?)
                    """,
                    (intent_id, account_id, strategy_id, signal_id, symbol, side, client_reference, now, now),
                )
                connection.execute("COMMIT")
                return self._from_row(connection.execute(
                    "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
                ).fetchone()), True
            except BaseException:
                connection.execute("ROLLBACK")
                raise

        return self._run(operation)

    def get(self, account_id: str, strategy_id: str, signal_id: str) -> OrderIntent | None:
        return self._one(
            "SELECT * FROM order_intents WHERE account_id = ? AND strategy_id = ? AND signal_id = ?",
            (account_id, strategy_id, signal_id),
        )

    def get_active(self, account_id: str, strategy_id: str) -> OrderIntent | None:
        placeholders = ", ".join("?" for _ in self.ACTIVE_STATUSES)
        return self._one(
            f"SELECT * FROM order_intents WHERE account_id = ? AND strategy_id = ? "
            f"AND status IN ({placeholders}) ORDER BY created_at LIMIT 1",
            (account_id, strategy_id, *sorted(self.ACTIVE_STATUSES)),
        )

    def list_reconcilable(self, account_id: str | None = None) -> list[OrderIntent]:
        statuses = ("SUBMITTING", "ACCEPTED", "UNKNOWN", "RECONCILING")
        placeholders = ", ".join("?" for _ in statuses)
        query = f"SELECT * FROM order_intents WHERE status IN ({placeholders})"
        params: tuple[Any, ...] = statuses
        if account_id is not None:
            query += " AND account_id = ?"
            params += (account_id,)

        def operation(connection):
            return [self._from_row(row) for row in connection.execute(query, params).fetchall()]

        return self._run(operation)

    def set_request(self, intent_id: str, *, volume: float, sl: float, tp: float, request: dict[str, Any]):
        return self._update(
            intent_id,
            values={
                "requested_volume": float(volume),
                "requested_sl": float(sl),
                "requested_tp": float(tp),
                "request_json": self._json(request),
            },
        )

    def transition(self, intent_id: str, status: str, **values: Any) -> OrderIntent:
        if status not in self._TRANSITIONS:
            raise InvalidIntentTransition(f"unknown order intent status: {status}")

        def operation(connection):
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
                ).fetchone()
                if row is None:
                    raise OrderIntentStoreError(f"order intent not found: {intent_id}")
                current = row["status"]
                if current != status and status not in self._TRANSITIONS.get(current, set()):
                    raise InvalidIntentTransition(f"{current} -> {status} is not allowed")
                now = time.time()
                fields = {"status": status, "updated_at": now}
                fields.update(self._normalise_values(values))
                if status == "SUBMITTING":
                    fields["submitted_at"] = now
                if status in {"ACCEPTED", "PARTIALLY_FILLED"}:
                    fields["accepted_at"] = row["accepted_at"] or now
                if status in {"FILLED", "PARTIALLY_FILLED"}:
                    fields["filled_at"] = row["filled_at"] or now
                assignments = ", ".join(f"{name} = ?" for name in fields)
                connection.execute(
                    f"UPDATE order_intents SET {assignments} WHERE intent_id = ?",
                    (*fields.values(), intent_id),
                )
                updated = connection.execute(
                    "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
                ).fetchone()
                connection.execute("COMMIT")
                return self._from_row(updated)
            except BaseException:
                connection.execute("ROLLBACK")
                raise

        return self._run(operation)

    def _update(self, intent_id: str, *, values: dict[str, Any]) -> OrderIntent:
        values = self._normalise_values(values)

        def operation(connection):
            now = time.time()
            values_with_time = {**values, "updated_at": now}
            assignments = ", ".join(f"{name} = ?" for name in values_with_time)
            cursor = connection.execute(
                f"UPDATE order_intents SET {assignments} WHERE intent_id = ?",
                (*values_with_time.values(), intent_id),
            )
            if cursor.rowcount != 1:
                raise OrderIntentStoreError(f"order intent not found: {intent_id}")
            return self._from_row(
                connection.execute("SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)).fetchone()
            )

        return self._run(operation)

    def _one(self, query: str, params: Iterable[Any]) -> OrderIntent | None:
        def operation(connection):
            row = connection.execute(query, tuple(params)).fetchone()
            return self._from_row(row) if row is not None else None

        return self._run(operation)

    def _run(self, operation):
        for attempt in range(self.lock_retry_attempts + 1):
            connection = None
            try:
                connection = self._connect()
                return operation(connection)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt >= self.lock_retry_attempts:
                    raise OrderIntentStoreError(f"order intent store operation failed: {exc}") from exc
                time.sleep(self.lock_retry_base_sec * (2**attempt))
            except (sqlite3.DatabaseError, ValueError, TypeError) as exc:
                raise OrderIntentStoreError(f"order intent store operation failed: {exc}") from exc
            finally:
                if connection is not None:
                    connection.close()
        raise AssertionError("unreachable")

    @staticmethod
    def _json(value: dict[str, Any] | None) -> str | None:
        return None if value is None else json.dumps(value, sort_keys=True, default=str)

    def _normalise_values(self, values: dict[str, Any]) -> dict[str, Any]:
        normalised = dict(values)
        for key in ("request", "broker_result"):
            if key in normalised:
                normalised[f"{key}_json"] = self._json(normalised.pop(key))
        for key in ("broker_order_id", "broker_position_id"):
            if key in normalised:
                normalised[key] = self._normalise_broker_id(normalised[key])
        return normalised

    @staticmethod
    def _normalise_broker_id(value: Any) -> str | None:
        """Return only a real positive broker identifier.

        MT5 uses numeric zero as an absent order/deal/position ID on failed
        requests.  Persisting it would make unrelated failures collide on the
        account-scoped unique index.
        """
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            if int(text) <= 0:
                return None
        except ValueError:
            # Preserve non-numeric broker identifiers for testable adapter
            # compatibility; MT5 itself currently supplies positive integers.
            pass
        return text

    @staticmethod
    def _from_row(row: sqlite3.Row) -> OrderIntent:
        def load(value):
            return json.loads(value) if value else None

        return OrderIntent(
            intent_id=row["intent_id"], account_id=row["account_id"], strategy_id=row["strategy_id"],
            signal_id=row["signal_id"], symbol=row["symbol"], side=row["side"],
            requested_volume=row["requested_volume"], requested_sl=row["requested_sl"], requested_tp=row["requested_tp"],
            status=row["status"], client_reference=row["client_reference"],
            broker_order_id=row["broker_order_id"], broker_position_id=row["broker_position_id"],
            filled_volume=row["filled_volume"], request=load(row["request_json"]), retcode=row["retcode"],
            broker_result=load(row["broker_result_json"]), created_at=row["created_at"],
            submitted_at=row["submitted_at"], accepted_at=row["accepted_at"], filled_at=row["filled_at"],
            updated_at=row["updated_at"], last_error=row["last_error"],
        )
