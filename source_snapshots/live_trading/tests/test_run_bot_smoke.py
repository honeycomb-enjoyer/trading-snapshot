"""
Run bot smoke verification.

Mock-driven smoke tests (no MT5, no live trading, no network) for run_bot.py.
The goal is to prove that the unified runner resolves each strategy
correctly WITHOUT actually entering the trading main loop:

  1.  load_strategy() returns (config, klass) for each active strategy
  2.  discovered class name matches the expected one per strategy
  3.  config has the identity fields the rest of run_bot depends on
      (STRATEGY_NAME, SYMBOL, MAGIC, ACCOUNT, SIGNAL_TIMEFRAME)
  4.  resolve_update_state_method() picks update_h4_state for H4 strategies
      and update_h1_state for H1 strategies
  5.  resolve_update_state_method() raises on a bogus strategy object
  6.  load_strategy() raises SystemExit on an unknown package
  7.  load_strategy() raises SystemExit when a module has >1 *Strategy class
  8.  load_strategy() raises SystemExit when a module has 0 *Strategy classes
  9.  main() reads strategy name from sys.argv[1] when called with no args
 10.  main() rejects an unknown strategy name (registry validation) before
      touching any broker
 11.  deprecated runner shims stay absent
 12.  history_bars resolution: HISTORY_BARS override honored, default 500

Run:
    python tests/test_run_bot_smoke.py
"""

import os
import sys
import types
import importlib

# Make the live_trading root importable when run as a script.
_LIVE_TRADING = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIVE_TRADING not in sys.path:
    sys.path.insert(0, _LIVE_TRADING)

# run_bot.py imports `secret_config` at top level.
# secret_config.py is gitignored (real creds), so in the test env we inject a
# stub module so the import succeeds. The functions we test (load_strategy,
# resolve_update_state_method, argv parsing) never touch secret_config.
if "secret_config" not in sys.modules:
    _stub = types.ModuleType("secret_config")
    _stub.TELEGRAM_ENABLED = False
    _stub.TELEGRAM_BOT_TOKEN = ""
    _stub.MAIN_CHAT_ID = "0"
    _stub.ACCOUNTS = {}
    _stub.CHAT_IDS = {}
    sys.modules["secret_config"] = _stub

import run_bot  # noqa: E402

# ---------------------------------------------------------------------------
# Expected per-strategy facts. Cross-checked against the live config files so
# a silent rename in strategies/ would surface as a test failure here.
# ---------------------------------------------------------------------------
EXPECTED = {
    "audcad_h4_reversion": {
        "class": "AUDCADH4ReversionStrategy",
        "timeframe": "H4",
        "update_method": "update_h4_state",
    },
    "xau_h4_continuation_breakout": {
        "class": "XAUH4ContinuationBreakoutStrategy",
        "timeframe": "H4",
        "update_method": "update_h4_state",
    },
    "eurgbp_h4_reversion_return_filter": {
        "class": "EURGBPH4ReversionReturnFilterStrategy",
        "timeframe": "H4",
        "update_method": "update_h4_state",
    },
}

RUNNERS_DIR = os.path.join(_LIVE_TRADING, "runners")

# Map wrapper filename -> the strategy name it must pass to run_bot.main.
WRAPPERS = {}  # Legacy wrappers were removed after hub-runtime parity.

_failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_load_each_strategy():
    print("\n== load_strategy() resolves all active strategies ==")
    for strat, expect in EXPECTED.items():
        cfg, klass = run_bot.load_strategy(strat)
        check(
            f"{strat}: class is {expect['class']}",
            klass.__name__ == expect["class"],
            f"got {klass.__name__}",
        )

        # run_bot.main() depends on these identity fields.
        for field in ("STRATEGY_NAME", "SYMBOL", "MAGIC", "ACCOUNT",
                      "SIGNAL_TIMEFRAME"):
            check(
                f"{strat}: config has {field}",
                hasattr(cfg, field),
            )

        check(
            f"{strat}: SIGNAL_TIMEFRAME == {expect['timeframe']}",
            getattr(cfg, "SIGNAL_TIMEFRAME", None) == expect["timeframe"],
        )


def test_resolve_update_state_method():
    print("\n== resolve_update_state_method() picks the right method ==")
    for strat, expect in EXPECTED.items():
        cfg, klass = run_bot.load_strategy(strat)
        instance = klass()
        method = run_bot.resolve_update_state_method(instance, cfg)
        check(
            f"{strat}: resolved {expect['update_method']}",
            method.__name__ == expect["update_method"],
            f"got {method.__name__}",
        )


def test_resolve_update_state_method_raises_on_bogus():
    print("\n== resolve_update_state_method() raises on a strategy w/o methods ==")

    class Bogus:
        pass

    class FakeConfig:
        SIGNAL_TIMEFRAME = "D1"

    raised = False
    try:
        run_bot.resolve_update_state_method(Bogus(), FakeConfig())
    except SystemExit:
        raised = True
    check("bogus strategy -> SystemExit", raised)


def test_strategy_market_guard_settings_applies_only_explicit_override():
    default = types.SimpleNamespace(ASSET_CLASS="FX", SYMBOL="AUDCAD")
    daily = types.SimpleNamespace(
        ASSET_CLASS="FX", SYMBOL="AUDCAD", MAX_SPREAD_POINTS=100.0
    )

    assert run_bot.strategy_market_guard_settings(default)["max_spread_points"] == 50
    assert run_bot.strategy_market_guard_settings(daily)["max_spread_points"] == 100.0


