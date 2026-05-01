"""
Market-data provider adapter for Upstox.

This wraps the existing UpstoxFeed instead of rewriting it. The goal is a
provider seam with no behavior change to the current SILVERMIC validator.
"""

from __future__ import annotations

from datetime import datetime

import pytz

from config import Config
from models import Candle, InstrumentRef, Quote
from upstox_feed import UpstoxFeed

IST = pytz.timezone("Asia/Kolkata")


class UpstoxMarketDataProvider:
    """Adapter exposing UpstoxFeed through the MarketDataProvider protocol."""

    provider_id = "upstox"

    def __init__(
        self,
        feed: UpstoxFeed | None = None,
        include_all_intraday_candles: bool = False,
        instrument_prefix: str = "SILVERMIC",
    ):
        self.feed = feed or UpstoxFeed(instrument_prefix=instrument_prefix)
        self.include_all_intraday_candles = include_all_intraday_candles
        self.instrument_prefix = instrument_prefix.upper()

    def resolve_instrument(self) -> InstrumentRef:
        self.feed.load_instrument()
        return InstrumentRef(
            instrument_key=self.feed.instrument_key,
            trading_symbol=self.feed.trading_symbol,
            expiry=self.feed.expiry,
            segment=Config.EXCHANGE,
            underlying=self.instrument_prefix,
        )

    def get_prev_day_ohlc(self, instrument: InstrumentRef) -> dict:
        self._sync_instrument(instrument)
        return self.feed.get_prev_day_ohlc()

    def get_warmup_candles(self, instrument: InstrumentRef, n: int) -> list[Candle]:
        self._sync_instrument(instrument)
        return [Candle.from_legacy(item) for item in self.feed.get_warmup_candles(n=n)]

    def get_intraday_candles(self, instrument: InstrumentRef) -> list[Candle]:
        self._sync_instrument(instrument)
        return [
            Candle.from_legacy(item)
            for item in self.feed.get_intraday_candles(
                include_all_today=self.include_all_intraday_candles
            )
        ]

    def get_latest_quote(self, instrument: InstrumentRef) -> Quote | None:
        self._sync_instrument(instrument)
        ltp = self.feed.get_latest_ltp()
        if ltp is None:
            return None
        return Quote(
            instrument_key=instrument.instrument_key,
            ltp=float(ltp),
            timestamp=datetime.now(IST),
        )

    def _sync_instrument(self, instrument: InstrumentRef) -> None:
        """Keep the wrapped legacy feed aligned with the normalized ref."""

        self.feed.instrument_key = instrument.instrument_key
        self.feed.trading_symbol = instrument.trading_symbol
        self.feed.expiry = instrument.expiry
