"""
SuperTrend Indicator (ATR-based)
Matches TradingView / PineScript SuperTrend implementation exactly.

Two instances used by the strategy:
  ST(5, 3.0) → entry stop-loss level
  ST(5, 1.5) → trailing stop after T1 is hit
"""

import logging
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)


class SuperTrend:
    """
    Incremental SuperTrend calculator.

    Usage:
        st = SuperTrend(length=5, factor=3.0)
        st.warmup(historical_candles)  # pre-load history
        st.update(new_candle)
        sl_level = st.value
        is_bullish = (st.trend == 1)
    """

    def __init__(self, length: int, factor: float):
        self.length = length
        self.factor = factor

        # ATR calculation — use Wilder's smoothing (RMA)
        self._atr: Optional[float] = None
        self._prev_close: Optional[float] = None
        self._bar_count: int = 0

        # SuperTrend bands
        self._upper: Optional[float] = None   # resistance band (SL for shorts)
        self._lower: Optional[float] = None   # support band  (SL for longs)

        # Trend direction: +1 = bullish, -1 = bearish
        self.trend: int = 1
        self.value: Optional[float] = None   # Current SuperTrend value

        # For True Range smoothing
        self._tr_buffer: deque = deque(maxlen=length)

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def warmup(self, candles: list[dict]):
        """Pre-load historical candles to initialize ATR and SuperTrend."""
        for candle in candles:
            self.update(candle)
        if self.value is not None:
            logger.debug(
                f"[ST({self.length},{self.factor})] Warmed up. "
                f"Value={self.value:.2f}, Trend={'↑' if self.trend == 1 else '↓'}"
            )

    def update(self, candle: dict) -> Optional[float]:
        """
        Process a new completed candle and update the SuperTrend.

        Args:
            candle: {'open', 'high', 'low', 'close', 'timestamp', ...}

        Returns:
            Current SuperTrend value (or None if not yet initialized)
        """
        H = candle["high"]
        L = candle["low"]
        C = candle["close"]
        hl2 = (H + L) / 2.0

        # True Range
        if self._prev_close is not None:
            tr = max(H - L, abs(H - self._prev_close), abs(L - self._prev_close))
        else:
            tr = H - L

        # ATR via Wilder's RMA (exponential smoothing with alpha=1/length)
        # For first `length` bars: use simple average
        self._tr_buffer.append(tr)
        self._bar_count += 1

        if self._bar_count < self.length:
            # Not enough data yet
            self._prev_close = C
            return None

        if self._atr is None:
            # Initial ATR = simple average of first `length` TRs
            self._atr = sum(self._tr_buffer) / self.length
        else:
            # Wilder's smoothing
            self._atr = ((self._atr * (self.length - 1)) + tr) / self.length

        # Band calculations
        raw_upper = hl2 + (self.factor * self._atr)  # SL level for shorts (above price)
        raw_lower = hl2 - (self.factor * self._atr)  # SL level for longs (below price)

        prev_upper = self._upper
        prev_lower = self._lower
        prev_close = self._prev_close
        prev_trend = self.trend

        # Smooth the bands (prevent whipsaw)
        if prev_upper is not None and prev_close is not None:
            # Upper band: only move down, never up while trend is bearish
            if raw_upper < prev_upper or prev_close > prev_upper:
                self._upper = raw_upper
            else:
                self._upper = prev_upper
        else:
            self._upper = raw_upper

        if prev_lower is not None and prev_close is not None:
            # Lower band: only move up, never down while trend is bullish
            if raw_lower > prev_lower or prev_close < prev_lower:
                self._lower = raw_lower
            else:
                self._lower = prev_lower
        else:
            self._lower = raw_lower

        # Trend flip logic
        if prev_upper is not None and prev_lower is not None:
            if prev_trend == -1:
                # Was bearish: flip to bullish if close breaks above upper band
                if C > self._upper:
                    self.trend = 1
                else:
                    self.trend = -1
            else:
                # Was bullish: flip to bearish if close breaks below lower band
                if C < self._lower:
                    self.trend = -1
                else:
                    self.trend = 1
        # else: first bar, keep default trend = 1

        # SuperTrend value = the active band
        self.value = self._lower if self.trend == 1 else self._upper

        self._prev_close = C
        return self.value

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def is_valid_long_sl(self, entry_price: float) -> bool:
        """SL is valid for a long if ST value is below entry price."""
        return self.value is not None and self.value < entry_price

    def is_valid_short_sl(self, entry_price: float) -> bool:
        """SL is valid for a short if ST value is above entry price."""
        return self.value is not None and self.value > entry_price

    def is_ready(self) -> bool:
        return self.value is not None

    def __repr__(self) -> str:
        trend_str = "↑" if self.trend == 1 else "↓"
        val = f"{self.value:.2f}" if self.value else "N/A"
        return f"SuperTrend({self.length},{self.factor}) = {val} {trend_str}"
