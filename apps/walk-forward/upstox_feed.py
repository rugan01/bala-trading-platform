"""
Upstox Market Data Feed
- Finds current front-month futures instrument key for a configured root symbol
- Fetches previous day OHLC (for CPR calculation)
- Fetches intraday 15m candles (polled at candle boundaries)
"""

import gzip
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote
from typing import Optional

import pytz
import requests

from config import Config

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")
MCX_MARKET_QUOTE_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz"
UPSTOX_HISTORICAL_URL = "https://api.upstox.com/v3/historical-candle"
UPSTOX_INTRADAY_URL = "https://api.upstox.com/v3/historical-candle/intraday"

# Simple in-memory cache to avoid redundant downloads
_instrument_cache: dict = {}


class UpstoxFeed:
    def __init__(self, instrument_prefix: str = "SILVERMIC"):
        self.instrument_prefix = instrument_prefix.upper()
        self.instrument_key: str = ""
        self.trading_symbol: str = ""
        self.expiry: str = ""

    # ──────────────────────────────────────────────────────────────────────────
    # Instrument Discovery
    # ──────────────────────────────────────────────────────────────────────────

    def load_instrument(self) -> str:
        """
        Find the current front-month futures instrument key for instrument_prefix.

        Resolution order:
          1. In-memory cache (same process run)
          2. Download MCX instrument master from Upstox
          3. instrument-specific manual override env vars (fallback only)
        """
        global _instrument_cache

        today = date.today()
        cache_key = f"{self.instrument_prefix}_{today}"

        if cache_key in _instrument_cache:
            self.instrument_key = _instrument_cache[cache_key]["key"]
            self.trading_symbol = _instrument_cache[cache_key]["symbol"]
            self.expiry = _instrument_cache[cache_key]["expiry"]
            logger.info(
                f"[Instrument] Using cached: {self.trading_symbol} | Key: {self.instrument_key}"
            )
            return self.instrument_key

        instruments = self._download_instruments_master()
        if instruments is None:
            manual_key, manual_symbol, manual_expiry = Config.manual_instrument_override(self.instrument_prefix)
            if manual_key:
                self.instrument_key = manual_key
                self.trading_symbol = manual_symbol or self.instrument_prefix
                self.expiry = manual_expiry or "unknown"
                logger.warning(
                    f"[Instrument] Automatic discovery failed; using manual override: "
                    f"{self.trading_symbol} | Key: {self.instrument_key}"
                )
                _instrument_cache[cache_key] = {
                    "key": self.instrument_key,
                    "symbol": self.trading_symbol,
                    "expiry": self.expiry,
                }
                return self.instrument_key

            raise RuntimeError(
                f"Could not discover the active {self.instrument_prefix} futures contract automatically.\n"
                "The walk-forward engine now tries the same Upstox market-quote instrument "
                "master used by the analyzer project, then falls back to legacy sources.\n"
                "If all of them fail, add a temporary manual override in the repo .env file:\n"
                f"  UPSTOX_{self.instrument_prefix}_KEY=MCX_FO|<token_number>\n"
                f"  UPSTOX_{self.instrument_prefix}_SYMBOL={self.instrument_prefix}26MAYFUT\n"
                f"  UPSTOX_{self.instrument_prefix}_EXPIRY=2026-05-05"
            )

        # Filter the nearest active contract for the configured root symbol.
        candidates = []
        for inst in instruments:
            segment = (inst.get("segment") or inst.get("exchange") or "").upper()
            name = (inst.get("name", "") or inst.get("underlying_symbol", "")).upper()
            symbol = inst.get("trading_symbol", "") or inst.get("tradingsymbol", "")
            symbol_upper = symbol.upper()
            itype = (inst.get("instrument_type", "")).upper()
            expiry_date, expiry_str = self._normalize_expiry(inst.get("expiry"))

            if segment not in ("MCX_FO", "MCX"):
                continue
            if self.instrument_prefix not in symbol_upper and name != self.instrument_prefix:
                continue
            if itype not in ("FUT", "FUTCOM"):
                continue
            if not expiry_date or not expiry_str:
                continue

            if expiry_date < today:
                continue  # expired

            instrument_key = str(inst.get("instrument_key") or inst.get("instrument_token") or "")
            if not instrument_key:
                continue

            if "|" in instrument_key:
                key = instrument_key
            else:
                key = f"MCX_FO|{instrument_key}"

            candidates.append({
                "key": key,
                "symbol": symbol,
                "expiry": expiry_str,
                "expiry_date": expiry_date,
            })

        if not candidates:
            raise RuntimeError(
                f"No active {self.instrument_prefix} futures found in instrument master. "
                "The master file format may have changed — check manually."
            )

        # Sort by expiry ascending → front-month first
        candidates.sort(key=lambda x: x["expiry_date"])
        front = candidates[0]

        self.instrument_key = front["key"]
        self.trading_symbol = front["symbol"]
        self.expiry = front["expiry"]

        _instrument_cache[cache_key] = front
        logger.info(
            f"[Instrument] Found: {self.trading_symbol} | Key: {self.instrument_key} | Expiry: {self.expiry}"
        )
        return self.instrument_key

    def _normalize_expiry(self, raw_expiry) -> tuple[date | None, str]:
        """Normalize Upstox expiry values to (date, YYYY-MM-DD)."""
        if raw_expiry in (None, ""):
            return None, ""

        if isinstance(raw_expiry, (int, float)):
            ts = float(raw_expiry)
            if ts > 10_000_000_000:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            return dt, dt.isoformat()

        expiry_text = str(raw_expiry).strip()
        if not expiry_text:
            return None, ""

        if expiry_text.isdigit():
            ts = float(expiry_text)
            if ts > 10_000_000_000:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            return dt, dt.isoformat()

        try:
            dt = datetime.strptime(expiry_text[:10], "%Y-%m-%d").date()
            return dt, dt.isoformat()
        except ValueError:
            return None, ""

    def _download_instruments_master(self) -> list | None:
        """
        Download MCX instruments from Upstox.
        Priority: market-quote MCX master → authenticated API → legacy CDN fallbacks.
        Returns list of instrument dicts, or None if all sources fail.
        """
        sources = [
            {
                "label": "Market quote CDN — MCX instruments",
                "url": MCX_MARKET_QUOTE_MASTER_URL,
                "params": {},
                "headers": {"Accept": "application/json"},
                "is_gzip": True,
                "data_key": None,
            },
            {
                "label": "Upstox API (authenticated)",
                "url": f"{Config.UPSTOX_BASE_URL}/instruments",
                "params": {"exchange": "MCX_FO"},
                "headers": Config.upstox_headers(),
                "is_gzip": False,
                "data_key": "data",
            },
            {
                "label": "CDN — MCX instruments",
                "url": "https://assets.upstox.com/market-assets/instruments/exchange/MCX.json.gz",
                "params": {},
                "headers": {
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
                    "Referer": "https://upstox.com/",
                },
                "is_gzip": True,
                "data_key": None,
            },
            {
                "label": "CDN — complete instruments",
                "url": "https://assets.upstox.com/market-assets/instruments/v2/complete.json.gz",
                "params": {},
                "headers": {
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
                    "Referer": "https://upstox.com/",
                },
                "is_gzip": True,
                "data_key": None,
            },
        ]

        for src in sources:
            try:
                logger.info(f"[Instrument] Trying: {src['label']}")
                resp = requests.get(
                    src["url"], headers=src["headers"],
                    params=src["params"], timeout=30
                )
                if resp.status_code != 200:
                    logger.warning(f"[Instrument] HTTP {resp.status_code} from {src['label']}")
                    continue

                if src["is_gzip"]:
                    try:
                        data = json.loads(gzip.decompress(resp.content))
                    except Exception:
                        data = resp.json()
                else:
                    body = resp.json()
                    data = body.get(src["data_key"], body) if src["data_key"] else body

                if isinstance(data, dict) and "data" in data:
                    data = data["data"]

                logger.info(f"[Instrument] Loaded {len(data)} instruments from {src['label']}")
                return data

            except Exception as e:
                logger.warning(f"[Instrument] {src['label']} failed: {e}")
                continue

        return None

    def _encoded_key(self) -> str:
        """URL-encode the instrument key (| → %7C)."""
        return quote(self.instrument_key, safe="")

    # ──────────────────────────────────────────────────────────────────────────
    # Historical Data
    # ──────────────────────────────────────────────────────────────────────────

    def get_prev_day_ohlc(self) -> dict:
        """
        Fetch the most recent completed trading day's OHLC.
        Used for CPR calculation before session starts.
        Returns: {'open': float, 'high': float, 'low': float, 'close': float, 'date': str}
        """
        today = date.today()
        to_date = today.strftime("%Y-%m-%d")
        from_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")  # buffer for weekends/holidays

        logger.info(f"[Feed] Fetching daily OHLC | {from_date} → {to_date}")

        for attempt in range(3):
            try:
                resp = requests.get(
                    f"{UPSTOX_HISTORICAL_URL}/{self._encoded_key()}/days/1/{to_date}/{from_date}",
                    headers=Config.upstox_headers(),
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
                raw = data.get("data", {}).get("candles", [])

                candles = [self._parse_candle(c) for c in raw]
                candles.sort(key=lambda x: x["timestamp"])

                if not candles:
                    raise ValueError("No daily candles returned")

                completed = [c for c in candles if c["timestamp"].date() < today]

                if not completed:
                    raise ValueError("No completed day candle found")

                prev = completed[-1]
                ohlc = {
                    "date": prev["timestamp"].date().isoformat(),
                    "open": float(prev["open"]),
                    "high": float(prev["high"]),
                    "low": float(prev["low"]),
                    "close": float(prev["close"]),
                }
                logger.info(
                    f"[Feed] Prev day ({ohlc['date']}): "
                    f"H={ohlc['high']} L={ohlc['low']} C={ohlc['close']}"
                )
                return ohlc

            except Exception as e:
                logger.warning(f"[Feed] Attempt {attempt+1}/3 failed for daily OHLC: {e}")
                time.sleep(2 ** attempt)

        raise RuntimeError("Failed to fetch previous day OHLC after 3 attempts")

    def get_warmup_candles(self, n: int = 60) -> list[dict]:
        """
        Fetch historical 15m candles for SuperTrend warm-up.
        Returns last `n` candles before today's session.
        """
        today = date.today()
        to_date = today.strftime("%Y-%m-%d")
        from_date = (today - timedelta(days=14)).strftime("%Y-%m-%d")  # ~2 weeks buffer

        logger.info(f"[Feed] Fetching warm-up candles for SuperTrend initialization...")

        try:
            resp = requests.get(
                f"{UPSTOX_HISTORICAL_URL}/{self._encoded_key()}/minutes/15/{to_date}/{from_date}",
                headers=Config.upstox_headers(),
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data.get("data", {}).get("candles", [])

            candles = [self._parse_candle(c) for c in raw]
            candles.sort(key=lambda x: x["timestamp"])

            # Exclude today's incomplete candles (keep only fully completed ones)
            today_str = today.isoformat()
            candles = [c for c in candles if c["timestamp"].date().isoformat() < today_str]

            result = candles[-n:] if len(candles) > n else candles
            logger.info(f"[Feed] Loaded {len(result)} warm-up candles")
            return result

        except Exception as e:
            logger.warning(f"[Feed] Warm-up candles failed: {e}. Proceeding with empty history.")
            return []

    def get_intraday_candles(self, include_all_today: bool = False) -> list[dict]:
        """
        Fetch today's intraday 15m candles.
        Called at each 15-minute boundary during session.
        Returns list of candle dicts sorted ascending by timestamp.
        """
        today = date.today()

        for attempt in range(3):
            try:
                resp = requests.get(
                    f"{UPSTOX_INTRADAY_URL}/{self._encoded_key()}/minutes/15",
                    headers=Config.upstox_headers(),
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                raw = data.get("data", {}).get("candles", [])

                candles = [self._parse_candle(c) for c in raw]
                if include_all_today:
                    candles = [c for c in candles if c["timestamp"].astimezone(IST).date() == today]
                else:
                    candles = [c for c in candles if self._is_today_session_candle(c, today)]
                candles.sort(key=lambda x: x["timestamp"])
                return candles

            except Exception as e:
                logger.warning(f"[Feed] Intraday fetch attempt {attempt+1}/3 failed: {e}")
                time.sleep(2 ** attempt)

        logger.error("[Feed] All intraday fetch attempts failed")
        return []

    @staticmethod
    def _is_today_session_candle(candle: dict, session_date: date) -> bool:
        """Keep only today's strategy-session candles; never recycle prior sessions."""
        ts = candle["timestamp"].astimezone(IST)
        if ts.date() != session_date:
            return False

        start = (Config.SESSION_START_H, Config.SESSION_START_M)
        end = (Config.SESSION_END_H, Config.SESSION_END_M)
        current = (ts.hour, ts.minute)
        return start <= current <= end

    def get_latest_ltp(self) -> Optional[float]:
        """Get last traded price for real-time SL monitoring."""
        url = f"{Config.UPSTOX_BASE_URL}/market-quote/ltp"
        params = {"instrument_key": self.instrument_key}
        try:
            resp = requests.get(url, headers=Config.upstox_headers(), params=params, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            ltp_data = data.get("data", {})
            # Key in response uses dot notation: e.g., "MCX_FO:SILVERMIC25APRFUT"
            for key, val in ltp_data.items():
                return float(val.get("last_price", 0))
        except Exception as e:
            logger.warning(f"[Feed] LTP fetch failed: {e}")
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_candle(raw: list) -> dict:
        """
        Parse raw candle array from Upstox:
        [timestamp_str, open, high, low, close, volume, oi]
        """
        ts_str = raw[0]
        # Upstox returns ISO format: "2026-04-12T17:15:00+05:30"
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = IST.localize(ts)
        except Exception:
            ts = datetime.now(IST)

        return {
            "timestamp": ts,
            "open": float(raw[1]),
            "high": float(raw[2]),
            "low": float(raw[3]),
            "close": float(raw[4]),
            "volume": int(raw[5]) if len(raw) > 5 else 0,
        }
