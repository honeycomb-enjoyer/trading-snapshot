# shared package
#
# Cross-cutting utilities shared across the whole live_trading project.
#
# Currently exposes the strategy portfolio registry:
#   from shared import registry
#   registry.list_strategies(account="hub_demo")
#   registry.get_strategy("audcad_h4_reversion")
#
# Importing `registry` triggers a one-time load + validation of
# strategies.yaml; a broken registry raises RegistryError at import
# time so misconfiguration surfaces at startup, not mid-trade.

from shared.registry import (
    StrategyRegistry,
    RegistryError,
    registry,
)

__all__ = ["StrategyRegistry", "RegistryError", "registry"]
