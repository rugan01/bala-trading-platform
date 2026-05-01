"""
Position lifecycle plans for the walk-forward engine.

The first plan intentionally mirrors the existing SILVERMIC behavior:
- 2 lots total
- book 1 lot at T1
- trail the remaining lot
- force close at EOD
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from config import Config


@dataclass(frozen=True)
class PositionPlan:
    plan_id: str
    display_name: str
    total_lots: int
    t1_exit_lots: int
    lot_size: float
    fee_per_lot: float
    trail_after_t1: bool = True
    force_close_enabled: bool = True

    def validate(self) -> None:
        if self.total_lots <= 0:
            raise ValueError("total_lots must be positive")
        if self.t1_exit_lots < 0:
            raise ValueError("t1_exit_lots cannot be negative")
        if self.t1_exit_lots > self.total_lots:
            raise ValueError("t1_exit_lots cannot exceed total_lots")
        if self.lot_size <= 0:
            raise ValueError("lot_size must be positive")
        if self.fee_per_lot < 0:
            raise ValueError("fee_per_lot cannot be negative")

    @property
    def remaining_lots_after_t1(self) -> int:
        return self.total_lots - self.t1_exit_lots


PositionPlanFactory = Callable[[], PositionPlan]

_POSITION_PLANS: dict[str, PositionPlanFactory] = {}


def register_position_plan(plan_id: str, factory: PositionPlanFactory) -> None:
    if not plan_id:
        raise ValueError("plan_id cannot be empty")
    _POSITION_PLANS[plan_id] = factory


def create_position_plan(plan_id: str) -> PositionPlan:
    try:
        plan = _POSITION_PLANS[plan_id]()
    except KeyError as exc:
        known = ", ".join(sorted(_POSITION_PLANS)) or "<none>"
        raise ValueError(f"Unknown position plan '{plan_id}'. Registered plans: {known}") from exc
    plan.validate()
    return plan


def registered_position_plan_ids() -> list[str]:
    return sorted(_POSITION_PLANS)


def partial_t1_trail_plan() -> PositionPlan:
    return PositionPlan(
        plan_id="partial_t1_trail",
        display_name="Partial T1 Exit + SuperTrend Trail",
        total_lots=Config.LOTS,
        t1_exit_lots=1,
        lot_size=Config.LOT_SIZE,
        fee_per_lot=Config.FEES_PER_LOT,
        trail_after_t1=True,
        force_close_enabled=True,
    )


def full_t1_exit_plan() -> PositionPlan:
    return PositionPlan(
        plan_id="full_t1_exit",
        display_name="Full Exit At T1",
        total_lots=Config.LOTS,
        t1_exit_lots=Config.LOTS,
        lot_size=Config.LOT_SIZE,
        fee_per_lot=Config.FEES_PER_LOT,
        trail_after_t1=False,
        force_close_enabled=True,
    )


def single_lot_t1_exit_plan() -> PositionPlan:
    return PositionPlan(
        plan_id="single_lot_t1_exit",
        display_name="Single Lot Full Exit At T1",
        total_lots=1,
        t1_exit_lots=1,
        lot_size=Config.LOT_SIZE,
        fee_per_lot=Config.FEES_PER_LOT,
        trail_after_t1=False,
        force_close_enabled=True,
    )


register_position_plan("partial_t1_trail", partial_t1_trail_plan)
register_position_plan("full_t1_exit", full_t1_exit_plan)
register_position_plan("single_lot_t1_exit", single_lot_t1_exit_plan)
