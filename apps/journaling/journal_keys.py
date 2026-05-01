#!/usr/bin/env python3
"""Helpers for stable Trading Journal dedupe keys."""

from __future__ import annotations

from datetime import date
from typing import Iterable, Optional


JOURNAL_KEY_PROPERTY = "Journal Key"
JOURNAL_KEY_VERSION = "v1"


def _normalize_float(value: Optional[float]) -> str:
    if value is None:
        return ""
    numeric = float(value)
    if numeric.is_integer():
        return str(int(numeric))
    return format(numeric, "g")


def _normalize_date(value: Optional[date]) -> str:
    return value.isoformat() if value else ""


def _normalize_text(value: Optional[str]) -> str:
    return (value or "").strip().upper()


def _normalize_ids(values: Optional[Iterable[str]]) -> str:
    normalized = sorted({
        str(value).strip()
        for value in (values or [])
        if str(value).strip()
    })
    return ",".join(normalized)


def build_journal_key(
    *,
    account: str,
    symbol: str,
    direction: str,
    entry_date: date,
    instrument_type: str,
    expiry_date: Optional[date] = None,
    option_type: Optional[str] = None,
    option_strike: Optional[float] = None,
    entry_source_ids: Optional[Iterable[str]] = None,
    exit_source_ids: Optional[Iterable[str]] = None,
) -> str:
    """Build a stable dedupe key for one journal row.

    The source ID lists are the strongest identity anchors. For open rows,
    `exit_source_ids` will usually be empty until the position is later closed.
    """

    parts = [
        JOURNAL_KEY_VERSION,
        f"acct={_normalize_text(account)}",
        f"sym={_normalize_text(symbol)}",
        f"dir={_normalize_text(direction)}",
        f"entry_date={_normalize_date(entry_date)}",
        f"inst={_normalize_text(instrument_type)}",
        f"expiry={_normalize_date(expiry_date)}",
        f"opt={_normalize_text(option_type)}",
        f"strike={_normalize_float(option_strike)}",
        f"entry_ids={_normalize_ids(entry_source_ids)}",
        f"exit_ids={_normalize_ids(exit_source_ids)}",
    ]
    return "|".join(parts)


def parse_journal_key(value: Optional[str]) -> dict[str, str]:
    text = (value or "").strip()
    if not text:
        return {}

    parts = text.split("|")
    data: dict[str, str] = {}
    if parts:
        data["version"] = parts[0]
    for part in parts[1:]:
        if "=" not in part:
            continue
        key, raw_value = part.split("=", 1)
        data[key] = raw_value
    return data


def extract_source_ids(value: Optional[str], field_name: str) -> list[str]:
    parsed = parse_journal_key(value)
    raw = parsed.get(field_name, "")
    return [item for item in raw.split(",") if item]
