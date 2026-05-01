"""
Signal Detector — SILVERMIC V3 (CPR Band TC/BC Rejection)

Logic:
  - Monitor BC for LONG signals, TC for SHORT signals
  - A "touch" = candle low/high comes within TOUCH_TOLERANCE_PCT of the level
  - Valid entry = 2nd touch with at least MIN_TOUCH_GAP_BARS between touch 1 and touch 2
  - Entry price = close of the 2nd-touch bar (candle that completes the pattern)
  - SL = SuperTrend(5, 3.0); fallback = level ± 0.8%
  - T1 = TC (for longs), BC (for shorts)  [50% position exit]
  - Trailing starts after T1 using SuperTrend(5, 1.5)
  - HTF Filter: OFF
"""

import logging
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

from config import Config
from cpr_calculator import CPRCalculator
from supertrend import SuperTrend

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    direction: str          # "Long" or "Short"
    entry_price: float      # close of signal bar
    sl: float               # initial stop-loss level
    t1: float               # first target (50% exit level)
    bc: float               # CPR bottom central (for context)
    tc: float               # CPR top central (for context)
    pivot: float
    timestamp: datetime
    touch_level: float      # BC for long, TC for short
    sl_source: str          # "SuperTrend" or "Fallback"
    bar_index: int


@dataclass
class TouchState:
    """Tracks touch attempts for a given CPR level."""
    level: float
    direction: str           # "Long" (BC) or "Short" (TC)
    touch_count: int = 0
    last_touch_bar: int = -999
    signal_fired: bool = False  # prevent re-entry on same level until reset

    def reset(self):
        self.touch_count = 0
        self.last_touch_bar = -999
        self.signal_fired = False


