"""
Config proxy verification: config.py to strategies.yaml consistency.

Every strategies/<name>/config.py is a thin proxy: it
reads SYMBOL / MAGIC / ACCOUNT from strategies.yaml via the shared
registry instead of hardcoding them. The engine (core/, guards/,
analytics/, monitoring/, run_bot) still reads config.SYMBOL etc. as
before - the proxy keeps that contract while collapsing duplication.

These tests guard the proxy contract so a future edit can't silently
re-introduce a hardcoded identity (which would re-open the drift window
between yaml and config.py):

  1.  every config.py exposes STRATEGY_NAME/SYMBOL/MAGIC/ACCOUNT
      (the contract run_bot depends on)
  2.  each config's SYMBOL/MAGIC/ACCOUNT equals the registry's value
      for that strategy (no drift, single source of truth)
  3.  timeframe is still owned by config.py (NOT in the registry - it's
      behavior, not identity)
  4.  mutating the registry snapshot does not leak into config (config
      binds values once at import, not on every attribute access)
  5.  config still owns RISK_PER_TRADE_USD (risk lives in config.py by
      design - see the identity-vs-behavior split in strategies.yaml)

Run:
    python tests/test_config_proxy.py
"""

import os
import sys
import importlib

# Make the live_trading package importable when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.registry import registry


# The real strategies and the STRATEGY_NAME each config.py must expose.
# Keys mirror strategies/* directory names (the registry identity keys).
STRATEGIES = {
    "audcad_h4_reversion": "AUDCAD_H4_REVERSION",
    "xau_h4_continuation_breakout": "XAU_H4_CONTINUATION_BREAKOUT",
    "eurgbp_h4_reversion_return_filter": "EURGBP_H4_REVERSION_RETURN_FILTER",
}


def _load_config(name):
    """Import (or re-import) strategies/<name>/config and return it."""
    return importlib.import_module(f"strategies.{name}.config")


# =====================================
# TESTS
# =====================================
def test_each_config_exposes_identity_fields():
    """run_bot.main depends on these attributes existing on config."""
    print("\n== every config.py exposes the identity contract ==")
    for name, strat_name in STRATEGIES.items():
        cfg = _load_config(name)
        for field in ("STRATEGY_NAME", "SYMBOL", "ASSET_CLASS", "MAGIC", "ACCOUNT"):
            assert hasattr(cfg, field), f"{name}: missing config.{field}"
        assert cfg.STRATEGY_NAME == strat_name, (
            f"{name}: STRATEGY_NAME={cfg.STRATEGY_NAME!r} expected {strat_name!r}"
        )
        print(f"  PASS - {name}: identity fields present")
    print("PASS - all active configs expose the identity contract")


def test_config_identity_matches_registry():
    """The single-source-of-truth check: config.SYMBOL/MAGIC/ACCOUNT
    must equal registry.get_strategy(name). Drift here means someone
    hardcoded identity back into a config.py."""
    print("\n== config identity == registry identity (no drift) ==")
    for name in STRATEGIES:
        cfg = _load_config(name)
        meta = registry.get_strategy(name)
        assert cfg.SYMBOL == meta["symbol"], (
            f"{name}: config.SYMBOL={cfg.SYMBOL!r} but yaml={meta['symbol']!r}"
        )
        assert cfg.ASSET_CLASS == meta["asset_class"], (
            f"{name}: config.ASSET_CLASS={cfg.ASSET_CLASS!r} "
            f"but yaml={meta['asset_class']!r}"
        )
        assert cfg.MAGIC == meta["magic"], (
            f"{name}: config.MAGIC={cfg.MAGIC!r} but yaml={meta['magic']!r}"
        )
        assert cfg.ACCOUNT == meta["account"], (
            f"{name}: config.ACCOUNT={cfg.ACCOUNT!r} but yaml={meta['account']!r}"
        )
        print(
            f"  PASS - {name}: symbol/magic/account match yaml "
            f"({cfg.SYMBOL}/{cfg.MAGIC}/{cfg.ACCOUNT})"
        )
    print("PASS - config identity matches registry, no drift")


def test_timeframe_owned_by_config_not_registry():
    """timeframe is BEHAVIOR (read by strategy.py to load bars), not
    identity. It must live in config.SIGNAL_TIMEFRAME and must NOT
    appear in the registry snapshot."""
    print("\n== timeframe stays in config, out of the registry ==")
    for name in STRATEGIES:
        cfg = _load_config(name)
        meta = registry.get_strategy(name)
        assert hasattr(cfg, "SIGNAL_TIMEFRAME"), (
            f"{name}: SIGNAL_TIMEFRAME missing from config"
        )
        assert cfg.SIGNAL_TIMEFRAME in ("M30", "H1", "H4"), (
            f"{name}: unexpected SIGNAL_TIMEFRAME={cfg.SIGNAL_TIMEFRAME!r}"
        )
        assert "timeframe" not in meta, (
            f"{name}: timeframe leaked into registry identity: {meta}"
        )
        print(f"  PASS - {name}: TF={cfg.SIGNAL_TIMEFRAME} (config only)")
    print("PASS - timeframe owned by config, absent from registry")


def test_config_binds_identity_once():
    """config.SYMBOL etc. are bound at import time from the registry
    snapshot, then frozen. Mutating the registry's returned dict after
    the fact must NOT change config attributes - otherwise the engine
    would see flapping identity during a run."""
    name = "audcad_h4_reversion"
    cfg = _load_config(name)

    original_symbol = cfg.SYMBOL
    original_magic = cfg.MAGIC

    # Tamper with a fresh snapshot from the registry.
    poisoned = registry.get_strategy(name)
    poisoned["symbol"] = "HACKED"
    poisoned["magic"] = 999999

    # config must be unaffected - it bound its own values at import.
    assert cfg.SYMBOL == original_symbol, cfg.SYMBOL
    assert cfg.MAGIC == original_magic, cfg.MAGIC
    print("PASS - config identity bound once at import, tamper-proof")


def test_risk_owned_by_config_not_registry():
    """The risk block is BEHAVIOR/TUNING and stays in config.py
    (RISK_PER_TRADE_USD, DAILY/WEEKLY_SL_LIMIT_USD) next to the strategy
    that consumes it. It must NOT be in the registry snapshot."""
    print("\n== risk block stays in config, out of the registry ==")
    for name in STRATEGIES:
        cfg = _load_config(name)
        meta = registry.get_strategy(name)
        assert hasattr(cfg, "RISK_PER_TRADE_USD"), (
            f"{name}: RISK_PER_TRADE_USD missing from config"
        )
        assert isinstance(cfg.RISK_PER_TRADE_USD, (int, float)) and \
            cfg.RISK_PER_TRADE_USD > 0, (
            f"{name}: bad RISK_PER_TRADE_USD={cfg.RISK_PER_TRADE_USD!r}"
        )
        assert "risk_per_trade_usd" not in meta, (
            f"{name}: risk leaked into registry identity: {meta}"
        )
        print(
            f"  PASS - {name}: risk={cfg.RISK_PER_TRADE_USD} "
            f"(config only)"
        )
    print("PASS - risk owned by config, absent from registry")


# =====================================
# MAIN
# =====================================
if __name__ == "__main__":
    print("=" * 60)
    print("Config proxy consistency verification")
    print("=" * 60)

    test_each_config_exposes_identity_fields()
    test_config_identity_matches_registry()
    test_timeframe_owned_by_config_not_registry()
    test_config_binds_identity_once()
    test_risk_owned_by_config_not_registry()

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)
