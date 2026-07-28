"""CLI entrypoint: one process owns one MT5 hub and all its strategies."""

from __future__ import annotations

import argparse

from shared.config_validator import ConfigurationValidationError, validate_configuration
from shared.strategy_config_validator import StrategyConfigValidationError, validate_all_strategy_configs


def strategy_ids_for_hub(hub_id: str) -> list[str]:
    from shared.registry import registry

    return [
        strategy_id
        for strategy_id in registry.list_strategies(enabled_only=True)
        if registry.get_strategy(strategy_id)["account"] == hub_id
    ]


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Run all enabled strategies assigned to one hub")
    parser.add_argument("hub_id", help="hub_1, hub_2, hub_3, or hub_demo")
    parser.add_argument("--shadow", action="store_true", help="connect and recover without signal/order steps")
    args = parser.parse_args(argv)
    from accounts import list_accounts
    if args.hub_id not in list_accounts():
        raise SystemExit(f"unknown hub: {args.hub_id}")
    try:
        validation = validate_configuration()
        validate_all_strategy_configs()
    except (ConfigurationValidationError, StrategyConfigValidationError) as exc:
        raise SystemExit(f"configuration invalid:\n{exc}") from exc
    strategy_ids = strategy_ids_for_hub(args.hub_id)
    if not strategy_ids:
        raise SystemExit(f"hub {args.hub_id} has no enabled strategies")
    from hub_runtime import HubRuntime

    HubRuntime(args.hub_id, strategy_ids, shadow=args.shadow).run_forever()


if __name__ == "__main__":
    main()
