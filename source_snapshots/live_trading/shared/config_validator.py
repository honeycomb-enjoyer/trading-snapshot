"""Offline validation for the live-trading configuration contract.

The validator parses configuration as data instead of importing
``secret_config.py`` or strategy modules. A dry run therefore cannot execute
MT5 initialization or another configuration side effect.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError


LIVE_TRADING_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_STRATEGY_FIELDS = ("symbol", "asset_class", "magic", "account", "enabled")
REQUIRED_CREDENTIAL_FIELDS = ("login", "password", "server", "mt5_path")
REQUIRED_RISK_FIELDS = (
    "RISK_PER_TRADE_USD",
    "DAILY_SL_LIMIT_USD",
    "WEEKLY_SL_LIMIT_USD",
)
SENSITIVE_FIELD_RE = re.compile(r"(?:password|token|api[_-]?key|api[_-]?secret|secret|login)", re.I)
PLACEHOLDER_RE = re.compile(r"(?:replace|example|changeme|your[_ -]?(?:token|password|secret))", re.I)


class ConfigurationValidationError(Exception):
    """Raised when one or more configuration contract errors are found."""


class StrategyRegistryEntry(BaseModel):
    """Typed schema for one canonical strategies.yaml entry."""

    model_config = ConfigDict(extra="forbid", strict=True)

    symbol: StrictStr = Field(min_length=1)
    asset_class: StrictStr = Field(min_length=1)
    magic: StrictInt = Field(gt=0)
    account: StrictStr = Field(min_length=1)
    enabled: StrictBool


class StrategyConfigContract(BaseModel):
    """The baseline static fields required from every strategy config.py."""

    model_config = ConfigDict(extra="ignore", strict=True)

    STRATEGY_NAME: StrictStr = Field(min_length=1)
    SIGNAL_TIMEFRAME: StrictStr = Field(min_length=1)
    RISK_PER_TRADE_USD: float = Field(gt=0)
    DAILY_SL_LIMIT_USD: float | None = Field(default=None, gt=0)
    WEEKLY_SL_LIMIT_USD: float | None = Field(default=None, gt=0)


class AccountCredentials(BaseModel):
    """Typed schema for the secret credentials of an enabled account."""

    model_config = ConfigDict(extra="ignore", strict=True)

    login: StrictInt = Field(gt=0)
    password: StrictStr = Field(min_length=1)
    server: StrictStr = Field(min_length=1)
    mt5_path: StrictStr = Field(min_length=1)


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.YAMLError(f"duplicate YAML key {key!r} at line {key_node.start_mark.line + 1}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return yaml.load(stream, Loader=_UniqueKeyLoader)


def _literal_assignments(path: Path) -> dict[str, Any]:
    """Return simple module-level assignments without executing the file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        if not names:
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError):
            continue
        for name in names:
            values[name] = value
    return values


def _is_registry_proxy(expression: ast.AST, field: str) -> bool:
    """Recognise the approved non-executing identity proxy syntax."""
    if field == "STRATEGY_NAME":
        return (
            isinstance(expression, ast.Call)
            and isinstance(expression.func, ast.Attribute)
            and expression.func.attr == "upper"
            and isinstance(expression.func.value, ast.Name)
            and expression.func.value.id == "_strategy_id"
            and not expression.args and not expression.keywords
        )
    return (
        isinstance(expression, ast.Subscript)
        and isinstance(expression.value, ast.Name)
        and expression.value.id == "_meta"
        and isinstance(expression.slice, ast.Constant)
        and expression.slice.value == field.lower()
    )