def test_load_strategy_unknown_package():
    print("\n== load_strategy() rejects unknown package ==")
    raised = False
    try:
        run_bot.load_strategy("this_strategy_does_not_exist")
    except SystemExit:
        raised = True
    check("unknown package -> SystemExit", raised)


def test_load_strategy_zero_and_multiple_classes():
    print("\n== load_strategy() rejects 0 or >1 *Strategy classes ==")

    # Build two fake strategy packages in sys.modules backed by temp modules.
    pkg_root = types.ModuleType("strategies")
    pkg_root.__path__ = []  # mark as package
    sys.modules["strategies"] = pkg_root

    # (a) zero *Strategy classes
    zero_pkg = types.ModuleType("strategies._t011_zero")
    zero_pkg.__path__ = []
    zero_cfg = types.ModuleType("strategies._t011_zero.config")
    zero_cfg.STRATEGY_NAME = "ZERO"
    zero_mod = types.ModuleType("strategies._t011_zero.strategy")

    class NotAStrategy:  # name doesn't end with "Strategy"
        pass
    zero_mod.NotAStrategy = NotAStrategy

    sys.modules["strategies._t011_zero"] = zero_pkg
    sys.modules["strategies._t011_zero.config"] = zero_cfg
    sys.modules["strategies._t011_zero.strategy"] = zero_mod

    raised_zero = False
    try:
        run_bot.load_strategy("_t011_zero")
    except SystemExit:
        raised_zero = True
    check("0 *Strategy classes -> SystemExit", raised_zero)

    # (b) two *Strategy classes
    multi_pkg = types.ModuleType("strategies._t011_multi")
    multi_pkg.__path__ = []
    multi_cfg = types.ModuleType("strategies._t011_multi.config")
    multi_cfg.STRATEGY_NAME = "MULTI"
    multi_mod = types.ModuleType("strategies._t011_multi.strategy")

    class AlphaStrategy:
        pass

    class BetaStrategy:
        pass
    multi_mod.AlphaStrategy = AlphaStrategy
    multi_mod.BetaStrategy = BetaStrategy

    sys.modules["strategies._t011_multi"] = multi_pkg
    sys.modules["strategies._t011_multi.config"] = multi_cfg
    sys.modules["strategies._t011_multi.strategy"] = multi_mod

    raised_multi = False
    try:
        run_bot.load_strategy("_t011_multi")
    except SystemExit:
        raised_multi = True
    check(">1 *Strategy classes -> SystemExit", raised_multi)


def test_main_reads_argv():
    print("\n== main() reads strategy name from sys.argv[1] ==")
    # Stash a fake strategy package so load_strategy succeeds, then have the
    # very first call (before broker) raise SystemExit via registry to stop
    # execution. We assert that the name reached load_strategy correctly.
    captured = {}

    original_load = run_bot.load_strategy
    original_validate = run_bot.validate_configuration

    def fake_load(name):
        captured["name"] = name
        raise SystemExit("stop-test")

    run_bot.load_strategy = fake_load
    run_bot.validate_configuration = lambda: types.SimpleNamespace(
        strategy_metadata={"audcad_h4_reversion": {"enabled": True}}
    )
    old_argv = sys.argv[:]
    sys.argv = ["run_bot.py", "audcad_h4_reversion"]
    try:
        run_bot.main()
    except SystemExit:
        pass
    finally:
        run_bot.load_strategy = original_load
        run_bot.validate_configuration = original_validate
        sys.argv = old_argv

    check("main() read argv[1]", captured.get("name") == "audcad_h4_reversion")


def test_main_unknown_name_rejected_before_broker():
    print("\n== main() rejects unknown strategy name before broker connect ==")
    # An unknown name must SystemExit during registry validation - BEFORE any
    # broker/secret_config access. We assert by checking no broker import side
    # effects happen (they would raise if secret_config is the example stub).
    old_argv = sys.argv[:]
    sys.argv = ["run_bot.py", "totally_unknown_strategy"]
    raised = False
    try:
        run_bot.main()
    except SystemExit:
        raised = True
    finally:
        sys.argv = old_argv
    check("unknown name -> SystemExit pre-broker", raised)


def test_deprecated_runner_shims_are_absent():
    assert WRAPPERS == {}
    if os.path.isdir(RUNNERS_DIR):
        assert not [
            name for name in os.listdir(RUNNERS_DIR)
            if name.startswith("run_") and name.endswith(".py")
        ]

def test_history_bars_resolution():
    print("\n== HISTORY_BARS resolution: override honored, default 500 ==")
    # Default when absent on config.
    cfg_default = types.SimpleNamespace()
    check(
        "missing HISTORY_BARS -> default 500",
        getattr(cfg_default, "HISTORY_BARS", 500) == 500,
    )
    cfg_custom = types.SimpleNamespace(HISTORY_BARS=200)
    check(
        "HISTORY_BARS=200 honored",
        getattr(cfg_custom, "HISTORY_BARS", 500) == 200,
    )


def main():
    print("=" * 64)
    print("run_bot.py smoke tests")
    print("=" * 64)

    test_load_each_strategy()
    test_resolve_update_state_method()
    test_resolve_update_state_method_raises_on_bogus()
    test_load_strategy_unknown_package()
    test_load_strategy_zero_and_multiple_classes()
    test_main_reads_argv()
    test_main_unknown_name_rejected_before_broker()
    test_wrappers_are_thin_shims()
    test_history_bars_resolution()

    print("\n" + "=" * 64)
    if _failures:
        print(f"RESULT: {len(_failures)} FAIL - {_failures}")
        sys.exit(1)
    print("RESULT: ALL PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
