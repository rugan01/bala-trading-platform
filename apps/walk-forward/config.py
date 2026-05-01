"""
Walk-Forward Validator — Configuration
SILVERMIC V3 (CPR Band TC/BC Rejection) | 17:00–23:00 IST | HTF Filter OFF
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load from project .env
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"
load_dotenv(ENV_FILE)


class Config:
    # ── Upstox ────────────────────────────────────────────────────────────────
    UPSTOX_ACCESS_TOKEN: str = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    UPSTOX_API_KEY: str = os.getenv("UPSTOX_API_KEY", "")
    UPSTOX_BASE_URL: str = "https://api.upstox.com/v2"
    UPSTOX_INSTRUMENTS_URL: str = (
        "https://assets.upstox.com/market-assets/instruments/v2/complete.json.gz"
    )

    # Manual instrument override (bypasses CDN download entirely)
    # Set these in .env when CDN is blocked or to skip the download on startup
    # Format: MCX_FO|<token_number>  e.g.  MCX_FO|438797
    UPSTOX_SILVERMIC_KEY: str = os.getenv("UPSTOX_SILVERMIC_KEY", "")
    UPSTOX_SILVERMIC_SYMBOL: str = os.getenv("UPSTOX_SILVERMIC_SYMBOL", "")
    UPSTOX_SILVERMIC_EXPIRY: str = os.getenv("UPSTOX_SILVERMIC_EXPIRY", "")

    # ── Notion ────────────────────────────────────────────────────────────────
    NOTION_API_KEY: str = os.getenv("NOTION_API_KEY", "")
    NOTION_WF_DB_ID: str = os.getenv("NOTION_WF_DB_ID", "")
    NOTION_API_VERSION: str = "2022-06-28"

    # ── Telegram ──────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Strategy ──────────────────────────────────────────────────────────────
    COMMODITY: str = "SILVERMIC"
    EXCHANGE: str = "MCX_FO"
    VARIANT: str = "V3"                   # CPR Band TC/BC rejection
    TIMEFRAME: str = "15minute"
    WFV_PROFILE_ID: str = os.getenv("WFV_PROFILE_ID", "silvermic_v3_default")
    WFV_STRATEGY_ID: str = os.getenv("WFV_STRATEGY_ID", "silvermic_cpr_band_v3")
    WFV_POSITION_PLAN_ID: str = os.getenv("WFV_POSITION_PLAN_ID", "partial_t1_trail")

    # Session window (IST)
    SESSION_START_H: int = 17
    SESSION_START_M: int = 0
    SESSION_END_H: int = 23
    SESSION_END_M: int = 0
    FORCE_CLOSE_H: int = 23
    FORCE_CLOSE_M: int = 0

    # Entry rules
    TOUCH_TOLERANCE_PCT: float = 0.0015  # 0.15% proximity counts as a touch
    MIN_TOUCH_GAP_BARS: int = 3          # bars between touch 1 and touch 2
    MAX_TRADES_PER_DAY: int = 2

    # Position sizing
    LOTS: int = 2
    LOT_SIZE: float = 1.0                # SILVERMIC: 1 kg/lot → ₹1 P&L per ₹1 move per lot

    # SuperTrend — entry SL
    ST_SL_LENGTH: int = 5
    ST_SL_FACTOR: float = 3.0

    # SuperTrend — trailing (after T1)
    ST_TRAIL_LENGTH: int = 5
    ST_TRAIL_FACTOR: float = 1.5

    # SL fallback if SuperTrend is invalid
    SL_FALLBACK_PCT: float = 0.008      # 0.8% beyond the touched level

    # SuperTrend warm-up: candles to pre-load before session
    ST_WARMUP_BARS: int = 60            # ~15 hours of 15m candles

    # ── MCX brokerage estimate ────────────────────────────────────────────────
    # Approx per-lot charges (brokerage + STT + transaction + GST + SEBI)
    FEES_PER_LOT: float = 50.0          # ₹50 per lot; update based on actual broker

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_DIR: Path = Path.home() / "Library" / "Logs" / "walk_forward"

    @classmethod
    def validate(cls):
        missing = []
        for field in ("UPSTOX_ACCESS_TOKEN", "NOTION_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            if not getattr(cls, field):
                missing.append(field)
        if missing:
            raise EnvironmentError(
                f"Missing required env vars: {', '.join(missing)}\n"
                f"Check {ENV_FILE}"
            )

    @classmethod
    def upstox_headers(cls) -> dict:
        return {
            "Authorization": f"Bearer {cls.UPSTOX_ACCESS_TOKEN}",
            "Accept": "application/json",
        }

    @classmethod
    def notion_headers(cls) -> dict:
        return {
            "Authorization": f"Bearer {cls.NOTION_API_KEY}",
            "Content-Type": "application/json",
            "Notion-Version": cls.NOTION_API_VERSION,
        }

    @classmethod
    def manual_instrument_override(cls, instrument_prefix: str) -> tuple[str, str, str]:
        token = instrument_prefix.upper().replace("-", "_")
        key = os.getenv(f"UPSTOX_{token}_KEY", "")
        symbol = os.getenv(f"UPSTOX_{token}_SYMBOL", "")
        expiry = os.getenv(f"UPSTOX_{token}_EXPIRY", "")

        if token == "SILVERMIC":
            key = key or cls.UPSTOX_SILVERMIC_KEY
            symbol = symbol or cls.UPSTOX_SILVERMIC_SYMBOL
            expiry = expiry or cls.UPSTOX_SILVERMIC_EXPIRY

        return key, symbol, expiry
