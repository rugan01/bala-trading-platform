from __future__ import annotations

import gzip
import json
from typing import Any, Iterable

import requests


UPSTOX_NSE_INSTRUMENTS_URL = (
    "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
)


def load_nse_instruments(timeout: int = 60) -> list[dict[str, Any]]:
    response = requests.get(UPSTOX_NSE_INSTRUMENTS_URL, timeout=timeout)
    response.raise_for_status()
    return json.loads(gzip.decompress(response.content))


def current_fno_stock_symbols(
    instruments: Iterable[dict[str, Any]] | None = None,
    timeout: int = 60,
) -> list[str]:
    rows = instruments if instruments is not None else load_nse_instruments(timeout=timeout)
    return sorted(
        {
            str(row["underlying_symbol"]).strip().upper()
            for row in rows
            if row.get("segment") == "NSE_FO"
            and row.get("underlying_type") == "EQUITY"
            and row.get("underlying_symbol")
        }
    )
