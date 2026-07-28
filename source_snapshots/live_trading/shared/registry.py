# shared/registry.py
#
# Single source of truth loader for the strategy portfolio.
#
# Reads strategies.yaml from the live_trading root once at startup,
# validates it (unique magics, known accounts, required fields), and
# exposes read-only accessors used by runners / equity monitor /
# profit target and future magic-number allocation.
#
# This registry owns only strategy IDENTITY - the small set of stable
# facts the rest of the system must know about every strategy:
#   symbol, asset_class, magic, account, enabled
#
# It deliberately does NOT hold trading parameters. timeframe, ATR/RANGE
# lookbacks, SL/TP models, break-even tuning, MAX_LOT, and the risk block
# (RISK_PER_TRADE_USD, DAILY/WEEKLY_SL_LIMIT_USD) are BEHAVIOR/TUNING and
# stay in strategies/<name>/config.py, next to the strategy that consumes
# them. Keeping identity and behavior apart is what makes this file a
# stable, rarely-edited portfolio overview rather than a second config.

from pathlib import Path

import yaml

# Resolve live_trading/ as the parent of the shared/ package, regardless
# of the caller's CWD. strategies.yaml always lives next to accounts.py.
_LIVE_TRADING_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STRATEGIES_YAML = _LIVE_TRADING_ROOT / "strategies.yaml"


# Fields every strategy entry MUST define. Anything missing is a config
# bug that should fail startup loudly, not silently fall back.
# NOTE: this is identity only - symbol/asset_class/magic/account/enabled. timeframe
# and risk live in config.py (behavior/tuning), not here.
REQUIRED_FIELDS = (
    "symbol",
    "asset_class",
    "magic",
    "account",
    "enabled",
)


class RegistryError(Exception):
    """Raised when strategies.yaml is structurally invalid.

    Distinct from generic Exception so callers (runners, tests) can
    catch registry problems specifically and surface them as a startup
    config failure rather than a runtime/trade failure.
    """


