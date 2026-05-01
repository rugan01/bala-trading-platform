"""
Paper Trade Manager — State Machine

Trade lifecycle:
  OPEN → (T1 hit → switch to trailing) → CLOSED (SL / Trail / Force-close)

For SILVERMIC V3:
  - 2 lots total
  - 50% exit (1 lot) at T1
  - Remaining 50% (1 lot) trailed via SuperTrend(5, 1.5)
  - Hard close at 23:00 IST
  - Max 2 trades per day
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

import pytz

from config import Config
from position_plans import PositionPlan, partial_t1_trail_plan
from signal_detector import Signal

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


class TradeState(Enum):
    OPEN = "open"
    T1_HIT = "t1_hit"         # T1 reached, trailing now active
    CLOSED = "closed"


class ExitReason(Enum):
    SL_HIT = "Stop Loss Hit"
    T1_AND_TRAIL = "T1 + Trail Stop"
    T1_AND_FORCE = "T1 + Force Close (EOD)"
    FORCE_CLOSE = "Force Close (EOD)"
    T1_ONLY = "Full Exit At T1"


@dataclass
class PaperTrade:
    # ── Identity ──────────────────────────────────────────────────────────────
    trade_num: int
    signal: Signal
    trading_symbol: str

    # ── Entry ─────────────────────────────────────────────────────────────────
    entry_price: float
    entry_time: datetime
    profile_id: str = "default"
    trade_label_prefix: str = "WFT"
    strategy_display_name: str = ""
    position_plan: PositionPlan = field(default_factory=partial_t1_trail_plan)
    lots_total: int = 0
    lots_open: int = 0

    # ── Levels ────────────────────────────────────────────────────────────────
    sl: float = 0.0
    t1: float = 0.0
    trail_sl: Optional[float] = None        # active after T1 hit

    # ── State ─────────────────────────────────────────────────────────────────
    state: TradeState = TradeState.OPEN
    t1_exit_price: Optional[float] = None
    t1_exit_time: Optional[datetime] = None

    # ── Exit ──────────────────────────────────────────────────────────────────
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[ExitReason] = None

    # ── Notion ────────────────────────────────────────────────────────────────
    notion_page_id: Optional[str] = None

    def __post_init__(self):
        self.position_plan.validate()
        if self.lots_total <= 0:
            self.lots_total = self.position_plan.total_lots
        if self.lots_open <= 0:
            self.lots_open = self.lots_total

    # ── Computed ──────────────────────────────────────────────────────────────
    @property
    def direction(self) -> str:
        return self.signal.direction

    @property
    def is_long(self) -> bool:
        return self.direction == "Long"

    @property
    def label(self) -> str:
        date_str = self.entry_time.strftime("%Y-%m-%d")
        return (
            f"{self.trade_label_prefix}-{self.trade_num:03d} "
            f"{self.profile_id} {self.trading_symbol} {self.direction} {date_str}"
        )

    def gross_pnl(self) -> Optional[float]:
        """Total gross P&L across both partial exits."""
        if self.exit_price is None:
            return None

        direction_mult = 1 if self.is_long else -1
        lot_size = self.position_plan.lot_size
        t1_exit_lots = self.t1_exit_lots()

        # Partial exit at T1, if configured and reached.
        t1_pnl = 0.0
        if self.t1_exit_price is not None:
            t1_pnl = (self.t1_exit_price - self.entry_price) * direction_mult * t1_exit_lots * lot_size

        # Remaining lots exit at final close.
        remaining_lots = self.lots_total - (t1_exit_lots if self.t1_exit_price is not None else 0)
        final_pnl = (self.exit_price - self.entry_price) * direction_mult * remaining_lots * lot_size

        return round(t1_pnl + final_pnl, 2)

    def net_pnl(self) -> Optional[float]:
        gross = self.gross_pnl()
        if gross is None:
            return None
        fees = self.position_plan.fee_per_lot * self.lots_total
        return round(gross - fees, 2)

    def r_multiple(self) -> Optional[float]:
        """Net P&L expressed in R (initial risk per lot)."""
        net = self.net_pnl()
        if net is None:
            return None
        risk_per_lot = abs(self.entry_price - self.sl) * self.position_plan.lot_size
        if risk_per_lot <= 0:
            return None
        total_risk = risk_per_lot * self.lots_total
        return round(net / total_risk, 2)

    def outcome(self) -> Optional[str]:
        net = self.net_pnl()
        if net is None:
            return None
        if net > 0:
            return "Win"
        elif net < 0:
            return "Loss"
        return "Breakeven"

    def t1_exit_lots(self) -> int:
        return min(self.position_plan.t1_exit_lots, self.lots_total)


class TradeManager:
    """
    Manages paper trades: entry, T1 partial exit, trailing, force-close.
    Coordinates with Notion logger and Telegram alerter.
    """

    def __init__(
        self,
        notion_logger,
        telegram_alerter,
        trading_symbol: str,
        position_plan: PositionPlan | None = None,
        profile_id: str = "default",
        trade_label_prefix: str = "WFT",
        strategy_display_name: str = "",
    ):
        self.notion = notion_logger
        self.telegram = telegram_alerter
        self.trading_symbol = trading_symbol
        self.position_plan = position_plan or partial_t1_trail_plan()
        self.profile_id = profile_id
        self.trade_label_prefix = trade_label_prefix
        self.strategy_display_name = strategy_display_name
        self.position_plan.validate()

        self._active_trade: Optional[PaperTrade] = None
        self._trade_count_today: int = 0
        self._trade_serial: int = 0         # global serial across days
        self._closed_trades: list[PaperTrade] = []

    # ──────────────────────────────────────────────────────────────────────────
    # Entry
    # ──────────────────────────────────────────────────────────────────────────

    def enter_trade(self, signal: Signal, timestamp: datetime):
        """Open a new paper trade based on the signal."""
        if self._active_trade is not None:
            logger.warning("[TradeManager] Cannot open trade — one already active")
            return

        if self.is_daily_limit_reached():
            logger.info("[TradeManager] Daily trade limit reached — skipping signal")
            return

        self._trade_serial += 1
        self._trade_count_today += 1

        trade = PaperTrade(
            trade_num=self._trade_serial,
            signal=signal,
            trading_symbol=self.trading_symbol,
            profile_id=self.profile_id,
            trade_label_prefix=self.trade_label_prefix,
            strategy_display_name=self.strategy_display_name,
            entry_price=signal.entry_price,
            entry_time=timestamp,
            position_plan=self.position_plan,
            sl=signal.sl,
            t1=signal.t1,
        )
        self._active_trade = trade

        logger.info(
            f"[TradeManager] ENTERED {trade.direction} #{trade.trade_num} | "
            f"Entry={trade.entry_price:.2f} | SL={trade.sl:.2f} | T1={trade.t1:.2f} | "
            f"Lots={trade.lots_total}"
        )

        # Log to Notion and send Telegram alert
        try:
            page_id = self.notion.create_entry(trade)
            trade.notion_page_id = page_id
        except Exception as e:
            logger.error(f"[TradeManager] Notion create failed: {e}")

        try:
            self.telegram.send_signal(trade)
        except Exception as e:
            logger.error(f"[TradeManager] Telegram signal alert failed: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # Trade update (called on each new candle)
    # ──────────────────────────────────────────────────────────────────────────

    def update_trade(self, candle: dict, trail_st_value: Optional[float]):
        """
        Check SL, T1, and trailing conditions against the new candle.
        Closes the trade if triggered.
        """
        trade = self._active_trade
        if trade is None or trade.state == TradeState.CLOSED:
            return

        H = candle["high"]
        L = candle["low"]
        C = candle["close"]
        ts = candle["timestamp"]

        # Update trail SL from latest SuperTrend(5, 1.5), when the plan uses trailing.
        if trade.state == TradeState.T1_HIT and trade.position_plan.trail_after_t1 and trail_st_value is not None:
            # Trail SL moves only in the favourable direction
            if trade.is_long:
                trade.trail_sl = max(trade.trail_sl or trail_st_value, trail_st_value)
            else:
                trail_val = trail_st_value
                trade.trail_sl = min(trade.trail_sl or trail_val, trail_val)

        if trade.is_long:
            self._update_long(trade, H, L, C, ts, trail_st_value)
        else:
            self._update_short(trade, H, L, C, ts, trail_st_value)

    def _update_long(
        self,
        trade: PaperTrade,
        H: float,
        L: float,
        C: float,
        ts: datetime,
        trail_st: Optional[float],
    ):
        # ── SL check ──────────────────────────────────────────────────────────
        active_sl = trade.trail_sl if trade.state == TradeState.T1_HIT else trade.sl
        if L <= active_sl:
            exit_price = active_sl  # assume filled at SL level
            reason = ExitReason.T1_AND_TRAIL if trade.state == TradeState.T1_HIT else ExitReason.SL_HIT
            self._close_trade(trade, exit_price, ts, reason)
            return

        # ── T1 check ──────────────────────────────────────────────────────────
        if trade.state == TradeState.OPEN and H >= trade.t1:
            self._hit_t1(trade, trade.t1, ts, trail_st)
            return  # Process trailing from next candle

    def _update_short(
        self,
        trade: PaperTrade,
        H: float,
        L: float,
        C: float,
        ts: datetime,
        trail_st: Optional[float],
    ):
        # ── SL check ──────────────────────────────────────────────────────────
        active_sl = trade.trail_sl if trade.state == TradeState.T1_HIT else trade.sl
        if H >= active_sl:
            exit_price = active_sl
            reason = ExitReason.T1_AND_TRAIL if trade.state == TradeState.T1_HIT else ExitReason.SL_HIT
            self._close_trade(trade, exit_price, ts, reason)
            return

        # ── T1 check ──────────────────────────────────────────────────────────
        if trade.state == TradeState.OPEN and L <= trade.t1:
            self._hit_t1(trade, trade.t1, ts, trail_st)
            return

    # ──────────────────────────────────────────────────────────────────────────
    # T1 Hit (partial exit)
    # ──────────────────────────────────────────────────────────────────────────

    def _hit_t1(self, trade: PaperTrade, t1_price: float, ts: datetime, trail_st: Optional[float]):
        trade.state = TradeState.T1_HIT
        trade.t1_exit_price = t1_price
        trade.t1_exit_time = ts
        exit_lots = trade.t1_exit_lots()
        trade.lots_open = trade.lots_total - exit_lots

        # Initialize trail SL from current SuperTrend(5, 1.5)
        if trade.position_plan.trail_after_t1 and trail_st is not None:
            trade.trail_sl = trail_st
        elif trade.position_plan.trail_after_t1:
            # Fallback: move SL to entry (breakeven)
            trade.trail_sl = trade.entry_price
            logger.warning("[TradeManager] Trail ST not ready at T1 — using entry as trail SL")
        else:
            # Safe fallback for future non-trailing partial-exit plans.
            trade.trail_sl = trade.entry_price

        t1_pnl = (
            (t1_price - trade.entry_price)
            * (1 if trade.is_long else -1)
            * exit_lots
            * trade.position_plan.lot_size
        )

        trail_display = f"{trade.trail_sl:.2f}" if trade.trail_sl is not None else "-"
        logger.info(
            f"[TradeManager] T1 HIT #{trade.trade_num} | "
            f"T1={t1_price:.2f} | Lots exited={exit_lots} | P&L={t1_pnl:.0f} | "
            f"Trail SL set={trail_display}"
        )

        try:
            self.telegram.send_t1_hit(trade, t1_pnl)
        except Exception as e:
            logger.error(f"[TradeManager] Telegram T1 alert failed: {e}")

        if trade.lots_open <= 0:
            self._close_trade(trade, t1_price, ts, ExitReason.T1_ONLY)

    # ──────────────────────────────────────────────────────────────────────────
    # Force Close (EOD)
    # ──────────────────────────────────────────────────────────────────────────

    def force_close_all(self, close_price: float, timestamp: datetime):
        """Force close any open trade at session end (23:00 IST)."""
        trade = self._active_trade
        if trade is None or trade.state == TradeState.CLOSED:
            return
        if not trade.position_plan.force_close_enabled:
            logger.info(
                f"[TradeManager] Force close requested for #{trade.trade_num}, "
                f"but plan {trade.position_plan.plan_id} has force_close_enabled=False"
            )
            return

        reason = (
            ExitReason.T1_AND_FORCE if trade.state == TradeState.T1_HIT
            else ExitReason.FORCE_CLOSE
        )
        logger.info(
            f"[TradeManager] FORCE CLOSE #{trade.trade_num} at {close_price:.2f} | {reason.value}"
        )
        self._close_trade(trade, close_price, timestamp, reason)

    # ──────────────────────────────────────────────────────────────────────────
    # Internal close
    # ──────────────────────────────────────────────────────────────────────────

    def _close_trade(
        self,
        trade: PaperTrade,
        exit_price: float,
        exit_time: datetime,
        reason: ExitReason,
    ):
        trade.exit_price = exit_price
        trade.exit_time = exit_time
        trade.exit_reason = reason
        trade.state = TradeState.CLOSED

        net = trade.net_pnl()
        r = trade.r_multiple()

        logger.info(
            f"[TradeManager] CLOSED #{trade.trade_num} {trade.direction} | "
            f"Entry={trade.entry_price:.2f} → Exit={exit_price:.2f} | "
            f"Net P&L=₹{net:.0f} | R={r} | Reason={reason.value}"
        )

        # Update Notion
        try:
            if trade.notion_page_id:
                self.notion.update_exit(trade)
        except Exception as e:
            logger.error(f"[TradeManager] Notion update failed: {e}")

        # Telegram
        try:
            self.telegram.send_trade_closed(trade)
        except Exception as e:
            logger.error(f"[TradeManager] Telegram close alert failed: {e}")

        self._closed_trades.append(trade)
        self._active_trade = None

    # ──────────────────────────────────────────────────────────────────────────
    # State queries
    # ──────────────────────────────────────────────────────────────────────────

    def has_open_trade(self) -> bool:
        return self._active_trade is not None

    def is_daily_limit_reached(self) -> bool:
        return self._trade_count_today >= Config.MAX_TRADES_PER_DAY

    def get_active_trade(self) -> Optional[PaperTrade]:
        return self._active_trade

    def get_closed_trades(self) -> list[PaperTrade]:
        """Return a copy of all closed trades captured this session."""
        return list(self._closed_trades)

    def pop_new_closed_trades(self, seen_count: int) -> tuple[list[PaperTrade], int]:
        """
        Return closed trades since the caller's last seen index, along with the
        updated seen count.
        """
        new_trades = self._closed_trades[seen_count:]
        return list(new_trades), len(self._closed_trades)

    def reset_daily_counts(self):
        """Call this at the start of each trading day."""
        self._trade_count_today = 0
        self._closed_trades.clear()
        logger.info("[TradeManager] Daily trade count reset")
