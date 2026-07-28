from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
import yaml


LIVE_ROOT = Path(__file__).resolve().parent.parent
if str(LIVE_ROOT) not in sys.path:
    sys.path.insert(0, str(LIVE_ROOT))

from shared.registry import RegistryError, StrategyRegistry  # noqa: E402


RETAINED_STRATEGIES = {
    "audcad_h4_reversion",
    "eurgbp_h4_reversion_return_filter",
    "xau_h4_continuation_breakout",
}


def _write_yaml(path: Path, strategies: dict) -> None:
    path.write_text(
        yaml.safe_dump({"strategies": strategies}, sort_keys=False),
        encoding="utf-8",
    )


def _valid_entry(
    *,
    symbol: str = "GBPUSD",
    asset_class: str = "FX",
    magic: object = 100001,
    account: str = "hub_demo",
    enabled: object = True,
) -> dict:
    return {
        "symbol": symbol,
        "asset_class": asset_class,
        "magic": magic,
        "account": account,
        "enabled": enabled,
    }


def _fake_accounts(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> None:
    fake = types.ModuleType("accounts")
    fake.list_accounts = lambda: list(names)
    monkeypatch.setitem(sys.modules, "accounts", fake)


def test_loads_retained_portfolio_strategies():
    from shared import registry as reg

    assert set(reg.list_strategies(enabled_only=False)) == RETAINED_STRATEGIES
    assert len(reg) == 3


def test_get_strategy_returns_identity_only_dict():
    from shared import registry as reg

    meta = reg.get_strategy("audcad_h4_reversion")
    assert meta["symbol"] == "AUDCAD"
    assert meta["asset_class"] == "FX"
    assert meta["magic"] == 46001
    assert meta["account"] == "hub_demo"
    assert meta["enabled"] is True
    assert "timeframe" not in meta
    assert "risk_per_trade_usd" not in meta


def test_list_strategies_filters_by_account():
    from shared import registry as reg

    assert set(reg.list_strategies(account="hub_demo")) == RETAINED_STRATEGIES
    assert reg.list_strategies(account="hub_1") == []


def test_enabled_flag_filters(tmp_path, monkeypatch):
    _fake_accounts(monkeypatch, ["hub_demo"])
    yaml_path = tmp_path / "strategies.yaml"
    _write_yaml(
        yaml_path,
        {
            "alive": _valid_entry(magic=100001, enabled=True),
            "paused": _valid_entry(symbol="EURUSD", magic=100002, enabled=False),
        },
    )

    reg = StrategyRegistry(yaml_path=yaml_path)

    assert reg.list_strategies() == ["alive"]
    assert reg.list_strategies(enabled_only=False) == ["alive", "paused"]


def test_get_strategy_unknown_raises_keyerror():
    from shared import registry as reg

    with pytest.raises(KeyError, match="audcad_h4_reversion"):
        reg.get_strategy("does_not_exist")


def test_get_strategy_returns_defensive_copy():
    from shared import registry as reg

    meta = reg.get_strategy("audcad_h4_reversion")
    meta["symbol"] = "HACKED"
    meta["magic"] = 999999

    fresh = reg.get_strategy("audcad_h4_reversion")
    assert fresh["symbol"] == "AUDCAD"
    assert fresh["magic"] == 46001


def test_list_magics_maps_all_retained_strategies():
    from shared import registry as reg

    assert reg.list_magics() == {
        46001: "audcad_h4_reversion",
        52001: "eurgbp_h4_reversion_return_filter",
        53001: "xau_h4_continuation_breakout",
    }


def test_duplicate_magic_rejected(tmp_path, monkeypatch):
    _fake_accounts(monkeypatch, ["hub_demo"])
    yaml_path = tmp_path / "strategies.yaml"
    _write_yaml(
        yaml_path,
        {
            "a": _valid_entry(magic=200001),
            "b": _valid_entry(symbol="EURUSD", magic=200001),
        },
    )

    with pytest.raises(RegistryError, match="duplicate magic"):
        StrategyRegistry(yaml_path=yaml_path)


def test_unknown_account_rejected(tmp_path, monkeypatch):
    _fake_accounts(monkeypatch, ["hub_demo"])
    yaml_path = tmp_path / "strategies.yaml"
    _write_yaml(yaml_path, {"ghost": _valid_entry(magic=300001, account="hub_ghost")})

    with pytest.raises(RegistryError, match="hub_ghost"):
        StrategyRegistry(yaml_path=yaml_path)


def test_missing_required_field_rejected(tmp_path, monkeypatch):
    _fake_accounts(monkeypatch, ["hub_demo"])
    yaml_path = tmp_path / "strategies.yaml"
    entry = _valid_entry()
    entry.pop("magic")
    _write_yaml(yaml_path, {"incomplete": entry})

    with pytest.raises(RegistryError, match="magic"):
        StrategyRegistry(yaml_path=yaml_path)


@pytest.mark.parametrize(
    "entry, message",
    [
        (_valid_entry(magic=0), "magic"),
        (_valid_entry(symbol="", magic=400001), "symbol"),
        (_valid_entry(magic=500001, enabled="yes"), "enabled"),
        (_valid_entry(magic="47001"), "magic"),
    ],
)
def test_invalid_identity_values_are_rejected(tmp_path, monkeypatch, entry, message):
    _fake_accounts(monkeypatch, ["hub_demo"])
    yaml_path = tmp_path / "strategies.yaml"
    _write_yaml(yaml_path, {"bad": entry})

    with pytest.raises(RegistryError, match=message):
        StrategyRegistry(yaml_path=yaml_path)


def test_missing_file_rejected(tmp_path, monkeypatch):
    _fake_accounts(monkeypatch, ["hub_demo"])

    with pytest.raises(RegistryError, match="not found"):
        StrategyRegistry(yaml_path=tmp_path / "nope.yaml")


def test_missing_strategies_key_rejected(tmp_path, monkeypatch):
    _fake_accounts(monkeypatch, ["hub_demo"])
    yaml_path = tmp_path / "strategies.yaml"
    yaml_path.write_text("meta:\n  version: 1\n", encoding="utf-8")

    with pytest.raises(RegistryError, match="strategies"):
        StrategyRegistry(yaml_path=yaml_path)


def test_empty_strategies_rejected(tmp_path, monkeypatch):
    _fake_accounts(monkeypatch, ["hub_demo"])
    yaml_path = tmp_path / "strategies.yaml"
    _write_yaml(yaml_path, {})

    with pytest.raises(RegistryError, match="empty"):
        StrategyRegistry(yaml_path=yaml_path)


def test_singleton_loads():
    for module_name in list(sys.modules):
        if module_name in {"shared", "shared.registry"}:
            del sys.modules[module_name]

    from shared import registry as reg

    assert len(reg) == 3