class StrategyRegistry:
    """Loads strategies.yaml once and validates it.

    Validation rules (enforced in __init__, before any trade logic runs):
      1. `strategies` top-level mapping exists and is non-empty
      2. each entry has all REQUIRED_FIELDS (symbol/asset_class/magic/account/enabled)
      3. `magic` is a positive int and unique across the portfolio
         (duplicates break ADR-005 idempotency + trade reconciliation)
      4. `account` is a name that exists in accounts.ACCOUNTS
      5. `enabled` is a bool
      6. `symbol` is a non-empty string

    The accounts module is imported lazily inside __init__ so that
    tests can inject a fake `accounts` module via monkeypatching before
    constructing the registry. Importing at module top level would lock
    the real accounts.py in place before tests could replace it.
    """

    def __init__(self, yaml_path=None):
        self.yaml_path = Path(yaml_path) if yaml_path else DEFAULT_STRATEGIES_YAML

        if not self.yaml_path.is_file():
            raise RegistryError(
                f"strategies.yaml not found at {self.yaml_path}. "
                f"Create it or pass yaml_path explicitly."
            )

        # Load with safe_load: strategies.yaml must never contain
        # arbitrary Python objects, only plain data.
        with open(self.yaml_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        if not isinstance(raw, dict) or "strategies" not in raw:
            raise RegistryError(
                f"{self.yaml_path}: top-level 'strategies' mapping is missing."
            )

        strategies = raw["strategies"]
        if not isinstance(strategies, dict) or not strategies:
            raise RegistryError(
                f"{self.yaml_path}: 'strategies' must be a non-empty mapping "
                f"of name -> metadata."
            )

        # Lazily import accounts so tests can substitute a fake.
        import accounts  # noqa: WPS433 (local import is intentional)

        known_accounts = set(accounts.list_accounts())

        # --- validate each entry -------------------------------------
        seen_magics = {}
        normalized = {}

        for name, meta in strategies.items():
            if not isinstance(meta, dict):
                raise RegistryError(
                    f"strategy {name!r}: entry must be a mapping, got "
                    f"{type(meta).__name__}."
                )

            # 2. required fields
            missing = [f for f in REQUIRED_FIELDS if f not in meta]
            if missing:
                raise RegistryError(
                    f"strategy {name!r}: missing required field(s): "
                    f"{', '.join(missing)}."
                )

            magic = meta["magic"]
            account = meta["account"]
            symbol = meta["symbol"]
            asset_class = meta["asset_class"]
            enabled = meta["enabled"]

            # 3. magic uniqueness + type
            if not isinstance(magic, int) or isinstance(magic, bool):
                raise RegistryError(
                    f"strategy {name!r}: magic must be an int, got "
                    f"{type(magic).__name__} ({magic!r})."
                )
            if magic <= 0:
                raise RegistryError(
                    f"strategy {name!r}: magic must be positive, got {magic}."
                )
            if magic in seen_magics:
                raise RegistryError(
                    f"duplicate magic {magic}: strategy {name!r} reuses "
                    f"magic already assigned to {seen_magics[magic]!r}. "
                    f"Magic numbers must be portfolio-unique (ADR-005)."
                )
            seen_magics[magic] = name

            # 4. account exists in accounts.py
            if account not in known_accounts:
                raise RegistryError(
                    f"strategy {name!r}: account {account!r} not found in "
                    f"accounts.ACCOUNTS. Available: {sorted(known_accounts)}."
                )

            # 5. enabled is bool
            if not isinstance(enabled, bool):
                raise RegistryError(
                    f"strategy {name!r}: 'enabled' must be bool, got "
                    f"{type(enabled).__name__} ({enabled!r})."
                )

            # 6. symbol is a non-empty string
            if not isinstance(symbol, str) or not symbol:
                raise RegistryError(
                    f"strategy {name!r}: symbol must be a non-empty string, "
                    f"got {type(symbol).__name__} ({symbol!r})."
                )

            # 7. asset class is explicit; market-guard code never guesses it
            # from a broker-specific symbol spelling or suffix.
            if not isinstance(asset_class, str) or not asset_class.strip():
                raise RegistryError(
                    f"strategy {name!r}: asset_class must be a non-empty "
                    f"string, got {type(asset_class).__name__} "
                    f"({asset_class!r})."
                )

            # Store a defensive shallow copy so callers can mutate the
            # returned dict without corrupting the registry's cache.
            normalized[name] = dict(meta)

        # Frozen snapshot: swap-in only after full validation passed,
        # so a mid-validation RegistryError leaves no half-built state.
        self._strategies = normalized

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------
    def list_strategies(self, account=None, enabled_only=True):
        """Return strategy names matching the filter.

        Args:
            account: if set, restrict to strategies whose `account`
                matches. None = all accounts.
            enabled_only: if True (default), drop strategies with
                enabled: false. Disabled strategies are still valid and
                still validated at load - they're just skipped by code
                that only cares about what currently trades.

        Returns a sorted list of strategy names (not metadata). Use
        get_strategy(name) for the metadata of each.
        """
        names = []
        for name, meta in self._strategies.items():
            if account is not None and meta["account"] != account:
                continue
            if enabled_only and not meta["enabled"]:
                continue
            names.append(name)
        return sorted(names)

    def get_strategy(self, name):
        """Return a shallow copy of the metadata dict for `name`.

        Returns a copy so callers can freely mutate without affecting
        the registry or other consumers. Raises KeyError for an unknown
        name, with the available names listed to help debugging.
        """
        if name not in self._strategies:
            raise KeyError(
                f"Unknown strategy: {name!r}. "
                f"Available: {self.list_strategies(enabled_only=False)}"
            )
        return dict(self._strategies[name])

    def list_magics(self):
        """Return {magic: strategy_name} for the whole portfolio.

        Used by reconciliation, magic-number allocation and any
        component that needs to map a broker ticket back to a strategy.
        """
        return {meta["magic"]: name for name, meta in self._strategies.items()}

    def __len__(self):
        return len(self._strategies)

    def __contains__(self, name):
        return name in self._strategies

    def __repr__(self):
        return (
            f"StrategyRegistry(strategies={len(self)}, "
            f"yaml={self.yaml_path.name!r})"
        )


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------
# Loaded once on first import. Re-raising RegistryError here means the
# very first `from shared.registry import registry` in any runner will
# fail loudly at startup if strategies.yaml is broken - fail-fast behavior is intentional.
registry = StrategyRegistry()
