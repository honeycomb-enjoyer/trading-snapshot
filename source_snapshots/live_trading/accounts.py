"""Non-secret account metadata for the sanitized live-trading snapshot.

Credentials live in ``secret_config.ACCOUNTS``, never here. This module keeps
the account-level risk-state contract without exposing real accounts.
"""

from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path


ACCOUNTS = {
    "hub_demo": {
        "description": "Demo account used by the application snapshot",
        "environment": "demo",
        "risk_rules": {
            "max_dd_percent": None,
            "hard_dd_percent": None,
            "profit_target_percent": None,
            "profit_warning_percent": None,
            "starting_equity": None,
            "check_interval_sec": 5,
            "reset_state_on_startup": False,
        },
    },
}


DEFAULT_RISK_RULES = {
    "max_dd_percent": None,
    "hard_dd_percent": None,
    "profit_target_percent": None,
    "profit_warning_percent": None,
    "starting_equity": None,
    "check_interval_sec": 5,
    "reset_state_on_startup": False,
}


DEFAULT_BROKER_CLOCK_SETTINGS = {
    "offset_step_sec": 60 * 60,
    "max_observation_error_sec": 120,
    "max_abs_offset_sec": 14 * 60 * 60,
}


def get_account(name):
    """Return account metadata and fail fast on unknown account IDs."""
    if name not in ACCOUNTS:
        raise KeyError(
            f"Unknown account: {name!r}. "
            f"Available: {list(ACCOUNTS.keys())}"
        )
    return ACCOUNTS[name]


def get_risk_rules(name):
    """Return complete account risk rules as a shallow mutable copy."""
    account = get_account(name)
    rules = account.get("risk_rules")

    merged = dict(DEFAULT_RISK_RULES)
    if rules is not None:
        merged.update(rules)
    if merged["reset_state_on_startup"] is True:
        merged["_reset_request_token"] = startup_reset_token(name)
    return merged


def list_accounts():
    """Return registered account names."""
    return list(ACCOUNTS.keys())


def get_broker_clock_settings(name):
    """Return broker-clock inference bounds for one account."""
    account = get_account(name)
    settings = dict(DEFAULT_BROKER_CLOCK_SETTINGS)
    settings.update(account.get("broker_clock", {}))
    return settings


def startup_reset_token(name: str, *, source_path: Path | None = None) -> str:
    """Return a stable one-shot token for the current ``True`` flag version."""
    path = Path(source_path or __file__).resolve()
    stat = path.stat()
    raw = f"{path}|{name}|{stat.st_mtime_ns}|{stat.st_size}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def consume_startup_reset_flag(
    name: str,
    expected_token: str,
    *,
    source_path: Path | None = None,
) -> bool:
    """Atomically change one account's literal startup-reset flag to ``False``.

    A token mismatch means the operator changed the file after this process
    started; do not overwrite that newer instruction.
    """
    path = Path(source_path or __file__).resolve()
    if startup_reset_token(name, source_path=path) != expected_token:
        return False
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    value_node = _find_reset_flag_node(tree, name)
    if not isinstance(value_node, ast.Constant) or value_node.value is not True:
        return False
    lines = source.splitlines(keepends=True)
    index = value_node.lineno - 1
    line = lines[index]
    position = line.find("True", value_node.col_offset)
    if position < 0:
        return False
    lines[index] = line[:position] + "False" + line[position + len("True"):]
    updated = "".join(lines)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as target:
            target.write(updated)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
    return True


def _find_reset_flag_node(tree: ast.AST, account_name: str) -> ast.expr | None:
    for assignment in tree.body:
        if not isinstance(assignment, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "ACCOUNTS"
            for target in assignment.targets
        ):
            continue
        if not isinstance(assignment.value, ast.Dict):
            return None
        account = _dict_value(assignment.value, account_name)
        if not isinstance(account, ast.Dict):
            return None
        rules = _dict_value(account, "risk_rules")
        return _dict_value(rules, "reset_state_on_startup") if isinstance(rules, ast.Dict) else None
    return None


def _dict_value(mapping: ast.Dict, key: str) -> ast.expr | None:
    for candidate, value in zip(mapping.keys, mapping.values):
        if isinstance(candidate, ast.Constant) and candidate.value == key:
            return value
    return None
