"""
Replay market-data provider.

This provider lets the walk-forward engine run over normalized historical or
synthetic candles without touching live Upstox APIs.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytz

from models import Candle, InstrumentRef, Quote

IST = pytz.timezone("Asia/Kolkata")


@dataclass
class ReplayDataProvider:
    instrument: InstrumentRef
    prev_day_ohlc: dict[str, Any]
    warmup_candles: list[Candle]
    replay_candles: list[Candle]

    def resolve_instrument(self) -> InstrumentRef:
        return self.instrument

    def get_prev_day_ohlc(self, instrument: InstrumentRef) -> dict[str, Any]:
        return self.prev_day_ohlc

    def get_warmup_candles(self, instrument: InstrumentRef, n: int) -> list[Candle]:
        return self.warmup_candles[-n:] if n > 0 else []

    def get_intraday_candles(self, instrument: InstrumentRef) -> list[Candle]:
        return list(self.replay_candles)

    def get_latest_quote(self, instrument: InstrumentRef) -> Quote | None:
        if not self.replay_candles:
            return None
        last = self.replay_candles[-1]
        return Quote(instrument_key=instrument.instrument_key, ltp=last.close, timestamp=last.timestamp)


def load_candles_csv(path: Path) -> list[Candle]:
    """
    Load candles from a CSV with at least:
    timestamp, open, high, low, close

    Optional columns:
    volume, oi
    """

    candles: list[Candle] = []
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"timestamp", "open", "high", "low", "close"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV missing required columns: {', '.join(sorted(missing))}")

        for row in reader:
            candles.append(
                Candle(
                    timestamp=parse_timestamp(row["timestamp"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0.0),
                    oi=float(row.get("oi") or 0.0),
                )
            )

    candles.sort(key=lambda item: item.timestamp)
    return candles


def parse_timestamp(value: str) -> datetime:
    text = value.strip()
    ts = datetime.fromisoformat(text)
    if ts.tzinfo is None:
        ts = IST.localize(ts)
    return ts


def infer_prev_day_ohlc(candles: list[Candle], session_date: date) -> dict[str, Any]:
    previous = [item for item in candles if item.timestamp.astimezone(IST).date() < session_date]
    if not previous:
        raise ValueError("Cannot infer previous-day OHLC; provide candles before the session date")

    prev_date = previous[-1].timestamp.astimezone(IST).date()
    day = [item for item in previous if item.timestamp.astimezone(IST).date() == prev_date]
    return {
        "date": prev_date.isoformat(),
        "open": day[0].open,
        "high": max(item.high for item in day),
        "low": min(item.low for item in day),
        "close": day[-1].close,
    }


def split_replay_candles(
    candles: list[Candle],
    session_date: date,
    warmup_bars: int,
) -> tuple[list[Candle], list[Candle]]:
    warmup_source = [item for item in candles if item.timestamp.astimezone(IST).date() < session_date]
    replay = [item for item in candles if item.timestamp.astimezone(IST).date() == session_date]
    if not replay:
        raise ValueError(f"No replay candles found for session date {session_date.isoformat()}")
    return warmup_source[-warmup_bars:], replay


def available_session_dates(
    candles: list[Candle],
    date_from: date | None = None,
    date_to: date | None = None,
    require_previous_day: bool = True,
) -> list[date]:
    """
    Return sorted session dates present in the CSV.

    When require_previous_day=True, only include dates for which at least one
    prior candle exists, because replay needs previous-day OHLC to compute CPR.
    """
    dates = sorted({item.timestamp.astimezone(IST).date() for item in candles})
    selected: list[date] = []
    for session_date in dates:
        if date_from and session_date < date_from:
            continue
        if date_to and session_date > date_to:
            continue
        if require_previous_day and not any(
            item.timestamp.astimezone(IST).date() < session_date for item in candles
        ):
            continue
        selected.append(session_date)
    return selected
