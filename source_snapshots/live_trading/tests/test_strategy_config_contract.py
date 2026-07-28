"""Regression tests for the P0-T09 machine-checkable strategy contract."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

import pytest


LIVE_ROOT = Path(__file__).resolve().parent.parent
if str(LIVE_ROOT) not in sys.path:
    sys.path.insert(0, str(LIVE_ROOT))

from shared.strategy_config_validator import (  # noqa: E402
    StrategyConfigValidationError,
    validate_all_strategy_configs,
    validate_strategy_config,
)


class StubRegistry:
    def get_strategy(self, strategy_id):
        assert strategy_id == "alpha"
        return {
            "symbol": "EURUSD", "asset_class": "FX",
            "magic": 101, "account": "hub_demo",
        }


def valid_config(**overrides):
    values = {
        "STRATEGY_NAME": "ALPHA",
        "SYMBOL": "EURUSD",
        "ASSET_CLASS": "FX",
        "MAGIC": 101,
        "ACCOUNT": "hub_demo",
        "SIGNAL_TIMEFRAME": "H1",
        "RISK_PER_TRADE_USD": 30,
        "DAILY_SL_LIMIT_USD": None,
        "WEEKLY_SL_LIMIT_USD": 150,
        "STOP_LOSS_MODEL": "ATR_MULTIPLIER",
        "STOP_LOSS": 1.25,
        "TAKE_PROFIT_MODEL": "CUSTOM",
        "TAKE_PROFIT": 0.5,
        "USE_BREAK_EVEN": False,
        "BREAK_EVEN_MODEL": "R_MULTIPLE",
        "BREAK_EVEN_TRIGGER": 0.0,
        "BREAK_EVEN_OFFSET": 0.0,
        "MAX_SLIPPAGE_AS_STOP_FRACTION": 0.25,
        # Unknown uppercase fields are reserved for strategy.py only.
        "ATR_PERIOD": 14,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_all_real_configs_pass_the_one_contract():
    assert validate_all_strategy_configs() == (
        "audcad_h4_reversion",
        "eurgbp_h4_reversion_return_filter",
        "xau_h4_continuation_breakout",
    )


def test_missing_required_field_is_rejected():
    config = valid_config()
    del config.STOP_LOSS
    with pytest.raises(StrategyConfigValidationError, match="STOP_LOSS"):
        validate_strategy_config("alpha", config, registry=StubRegistry())


def test_wrong_enum_is_rejected():
    with pytest.raises(StrategyConfigValidationError, match="STOP_LOSS_MODEL"):
        validate_strategy_config(
            "alpha", valid_config(STOP_LOSS_MODEL="ATR"), registry=StubRegistry()
        )


def test_strategy_spread_override_must_be_positive_when_present():
    validated = validate_strategy_config(
        "alpha", valid_config(MAX_SPREAD_POINTS=100.0), registry=StubRegistry()
    )
    assert validated.MAX_SPREAD_POINTS == 100.0
    with pytest.raises(StrategyConfigValidationError, match="MAX_SPREAD_POINTS"):
        validate_strategy_config(
            "alpha", valid_config(MAX_SPREAD_POINTS=0.0), registry=StubRegistry()
        )


def test_none_take_profit_requires_explicit_none_model():
    none_model = validate_strategy_config(
        "alpha",
        valid_config(TAKE_PROFIT_MODEL="NONE", TAKE_PROFIT=None),
        registry=StubRegistry(),
    )
    assert none_model.TAKE_PROFIT is None
    with pytest.raises(StrategyConfigValidationError, match="TAKE_PROFIT"):
        validate_strategy_config(
            "alpha", valid_config(TAKE_PROFIT=None), registry=StubRegistry()
        )


def test_custom_stop_requires_explicit_none_value():
    custom = validate_strategy_config(
        "alpha",
        valid_config(STOP_LOSS_MODEL="CUSTOM", STOP_LOSS=None),
        registry=StubRegistry(),
    )
    assert custom.STOP_LOSS is None
    with pytest.raises(StrategyConfigValidationError, match="STOP_LOSS"):
        validate_strategy_config(
            "alpha",
            valid_config(STOP_LOSS_MODEL="CUSTOM", STOP_LOSS=10.0),
            registry=StubRegistry(),
        )


def test_wrong_type_is_rejected():
    with pytest.raises(StrategyConfigValidationError, match="RISK_PER_TRADE_USD"):
        validate_strategy_config(
            "alpha", valid_config(RISK_PER_TRADE_USD="30"), registry=StubRegistry()
        )


def test_strategy_specific_policy_allows_only_upper_snake_case():
    contract = validate_strategy_config(
        "alpha", valid_config(BREAKOUT_LOOKBACK=20), registry=StubRegistry()
    )
    assert contract.model_extra["BREAKOUT_LOOKBACK"] == 20

    with pytest.raises(StrategyConfigValidationError, match="UPPER_SNAKE_CASE"):
        validate_strategy_config(
            "alpha", valid_config(custom_indicator_period=20), registry=StubRegistry()
        )


def test_identity_proxy_mismatch_is_rejected():
    with pytest.raises(StrategyConfigValidationError, match="identity proxy mismatch"):
        validate_strategy_config(
            "alpha", valid_config(MAGIC=999), registry=StubRegistry()
        )


def test_core_position_manager_does_not_need_strategy_specific_fields(monkeypatch):
    """ATR and custom-indicator parameters must not be a core dependency."""
    # The bundled unit-test runtime has no terminal SDK. PositionManager only
    # needs MT5 constants when it manages a real position, not at construction.
    monkeypatch.setitem(sys.modules, "MetaTrader5", SimpleNamespace())
    sys.modules.pop("core.position_manager", None)
    from core.position_manager import PositionManager

    config = valid_config()
    del config.ATR_PERIOD
    manager = PositionManager(
        broker=object(),
        config=config,
        state_manager=object(),
        trade_logger=object(),
        alerts=None,
    )
    assert manager.config is config
