"""
Notion Logger — creates and updates walk-forward trade pages
in the "Trading Journal Walk forward" database.
Schema mirrors the live trade journal exactly.
"""

import logging
import time
from datetime import datetime
from typing import Optional

import requests

from config import Config

logger = logging.getLogger(__name__)

NOTION_PAGES_URL = "https://api.notion.com/v1/pages"


class NotionLogger:

    def __init__(self):
        if not Config.NOTION_API_KEY:
            logger.warning("[Notion] NOTION_API_KEY not set — logging disabled")

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def create_entry(self, trade) -> Optional[str]:
        """
        Create a new Notion page for a paper trade at entry.
        Returns the page_id for later update, or None on failure.
        """
        if not Config.NOTION_API_KEY:
            return None

        entry_time = trade.entry_time
        signal = trade.signal

        properties = {
            # Title
            "Trade Label": self._title(trade.label),

            # Identity
            "Symbol": self._text(trade.trading_symbol),
            "Direction": self._select(trade.direction),
            "Instrument Type": self._select("Commodity Futures"),
            "Timeframe": self._select("Intraday"),
            "Strategy": self._select(trade.strategy_display_name or "Walk Forward"),

            # Entry
            "Entry Date": self._date(entry_time),
            "Entry Time": self._text(entry_time.strftime("%H:%M")),
            "Entry Price": self._number(trade.entry_price),
            "Stop Loss": self._number(trade.sl),
            "Target": self._number(trade.t1),
            "Quantity": self._number(float(trade.lots_total)),
            "Lot Size": self._number(Config.LOT_SIZE),
            "Fees": self._number(Config.FEES_PER_LOT * trade.lots_total),

            # Pre-trade context (auto-generated)
            "Pre-trade Notes": self._text(self._build_pretrade_notes(trade)),

            # Followed Plan — always True for paper trades (rule-based)
            "Followed Plan": self._checkbox(True),
        }

        body = {
            "parent": {"database_id": Config.NOTION_WF_DB_ID},
            "properties": properties,
        }

        return self._post_page(body)

    def update_exit(self, trade) -> bool:
        """
        Update the Notion page with exit details after trade closes.
        """
        if not Config.NOTION_API_KEY or not trade.notion_page_id:
            return False

        exit_time = trade.exit_time
        net_pnl = trade.net_pnl() or 0.0
        gross_pnl = trade.gross_pnl() or 0.0
        r_mult = trade.r_multiple()
        outcome = trade.outcome() or "Loss"

        properties = {
            "Exit Date": self._date(exit_time),
            "Exit Time": self._text(exit_time.strftime("%H:%M")),
            "Exit Price": self._number(trade.exit_price),
            "P&L": self._number(gross_pnl),
            "Outcome": self._select(outcome),
            "Post-trade Review": self._text(self._build_posttrade_notes(trade)),
        }

        body = {"properties": properties}
        return self._patch_page(trade.notion_page_id, body)

    # ──────────────────────────────────────────────────────────────────────────
    # Note builders
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_pretrade_notes(trade) -> str:
        signal = trade.signal
        cpr_width = abs(signal.tc - signal.bc)
        cpr_width_pct = (cpr_width / signal.bc * 100) if signal.bc > 0 else 0
        lines = [
            f"[Walk-Forward Paper Trade #{trade.trade_num}]",
            f"Strategy: {trade.strategy_display_name or 'Walk Forward'} | Profile: {trade.profile_id}",
            f"Pivot={signal.pivot:.2f} | BC={signal.bc:.2f} | TC={signal.tc:.2f}",
            f"CPR Width={cpr_width_pct:.3f}% ({'NARROW' if cpr_width_pct < 0.3 else 'WIDE'})",
            f"Touch Level: {signal.touch_level:.2f} ({signal.direction} setup)",
            f"SL Source: {signal.sl_source} | SL={signal.sl:.2f}",
            f"R per lot: {abs(signal.entry_price - signal.sl):.2f}",
        ]
        return "\n".join(lines)

    @staticmethod
    def _build_posttrade_notes(trade) -> str:
        t1_note = ""
        if trade.t1_exit_price:
            t1_pnl = (trade.t1_exit_price - trade.entry_price) * (1 if trade.is_long else -1) * Config.LOT_SIZE
            t1_note = f"T1 hit at {trade.t1_exit_price:.2f} (+₹{t1_pnl:.0f} on lot 1) | "

        trail_note = f"Trail SL={trade.trail_sl:.2f} | " if trade.trail_sl else ""

        lines = [
            f"Exit Reason: {trade.exit_reason.value}",
            f"{t1_note}{trail_note}Exit={trade.exit_price:.2f}",
            f"Net P&L: ₹{trade.net_pnl() or 0:.0f} | R-Multiple: {trade.r_multiple() or 'N/A'}",
            f"Fees: ₹{Config.FEES_PER_LOT * trade.lots_total:.0f}",
        ]
        return "\n".join(lines)

    # ──────────────────────────────────────────────────────────────────────────
    # HTTP helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _post_page(self, body: dict) -> Optional[str]:
        for attempt in range(3):
            try:
                resp = requests.post(
                    NOTION_PAGES_URL,
                    headers=Config.notion_headers(),
                    json=body,
                    timeout=15,
                )
                resp.raise_for_status()
                page_id = resp.json().get("id")
                logger.info(f"[Notion] Page created: {page_id}")
                return page_id
            except requests.HTTPError as e:
                logger.error(f"[Notion] POST failed ({resp.status_code}): {resp.text[:300]}")
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                else:
                    return None
            except Exception as e:
                logger.error(f"[Notion] POST error attempt {attempt+1}: {e}")
                time.sleep(2 ** attempt)
        return None

    def _patch_page(self, page_id: str, body: dict) -> bool:
        url = f"{NOTION_PAGES_URL}/{page_id}"
        for attempt in range(3):
            try:
                resp = requests.patch(
                    url,
                    headers=Config.notion_headers(),
                    json=body,
                    timeout=15,
                )
                resp.raise_for_status()
                logger.info(f"[Notion] Page updated: {page_id}")
                return True
            except requests.HTTPError as e:
                logger.error(f"[Notion] PATCH failed ({resp.status_code}): {resp.text[:300]}")
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                else:
                    return False
            except Exception as e:
                logger.error(f"[Notion] PATCH error attempt {attempt+1}: {e}")
                time.sleep(2 ** attempt)
        return False

    # ──────────────────────────────────────────────────────────────────────────
    # Property builders (Notion API format)
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _title(text: str) -> dict:
        return {"title": [{"text": {"content": text}}]}

    @staticmethod
    def _text(text: str) -> dict:
        return {"rich_text": [{"text": {"content": str(text)}}]}

    @staticmethod
    def _number(value: float) -> dict:
        return {"number": round(value, 2)}

    @staticmethod
    def _select(name: str) -> dict:
        return {"select": {"name": name}}

    @staticmethod
    def _date(dt: datetime) -> dict:
        return {"date": {"start": dt.strftime("%Y-%m-%d")}}

    @staticmethod
    def _checkbox(value: bool) -> dict:
        return {"checkbox": value}