def _static_strategy_config_values(
    path: Path, strategy_id: str, metadata: dict[str, Any], errors: list[str],
) -> dict[str, Any]:
    """Parse config data without importing a strategy or executing a proxy."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        errors.append(f"strategy {strategy_id!r}: invalid config.py: {exc}")
        return {}
    assignments: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assignments[node.target.id] = node.value

    values: dict[str, Any] = {}
    for name, expression in assignments.items():
        try:
            values[name] = ast.literal_eval(expression)
        except (ValueError, TypeError, SyntaxError):
            pass
    expected = {
        "STRATEGY_NAME": strategy_id.upper(),
        "SYMBOL": metadata["symbol"],
        "ASSET_CLASS": metadata["asset_class"],
        "MAGIC": metadata["magic"],
        "ACCOUNT": metadata["account"],
    }
    for field, expected_value in expected.items():
        expression = assignments.get(field)
        if expression is None:
            errors.append(f"strategy {strategy_id!r}: config.py missing {field}")
            continue
        if field in values:
            if values[field] != expected_value:
                errors.append(
                    f"strategy {strategy_id!r}: {field}={values[field]!r} != registry {expected_value!r}"
                )
        elif _is_registry_proxy(expression, field):
            values[field] = expected_value
        else:
            errors.append(
                f"strategy {strategy_id!r}: {field} must be a registry-backed proxy or literal registry value"
            )
    return values


def _strategy_class_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [node.name for node in tree.body if isinstance(node, ast.ClassDef) and node.name.endswith("Strategy")]


def _is_positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _validate_reset_command(account_id: str, metadata: Any, errors: list[str]) -> None:
    """Validate the non-secret, one-shot account lifecycle reset command."""
    if not isinstance(metadata, dict):
        errors.append(f"account {account_id!r}: metadata must be a mapping")
        return
    rules = metadata.get("risk_rules", {})
    if not isinstance(rules, dict):
        errors.append(f"account {account_id!r}: risk_rules must be a mapping")
        return
    requested = rules.get("reset_state_on_startup", False)
    if not isinstance(requested, bool):
        errors.append(f"account {account_id!r}: reset_state_on_startup must be boolean")
        return


def _find_plaintext_secret_fields(path: Path) -> list[str]:
    """Find credential-like literal assignments in non-secret config files."""
    findings: list[str] = []
    try:
        assignments = _literal_assignments(path)
    except (OSError, SyntaxError) as exc:
        return [f"cannot inspect {path}: {exc}"]
    for name, value in assignments.items():
        if SENSITIVE_FIELD_RE.search(name) and value not in (None, "", False):
            findings.append(f"{path}: plaintext-like field {name!r} belongs in secret_config.py")
    return findings


@dataclass(frozen=True)
class ValidationResult:
    accounts: tuple[str, ...]
    strategies: tuple[str, ...]
    strategy_metadata: dict[str, dict[str, Any]]


def validate_configuration(
    root: Path | str | None = None,
    *,
    secret_config_path: Path | str | None = None,
) -> ValidationResult:
    """Validate enabled strategies without importing MT5 or secret config.

    ``secret_config_path`` is useful for CI/tests. It defaults to
    ``<root>/secret_config.py`` and is parsed as Python literals, never run.
    """
    root_path = Path(root).resolve() if root else LIVE_TRADING_ROOT
    yaml_path = root_path / "strategies.yaml"
    accounts_path = root_path / "accounts.py"
    secret_path = Path(secret_config_path).resolve() if secret_config_path else root_path / "secret_config.py"
    errors: list[str] = []

    portfolio_path = root_path / "portfolio_config.py"
    try:
        portfolio_values = _literal_assignments(portfolio_path)
        market_guard_assets = portfolio_values.get("MARKET_GUARD_BY_ASSET")
    except (OSError, SyntaxError) as exc:
        errors.append(f"{portfolio_path}: cannot parse MARKET_GUARD_BY_ASSET: {exc}")
        portfolio_values = {}
        market_guard_assets = None
    if not isinstance(market_guard_assets, dict) or not market_guard_assets:
        errors.append(
            f"{portfolio_path}: MARKET_GUARD_BY_ASSET must be a non-empty literal mapping"
        )
        market_guard_assets = {}

    operational_bounds = {
        "POSITION_RISK_SLIPPAGE_TOLERANCE_R": lambda value: value >= 0,
        "MAX_MARGIN_UTILIZATION": lambda value: 0 < value < 1,
        "MARGIN_STRESS_STOP_MULTIPLIER": lambda value: value > 0,
        "MARGIN_ESTIMATE_BUFFER": lambda value: value >= 1,
    }
    for name, predicate in operational_bounds.items():
        if name not in portfolio_values:
            continue
        value = portfolio_values[name]
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not predicate(value)
        ):
            errors.append(f"{portfolio_path}: invalid {name}={value!r}")

    try:
        raw = _load_yaml(yaml_path)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationValidationError(f"{yaml_path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("strategies"), dict):
        errors.append(f"{yaml_path}: top-level 'strategies' must be a mapping")
        strategies: dict[str, Any] = {}
    else:
        strategies = raw["strategies"]

    try:
        accounts = _literal_assignments(accounts_path).get("ACCOUNTS")
    except (OSError, SyntaxError) as exc:
        errors.append(f"{accounts_path}: cannot parse ACCOUNTS: {exc}")
        accounts = None
    if not isinstance(accounts, dict) or not accounts:
        errors.append(f"{accounts_path}: ACCOUNTS must be a non-empty literal mapping")
        accounts = {}
    for account_id, metadata in accounts.items():
        if not isinstance(account_id, str) or not account_id:
            errors.append("accounts.py: account IDs must be non-empty strings")
            continue
        _validate_reset_command(account_id, metadata, errors)

    if not secret_path.is_file():
        errors.append(f"{secret_path}: missing; copy secret_config.example.py and set real credentials")
        secret_values: dict[str, Any] = {}
    else:
        try:
            secret_values = _literal_assignments(secret_path)
        except (OSError, SyntaxError) as exc:
            errors.append(f"{secret_path}: cannot parse secret config: {exc}")
            secret_values = {}
    secret_accounts = secret_values.get("ACCOUNTS")
    chat_ids = secret_values.get("CHAT_IDS")
    if not isinstance(secret_accounts, dict):
        errors.append(f"{secret_path}: ACCOUNTS must be a literal mapping")
        secret_accounts = {}
    if not isinstance(chat_ids, dict):
        errors.append(f"{secret_path}: CHAT_IDS must be a literal mapping")
        chat_ids = {}

    seen_magics: dict[int, str] = {}
    enabled_accounts: set[str] = set()
    normalized: dict[str, dict[str, Any]] = {}
    for strategy_id, metadata in strategies.items():
        if not isinstance(strategy_id, str) or not strategy_id:
            errors.append("strategies.yaml: strategy IDs must be non-empty strings")
            continue
        if not isinstance(metadata, dict):
            errors.append(f"strategy {strategy_id!r}: entry must be a mapping")
            continue
        missing = [field for field in REQUIRED_STRATEGY_FIELDS if field not in metadata]
        if missing:
            errors.append(f"strategy {strategy_id!r}: missing required fields: {', '.join(missing)}")
            continue
        try:
            entry = StrategyRegistryEntry.model_validate(metadata)
        except ValidationError as exc:
            errors.append(f"strategy {strategy_id!r}: invalid registry entry: {exc.errors(include_url=False)}")
            continue
        metadata = entry.model_dump()
        if metadata["asset_class"].upper() not in market_guard_assets:
            errors.append(
                f"strategy {strategy_id!r}: asset_class "
                f"{metadata['asset_class']!r} is not configured in "
                "portfolio_config.MARKET_GUARD_BY_ASSET"
            )
        magic = metadata["magic"]
        if magic in seen_magics:
            errors.append(f"duplicate magic {magic}: {strategy_id!r} and {seen_magics[magic]!r}")
        else:
            seen_magics[magic] = strategy_id
        if metadata["account"] not in accounts:
            errors.append(f"strategy {strategy_id!r}: unknown account {metadata['account']!r}")
        enabled = metadata["enabled"]

        config_path = root_path / "strategies" / strategy_id / "config.py"
        strategy_path = root_path / "strategies" / strategy_id / "strategy.py"
        if not config_path.is_file():
            errors.append(f"strategy {strategy_id!r}: missing config.py")
            config_values: dict[str, Any] = {}
        else:
            config_values = _static_strategy_config_values(
                config_path, strategy_id, metadata, errors,
            )
        if not strategy_path.is_file():
            errors.append(f"strategy {strategy_id!r}: missing strategy.py")
        else:
            try:
                classes = _strategy_class_names(strategy_path)
            except SyntaxError as exc:
                errors.append(f"strategy {strategy_id!r}: invalid strategy.py: {exc}")
            else:
                if len(classes) != 1:
                    errors.append(f"strategy {strategy_id!r}: strategy.py must define exactly one *Strategy class; found {classes}")
        try:
            StrategyConfigContract.model_validate(config_values)
        except ValidationError as exc:
            errors.append(f"strategy {strategy_id!r}: invalid strategy config: {exc.errors(include_url=False)}")
        if enabled:
            enabled_accounts.add(metadata["account"])
            if strategy_id not in chat_ids or not isinstance(chat_ids[strategy_id], int):
                errors.append(f"strategy {strategy_id!r}: missing integer CHAT_IDS routing entry")
        normalized[strategy_id] = dict(metadata)

    for account_id in sorted(enabled_accounts):
        credentials = secret_accounts.get(account_id)
        if not isinstance(credentials, dict):
            errors.append(f"enabled account {account_id!r}: missing credentials")
            continue
        try:
            typed_credentials = AccountCredentials.model_validate(credentials)
        except ValidationError as exc:
            errors.append(f"enabled account {account_id!r}: invalid credentials: {exc.errors(include_url=False)}")
            continue
        if PLACEHOLDER_RE.search(typed_credentials.password):
            errors.append(f"enabled account {account_id!r}: invalid credential field 'password'")

    ordinary_configs = [accounts_path, root_path / "portfolio_config.py"]
    ordinary_configs.extend((root_path / "strategies").glob("*/config.py"))
    for path in ordinary_configs:
        if path.is_file():
            errors.extend(_find_plaintext_secret_fields(path))

    if errors:
        raise ConfigurationValidationError("\n".join(f"- {error}" for error in errors))
    return ValidationResult(
        accounts=tuple(sorted(enabled_accounts)),
        strategies=tuple(sorted(strategy_id for strategy_id, meta in normalized.items() if meta["enabled"])),
        strategy_metadata=normalized,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate live-trading configuration without MT5.")
    parser.add_argument("--root", type=Path, default=LIVE_TRADING_ROOT, help="live_trading root (default: validator location)")
    parser.add_argument("--secret-config", type=Path, help="path to secret_config.py (default: <root>/secret_config.py)")
    args = parser.parse_args(argv)
    try:
        result = validate_configuration(args.root, secret_config_path=args.secret_config)
    except ConfigurationValidationError as exc:
        print("configuration invalid", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 1
    print("configuration valid")
    print(f"accounts: {', '.join(result.accounts)}")
    print(f"strategies: {', '.join(result.strategies)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
