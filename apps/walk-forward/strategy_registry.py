"""
Strategy registry for the walk-forward runtime.

Adding a new strategy should mean registering a factory here, not editing the
main orchestration loop.
"""

from __future__ import annotations

from collections.abc import Callable

from interfaces import Strategy
from silvermic_cpr_breakout_strategy import SilvermicCprBreakoutStrategy
from silvermic_v3_strategy import SilvermicCprBandV3Strategy

StrategyFactory = Callable[[], Strategy]

_STRATEGIES: dict[str, StrategyFactory] = {}


def register_strategy(strategy_id: str, factory: StrategyFactory) -> None:
    if not strategy_id:
        raise ValueError("strategy_id cannot be empty")
    _STRATEGIES[strategy_id] = factory


def create_strategy(strategy_id: str) -> Strategy:
    try:
        return _STRATEGIES[strategy_id]()
    except KeyError as exc:
        known = ", ".join(sorted(_STRATEGIES)) or "<none>"
        raise ValueError(f"Unknown strategy '{strategy_id}'. Registered strategies: {known}") from exc


def registered_strategy_ids() -> list[str]:
    return sorted(_STRATEGIES)


register_strategy(SilvermicCprBandV3Strategy.strategy_id, SilvermicCprBandV3Strategy)
register_strategy(SilvermicCprBreakoutStrategy.strategy_id, SilvermicCprBreakoutStrategy)