class SignalDetector:
    """
    Processes 15m candles and emits entry signals when V3 rules are satisfied.
    """

    def __init__(self, cpr: CPRCalculator, warmup_candles: list[dict]):
        self.cpr = cpr
        self.bar_index: int = 0

        # Two SuperTrend instances
        self.st_sl = SuperTrend(Config.ST_SL_LENGTH, Config.ST_SL_FACTOR)
        self.st_trail = SuperTrend(Config.ST_TRAIL_LENGTH, Config.ST_TRAIL_FACTOR)

        # Touch tracking per side
        self.bc_touch = TouchState(level=cpr.bc, direction="Long")
        self.tc_touch = TouchState(level=cpr.tc, direction="Short")

        # Warm up SuperTrend with historical data
        if warmup_candles:
            self.st_sl.warmup(warmup_candles)
            self.st_trail.warmup(warmup_candles)
            self.bar_index = len(warmup_candles)
            logger.info(
                f"[Detector] Warmed up with {len(warmup_candles)} bars | "
                f"ST_SL={self.st_sl} | ST_Trail={self.st_trail}"
            )
        else:
            logger.warning("[Detector] No warm-up candles — SuperTrend needs first few live bars to stabilize")

    # ──────────────────────────────────────────────────────────────────────────
    # Main entry point
    # ──────────────────────────────────────────────────────────────────────────

    def process_candle(self, candle: dict) -> Optional[Signal]:
        """
        Process a completed 15m candle.
        Returns a Signal if entry conditions are met, else None.
        """
        self.bar_index += 1
        idx = self.bar_index

        # Update both SuperTrend instances
        self.st_sl.update(candle)
        self.st_trail.update(candle)

        H = candle["high"]
        L = candle["low"]
        C = candle["close"]
        ts = candle["timestamp"]

        logger.debug(
            f"[Bar {idx}] {ts.strftime('%H:%M')} | "
            f"H={H} L={L} C={C} | "
            f"BC_touches={self.bc_touch.touch_count} TC_touches={self.tc_touch.touch_count}"
        )

        # Check BC touches for LONG setup
        long_signal = self._check_touch(
            touch_state=self.bc_touch,
            bar_low=L,
            bar_high=H,
            close=C,
            timestamp=ts,
            bar_idx=idx,
            use_low=True,  # BC touch: check if candle LOW dips to BC
        )

        if long_signal:
            return long_signal

        # Check TC touches for SHORT setup
        short_signal = self._check_touch(
            touch_state=self.tc_touch,
            bar_low=L,
            bar_high=H,
            close=C,
            timestamp=ts,
            bar_idx=idx,
            use_low=False,  # TC touch: check if candle HIGH reaches TC
        )

        return short_signal

    # ──────────────────────────────────────────────────────────────────────────
    # Touch detection logic
    # ──────────────────────────────────────────────────────────────────────────

    def _check_touch(
        self,
        touch_state: TouchState,
        bar_low: float,
        bar_high: float,
        close: float,
        timestamp: datetime,
        bar_idx: int,
        use_low: bool,
    ) -> Optional[Signal]:
        """
        Check if this candle touches the level and whether entry conditions are met.

        Args:
            use_low: True for BC (check bar low), False for TC (check bar high)
        """
        if touch_state.signal_fired:
            return None  # already fired a signal for this level today

        level = touch_state.level
        test_price = bar_low if use_low else bar_high

        # Is this bar touching the level?
        if not self._is_touch(test_price, level):
            return None

        # First touch
        if touch_state.touch_count == 0:
            touch_state.touch_count = 1
            touch_state.last_touch_bar = bar_idx
            logger.info(
                f"[Detector] Touch 1/{2} on {touch_state.direction} level "
                f"({level:.2f}) at bar {bar_idx} | price={test_price:.2f}"
            )
            return None

        # Subsequent touches
        gap = bar_idx - touch_state.last_touch_bar
        if gap < Config.MIN_TOUCH_GAP_BARS:
            # Too close to the previous touch — update the touch bar but don't count
            logger.debug(
                f"[Detector] Touch on {touch_state.direction} level too close (gap={gap} < {Config.MIN_TOUCH_GAP_BARS}). Skipping."
            )
            touch_state.last_touch_bar = bar_idx  # reset to this bar
            return None

        # Valid 2nd touch → generate signal
        touch_state.touch_count += 1
        touch_state.signal_fired = True

        signal = self._build_signal(
            direction=touch_state.direction,
            close=close,
            timestamp=timestamp,
            bar_idx=bar_idx,
        )

        logger.info(
            f"[Detector] *** SIGNAL: {signal.direction} *** | "
            f"Entry={signal.entry_price:.2f} | SL={signal.sl:.2f} ({signal.sl_source}) | "
            f"T1={signal.t1:.2f} | Bar={bar_idx} | {timestamp.strftime('%H:%M')}"
        )
        return signal

    def _is_touch(self, price: float, level: float) -> bool:
        """Price within TOUCH_TOLERANCE_PCT of level."""
        if level <= 0:
            return False
        return abs(price - level) / level <= Config.TOUCH_TOLERANCE_PCT

    # ──────────────────────────────────────────────────────────────────────────
    # Signal construction
    # ──────────────────────────────────────────────────────────────────────────

    def _build_signal(
        self,
        direction: str,
        close: float,
        timestamp: datetime,
        bar_idx: int,
    ) -> Signal:
        """Build a Signal object with SL and T1 levels."""
        is_long = direction == "Long"
        touch_level = self.cpr.bc if is_long else self.cpr.tc

        # Target 1: TC for longs, BC for shorts (cross the CPR band)
        t1 = self.cpr.tc if is_long else self.cpr.bc

        # Stop Loss: SuperTrend(5, 3.0) if valid, else fallback
        sl, sl_source = self._compute_sl(direction, close, touch_level)

        return Signal(
            direction=direction,
            entry_price=close,
            sl=sl,
            t1=t1,
            bc=self.cpr.bc,
            tc=self.cpr.tc,
            pivot=self.cpr.pivot,
            timestamp=timestamp,
            touch_level=touch_level,
            sl_source=sl_source,
            bar_index=bar_idx,
        )

    def _compute_sl(
        self, direction: str, entry_price: float, touch_level: float
    ) -> tuple[float, str]:
        """
        Compute SL using SuperTrend(5, 3.0).
        Falls back to 0.8% beyond the touched level if ST is invalid.
        """
        is_long = direction == "Long"

        if self.st_sl.is_ready():
            st_val = self.st_sl.value
            if is_long and st_val < entry_price:
                return round(st_val, 2), "SuperTrend"
            elif not is_long and st_val > entry_price:
                return round(st_val, 2), "SuperTrend"

        # Fallback: 0.8% beyond the touched level
        if is_long:
            sl = round(touch_level * (1 - Config.SL_FALLBACK_PCT), 2)
        else:
            sl = round(touch_level * (1 + Config.SL_FALLBACK_PCT), 2)

        logger.warning(
            f"[Detector] SuperTrend SL invalid for {direction}, using fallback: {sl:.2f}"
        )
        return sl, "Fallback"

    # ──────────────────────────────────────────────────────────────────────────
    # Accessors
    # ──────────────────────────────────────────────────────────────────────────

    def get_trail_st_value(self) -> Optional[float]:
        """Current SuperTrend(5, 1.5) value for trailing stop management."""
        return self.st_trail.value

    def get_sl_st_value(self) -> Optional[float]:
        """Current SuperTrend(5, 3.0) value."""
        return self.st_sl.value

    def reset_bc_touch(self):
        """Reset BC touch state (e.g., after T1 hit to prevent re-entry)."""
        self.bc_touch.reset()

    def reset_tc_touch(self):
        """Reset TC touch state."""
        self.tc_touch.reset()
