"""
Telegram Alerter — sends formatted messages via Telegram Bot API.

Messages sent:
  1. Day start   — CPR levels for the day
  2. Signal      — Entry triggered (price, SL, T1, R-risk)
  3. T1 hit      — First target reached, trailing activated
  4. Trade closed — Final P&L, R-multiple, outcome
  5. Day end     — Daily summary
"""

import logging
from typing import Optional

import requests

from config import Config

logger = logging.getLogger(__name__)

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramAlerter:

    def __init__(self, runner_label: str = "Walk-Forward"):
        self._enabled = bool(Config.TELEGRAM_BOT_TOKEN and Config.TELEGRAM_CHAT_ID)
        self.runner_label = runner_label
        if not self._enabled:
            logger.warning("[Telegram] Bot token or chat ID missing — alerts disabled")

    # ──────────────────────────────────────────────────────────────────────────
    # Public alert methods
    # ──────────────────────────────────────────────────────────────────────────

    def send_day_start(self, cpr):
        """Send CPR levels at start of monitoring session."""
        lvl = cpr.levels
        width_label = "NARROW — trending day" if lvl.cpr_width_pct < 0.3 else "WIDE — ranging day"
        msg = (
            f"📊 *{self.runner_label} — Walk-Forward Day Start*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"CPR Levels (from yesterday's OHLC)\n\n"
            f"  Pivot : `{lvl.pivot:.2f}`\n"
            f"  TC    : `{lvl.tc:.2f}`  ← SHORT trigger\n"
            f"  BC    : `{lvl.bc:.2f}`  ← LONG trigger\n"
            f"  R1    : `{lvl.r1:.2f}`\n"
            f"  S1    : `{lvl.s1:.2f}`\n\n"
            f"CPR Width: `{lvl.cpr_width_pct:.3f}%` — {width_label}\n\n"
            f"Session: 17:00 – 23:00 IST | HTF: OFF\n"
            f"Max trades: {Config.MAX_TRADES_PER_DAY}"
        )
        self._send(msg)

    def send_signal(self, trade):
        """Entry signal alert."""
        signal = trade.signal
        direction_emoji = "🟢" if trade.is_long else "🔴"
        risk_per_lot = abs(signal.entry_price - signal.sl)
        risk_total = risk_per_lot * Config.LOTS * Config.LOT_SIZE

        msg = (
            f"{direction_emoji} *{self.runner_label} SIGNAL: {trade.direction.upper()} #{trade.trade_num}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Symbol  : `{trade.trading_symbol}`\n"
            f"Entry   : `₹{signal.entry_price:.2f}`\n"
            f"SL      : `₹{signal.sl:.2f}` ({signal.sl_source})\n"
            f"T1      : `₹{signal.t1:.2f}`\n"
            f"Lots    : `{Config.LOTS}`\n"
            f"Risk    : `₹{risk_total:.0f}` total\n"
            f"Time    : `{signal.timestamp.strftime('%H:%M IST')}`\n\n"
            f"CPR: BC=`{signal.bc:.2f}` | TC=`{signal.tc:.2f}`"
        )
        self._send(msg)

    def send_t1_hit(self, trade, t1_pnl: float):
        """T1 partial exit alert."""
        trail_sl = trade.trail_sl or trade.entry_price
        pnl_sign = "+" if t1_pnl >= 0 else ""
        msg = (
            f"🎯 *{self.runner_label} T1 HIT — #{trade.trade_num} {trade.trading_symbol}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Lot 1 exited at `₹{trade.t1_exit_price:.2f}`\n"
            f"Lot 1 P&L : `{pnl_sign}₹{t1_pnl:.0f}`\n\n"
            f"Lot 2 still open — trailing now active\n"
            f"Trail SL  : `₹{trail_sl:.2f}` (ST 5,1.5)\n"
            f"Time      : `{trade.t1_exit_time.strftime('%H:%M IST')}`"
        )
        self._send(msg)

    def send_trade_closed(self, trade):
        """Final trade closed alert with full P&L summary."""
        net = trade.net_pnl() or 0
        r = trade.r_multiple()
        outcome = trade.outcome() or "Unknown"

        if outcome == "Win":
            result_emoji = "✅"
        elif outcome == "Loss":
            result_emoji = "❌"
        else:
            result_emoji = "➖"

        pnl_sign = "+" if net >= 0 else ""
        r_str = f"{'+' if r and r >= 0 else ''}{r:.2f}R" if r is not None else "N/A"

        t1_line = ""
        if trade.t1_exit_price:
            t1_line = f"T1 Exit   : `₹{trade.t1_exit_price:.2f}` (lot 1)\n"

        msg = (
            f"{result_emoji} *{self.runner_label} CLOSED — #{trade.trade_num} {outcome.upper()}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Symbol    : `{trade.trading_symbol}` {trade.direction}\n"
            f"Entry     : `₹{trade.entry_price:.2f}` at {trade.entry_time.strftime('%H:%M')}\n"
            f"{t1_line}"
            f"Exit      : `₹{trade.exit_price:.2f}` at {trade.exit_time.strftime('%H:%M')}\n"
            f"Reason    : {trade.exit_reason.value}\n\n"
            f"Net P&L   : `{pnl_sign}₹{net:.0f}`\n"
            f"R-Multiple: `{r_str}`\n"
            f"Fees      : `₹{Config.FEES_PER_LOT * trade.lots_total:.0f}`"
        )
        self._send(msg)

    def send_day_summary(self, trades: list, date_str: str):
        """End-of-day summary of all paper trades."""
        if not trades:
            msg = (
                f"📋 *Walk-Forward Day Summary — {date_str}*\n"
                f"{self.runner_label}\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"No signals triggered today."
            )
            self._send(msg)
            return

        wins = [t for t in trades if t.outcome() == "Win"]
        losses = [t for t in trades if t.outcome() == "Loss"]
        total_net = sum(t.net_pnl() or 0 for t in trades)
        pnl_sign = "+" if total_net >= 0 else ""

        trade_lines = []
        for t in trades:
            r = t.r_multiple()
            r_str = f"{'+' if r and r >= 0 else ''}{r:.2f}R" if r is not None else "N/A"
            outcome_emoji = "✅" if t.outcome() == "Win" else ("❌" if t.outcome() == "Loss" else "➖")
            trade_lines.append(
                f"  {outcome_emoji} #{t.trade_num} {t.direction[:1]} | "
                f"Entry={t.entry_price:.0f} Exit={t.exit_price:.0f} | "
                f"P&L={'+' if (t.net_pnl() or 0) >= 0 else ''}₹{t.net_pnl() or 0:.0f} ({r_str})"
            )

        msg = (
            f"📋 *Walk-Forward Day Summary — {date_str}*\n"
            f"{self.runner_label}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Trades : {len(trades)} | W:{len(wins)} L:{len(losses)}\n"
            f"Net P&L: `{pnl_sign}₹{total_net:.0f}`\n\n"
            + "\n".join(trade_lines)
        )
        self._send(msg)

    def send_error(self, message: str):
        """Send an error/warning alert."""
        msg = f"⚠️ *Walk-Forward Error*\n{message}"
        self._send(msg)

    # ──────────────────────────────────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────────────────────────────────

    def _send(self, text: str):
        if not self._enabled:
            logger.info(f"[Telegram MOCK] {text[:120]}...")
            return

        url = TELEGRAM_URL.format(token=Config.TELEGRAM_BOT_TOKEN)
        payload = {
            "chat_id": Config.TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"[Telegram] Send failed: {e}")
