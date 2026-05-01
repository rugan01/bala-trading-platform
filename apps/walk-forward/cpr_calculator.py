"""
CPR (Central Pivot Range) Calculator
Computes Pivot, TC (Top Central), BC (Bottom Central), R1, S1, R2, S2
from previous day's OHLC.

Strategy V3 uses TC and BC for entry signals.
"""

import logging
from dataclasses import dataclass

from config import Config

logger = logging.getLogger(__name__)


@dataclass
class CPRLevels:
    pivot: float
    bc: float       # Bottom Central = (High + Low) / 2
    tc: float       # Top Central = (Pivot - BC) + Pivot = 2*Pivot - BC
    r1: float       # Resistance 1
    s1: float       # Support 1
    r2: float       # Resistance 2
    s2: float       # Support 2
    prev_high: float
    prev_low: float
    prev_close: float
    cpr_width_pct: float   # (TC - BC) / BC * 100 — narrow (<0.3%) = trending day


class CPRCalculator:
    """
    Computes CPR levels and provides proximity checks for touch detection.
    """

    def __init__(self, prev_ohlc: dict):
        """
        Args:
            prev_ohlc: {'high': float, 'low': float, 'close': float, ...}
        """
        self.levels = self._compute(prev_ohlc)
        self._log_levels()

    def _compute(self, ohlc: dict) -> CPRLevels:
        H = ohlc["high"]
        L = ohlc["low"]
        C = ohlc["close"]

        pivot = (H + L + C) / 3.0
        bc = (H + L) / 2.0
        tc = (2.0 * pivot) - bc

        # Ensure TC > BC (always true mathematically, but sanity check)
        if tc < bc:
            tc, bc = bc, tc

        r1 = (2.0 * pivot) - L
        s1 = (2.0 * pivot) - H
        r2 = pivot + (H - L)
        s2 = pivot - (H - L)

        cpr_width_pct = ((tc - bc) / bc) * 100.0 if bc > 0 else 0.0

        return CPRLevels(
            pivot=round(pivot, 2),
            bc=round(bc, 2),
            tc=round(tc, 2),
            r1=round(r1, 2),
            s1=round(s1, 2),
            r2=round(r2, 2),
            s2=round(s2, 2),
            prev_high=H,
            prev_low=L,
            prev_close=C,
            cpr_width_pct=round(cpr_width_pct, 4),
        )

    def _log_levels(self):
        lvl = self.levels
        width_label = "NARROW (trending)" if lvl.cpr_width_pct < 0.3 else "WIDE (ranging)"
        logger.info(
            f"[CPR] Pivot={lvl.pivot} | BC={lvl.bc} | TC={lvl.tc} | "
            f"R1={lvl.r1} | S1={lvl.s1} | "
            f"CPR Width={lvl.cpr_width_pct:.4f}% [{width_label}]"
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Touch / Proximity helpers
    # ──────────────────────────────────────────────────────────────────────────

    def is_near_bc(self, price: float) -> bool:
        """Returns True if price is within TOUCH_TOLERANCE_PCT of BC (long setup)."""
        return self._within_tolerance(price, self.levels.bc)

    def is_near_tc(self, price: float) -> bool:
        """Returns True if price is within TOUCH_TOLERANCE_PCT of TC (short setup)."""
        return self._within_tolerance(price, self.levels.tc)

    @staticmethod
    def _within_tolerance(price: float, level: float) -> bool:
        if level <= 0:
            return False
        return abs(price - level) / level <= Config.TOUCH_TOLERANCE_PCT

    # Convenience properties
    @property
    def pivot(self) -> float:
        return self.levels.pivot

    @property
    def bc(self) -> float:
        return self.levels.bc

    @property
    def tc(self) -> float:
        return self.levels.tc

    @property
    def r1(self) -> float:
        return self.levels.r1

    @property
    def s1(self) -> float:
        return self.levels.s1

    def summary(self) -> str:
        l = self.levels
        return (
            f"Pivot={l.pivot} | BC={l.bc} | TC={l.tc} | "
            f"R1={l.r1} | S1={l.s1} | CPR Width={l.cpr_width_pct:.3f}%"
        )
