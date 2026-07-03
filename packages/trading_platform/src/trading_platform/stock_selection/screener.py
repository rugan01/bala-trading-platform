"""Parse historical fundamentals from cached Screener company pages."""

from __future__ import annotations

from datetime import date
import math
import re
from typing import Any

from bs4 import BeautifulSoup


SCREENER_BASE_URL = "https://www.screener.in/company/{symbol}/consolidated/"


def _clean_label(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("+", " ")).strip()


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip().replace(",", "").replace("%", "").replace("₹", "")
    if not text or text in {"-", "--"}:
        return None
    multiplier = 1.0
    if text.endswith("Cr."):
        text = text[:-3].strip()
    if text.endswith("K"):
        multiplier = 1_000
        text = text[:-1]
    elif text.endswith("M"):
        multiplier = 1_000_000
        text = text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        return float(match.group()) * multiplier if match else None


def _safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def _pct_change(current: float | None, prior: float | None) -> float | None:
    ratio = _safe_div(current, prior)
    return (ratio - 1) * 100 if ratio is not None else None


def _cagr(current: float | None, prior: float | None, years: int) -> float | None:
    if current is None or prior is None or current <= 0 or prior <= 0 or years <= 0:
        return None
    return (math.pow(current / prior, 1 / years) - 1) * 100


def _parse_table(table: Any) -> dict[str, dict[str, float | None]]:
    headers = [_clean_label(cell.get_text(" ", strip=True)) for cell in table.select("thead th")]
    if len(headers) < 2:
        first_row = table.find("tr")
        headers = [_clean_label(cell.get_text(" ", strip=True)) for cell in first_row.find_all(["th", "td"])]
    periods = headers[1:]
    rows: dict[str, dict[str, float | None]] = {}
    for tr in table.select("tbody tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        label = _clean_label(cells[0].get_text(" ", strip=True))
        rows[label] = {
            period: _number(cell.get_text(" ", strip=True))
            for period, cell in zip(periods, cells[1:])
        }
    return rows


def _section_table(soup: BeautifulSoup, section_id: str) -> dict[str, dict[str, float | None]]:
    section = soup.find(id=section_id)
    if not section:
        return {}
    table = section.find("table")
    return _parse_table(table) if table else {}


def _shareholding_table(soup: BeautifulSoup) -> dict[str, dict[str, float | None]]:
    section = soup.find(id="shareholding")
    if not section:
        return {}
    candidates = []
    for table in section.find_all("table"):
        headers = [cell.get_text(" ", strip=True) for cell in table.select("thead th")]
        annual_count = sum(bool(re.fullmatch(r"Mar \d{4}", header)) for header in headers)
        candidates.append((annual_count, table))
    if not candidates:
        return {}
    return _parse_table(max(candidates, key=lambda item: item[0])[1])


def _top_ratios(soup: BeautifulSoup) -> dict[str, float | None]:
    ratios: dict[str, float | None] = {}
    for li in soup.select("#top-ratios li"):
        name = li.find("span", class_="name")
        value = li.find("span", class_="number")
        if name and value:
            ratios[_clean_label(name.get_text(" ", strip=True))] = _number(
                value.get_text(" ", strip=True)
            )
    return ratios


def _period_date(period: str) -> str | None:
    match = re.fullmatch(r"Mar (\d{4})", period)
    return f"{match.group(1)}-03-31" if match else None


def _value(table: dict[str, dict[str, float | None]], row: str, period: str) -> float | None:
    return table.get(row, {}).get(period)


def _calculate_row(
    symbol: str,
    period: str,
    periods: list[str],
    pnl: dict[str, dict[str, float | None]],
    balance: dict[str, dict[str, float | None]],
    cashflow: dict[str, dict[str, float | None]],
    ratios: dict[str, dict[str, float | None]],
    shareholding: dict[str, dict[str, float | None]],
) -> dict[str, Any]:
    index = periods.index(period)
    prior = periods[index - 1] if index >= 1 else None
    prior_3y = periods[index - 3] if index >= 3 else None

    sales = _value(pnl, "Sales", period)
    operating_profit = _value(pnl, "Operating Profit", period)
    net_profit = _value(pnl, "Net Profit", period)
    equity = (_value(balance, "Equity Capital", period) or 0) + (
        _value(balance, "Reserves", period) or 0
    )
    borrowings = _value(balance, "Borrowings", period)
    total_assets = _value(balance, "Total Assets", period)
    cfo = _value(cashflow, "Cash from Operating Activity", period)
    fcf = _value(cashflow, "Free Cash Flow", period)

    return {
        "symbol": symbol,
        "as_of_date": _period_date(period),
        "sales_yoy": _pct_change(sales, _value(pnl, "Sales", prior)) if prior else None,
        "ebitda_yoy": _pct_change(
            operating_profit, _value(pnl, "Operating Profit", prior)
        )
        if prior
        else None,
        "profit_yoy": _pct_change(net_profit, _value(pnl, "Net Profit", prior))
        if prior
        else None,
        "sales_cagr_3y": _cagr(sales, _value(pnl, "Sales", prior_3y), 3)
        if prior_3y
        else None,
        "ebitda_cagr_3y": _cagr(
            operating_profit, _value(pnl, "Operating Profit", prior_3y), 3
        )
        if prior_3y
        else None,
        "profit_cagr_3y": _cagr(net_profit, _value(pnl, "Net Profit", prior_3y), 3)
        if prior_3y
        else None,
        "ebitda_margin": (_safe_div(operating_profit, sales) or 0) * 100
        if sales
        else None,
        "net_margin": (_safe_div(net_profit, sales) or 0) * 100 if sales else None,
        "cfo_to_pat": _safe_div(cfo, net_profit),
        "fcf_margin": (_safe_div(fcf, sales) or 0) * 100 if sales and fcf is not None else None,
        "asset_turnover": _safe_div(sales, total_assets),
        "debt_to_equity": _safe_div(borrowings, equity),
        "interest_coverage": _safe_div(
            operating_profit, _value(pnl, "Interest", period)
        ),
        "roce": _value(ratios, "ROCE %", period),
        "promoter_holding": _value(shareholding, "Promoters", period),
        "fii_holding": _value(shareholding, "FIIs", period),
        "dii_holding": _value(shareholding, "DIIs", period),
    }


def parse_screener_html(symbol: str, html: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return the latest scorecard row and point-in-time annual history."""
    soup = BeautifulSoup(html, "lxml")
    pnl = _section_table(soup, "profit-loss")
    balance = _section_table(soup, "balance-sheet")
    cashflow = _section_table(soup, "cash-flow")
    ratios = _section_table(soup, "ratios")
    shareholding = _shareholding_table(soup)

    sales_periods = list(pnl.get("Sales", {}).keys())
    periods = [period for period in sales_periods if _period_date(period)]
    if not periods:
        raise ValueError(f"No annual Screener history found for {symbol}")

    history = [
        _calculate_row(symbol, period, periods, pnl, balance, cashflow, ratios, shareholding)
        for period in periods
    ]
    latest = dict(history[-1])
    current = _top_ratios(soup)
    latest.update(
        {
            "as_of_date": date.today().isoformat(),
            "pe": current.get("Stock P/E"),
            "pb": _safe_div(current.get("Current Price"), current.get("Book Value")),
            "dividend_yield": current.get("Dividend Yield"),
            "roe": current.get("ROE"),
            "roce": current.get("ROCE") or latest.get("roce"),
            "source_url": SCREENER_BASE_URL.format(symbol=symbol.upper()),
        }
    )
    return latest, history

