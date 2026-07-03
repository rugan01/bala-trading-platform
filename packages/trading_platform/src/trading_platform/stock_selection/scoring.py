"""Auditable StockEdge-like scoring for personal stock selection.

The formulas intentionally remain transparent. They reproduce the useful
decision process, not StockEdge's proprietary calculations.
"""

from __future__ import annotations

import csv
import math
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


HORIZONS = {"1m": 21, "3m": 63, "6m": 126}
FUNDAMENTAL_GROUPS = (
    "growth",
    "profitability",
    "efficiency",
    "solvency",
    "quality",
    "valuation",
)


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def mean_available(values: Iterable[float | None]) -> float | None:
    available = [value for value in values if value is not None]
    return statistics.fmean(available) if available else None


def period_return(closes: Sequence[float], sessions: int) -> float | None:
    if len(closes) <= sessions or closes[-sessions - 1] == 0:
        return None
    return (closes[-1] / closes[-sessions - 1] - 1.0) * 100.0


def sma(closes: Sequence[float], sessions: int) -> float | None:
    if len(closes) < sessions:
        return None
    return statistics.fmean(closes[-sessions:])


def rsi(closes: Sequence[float], sessions: int = 14) -> float | None:
    if len(closes) <= sessions:
        return None
    changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    gains = [max(change, 0.0) for change in changes[-sessions:]]
    losses = [max(-change, 0.0) for change in changes[-sessions:]]
    average_gain = statistics.fmean(gains)
    average_loss = statistics.fmean(losses)
    if average_loss == 0:
        return 100.0
    relative_strength = average_gain / average_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def annualized_volatility(closes: Sequence[float], sessions: int) -> float | None:
    sample = closes[-sessions - 1 :]
    if len(sample) < 10:
        return None
    daily_returns = [
        sample[index] / sample[index - 1] - 1.0
        for index in range(1, len(sample))
        if sample[index - 1] != 0
    ]
    if len(daily_returns) < 2:
        return None
    return statistics.stdev(daily_returns) * math.sqrt(252) * 100.0


def percentile_rank(value: float | None, population: Sequence[float]) -> float:
    if value is None or not population:
        return 50.0
    less = sum(item < value for item in population)
    equal = sum(item == value for item in population)
    return 100.0 * (less + 0.5 * equal) / len(population)


def linear_score(
    value: float | None,
    bad: float,
    good: float,
    *,
    lower_is_better: bool = False,
) -> float | None:
    if value is None:
        return None
    if good == bad:
        return 50.0
    score = (value - bad) / (good - bad) * 100.0
    if lower_is_better:
        score = 100.0 - score
    return clamp(score)


@dataclass
class StockSnapshot:
    symbol: str
    sector: str
    closes: list[float]
    benchmark_closes: list[float]
    average_turnover: float | None = None
    returns: dict[str, float | None] = field(default_factory=dict)
    relative_returns: dict[str, float | None] = field(default_factory=dict)
    trend_strength: dict[str, float | None] = field(default_factory=dict)
    high_proximity: dict[str, float | None] = field(default_factory=dict)
    volatility: dict[str, float | None] = field(default_factory=dict)
    rsi_14: float | None = None
    above_sma20: bool = False
    above_sma50: bool = False
    above_sma100: bool = False


@dataclass
class FundamentalScore:
    overall: float | None
    groups: dict[str, float | None]
    coverage: float
    red_flags: list[str] = field(default_factory=list)


@dataclass
class StockScorecard:
    symbol: str
    sector: str
    momentum_scores: dict[str, float]
    momentum_score: float
    fundamental_score: float | None
    fundamental_groups: dict[str, float | None]
    fundamental_coverage: float
    sector_score: float = 0.0
    liquidity_score: float = 50.0
    final_score: float = 0.0
    status: str = "RESEARCH_REQUIRED"
    reasons: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    snapshot: StockSnapshot | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.pop("snapshot", None)
        return result


@dataclass
class SectorScorecard:
    sector: str
    stock_count: int
    momentum_1m: float
    momentum_3m: float
    momentum_6m: float
    breadth_rs_positive: float
    breadth_rsi50: float
    breadth_sma20: float
    breadth_sma50: float
    breadth_sma100: float
    breadth_score: float
    sector_score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_snapshot(
    symbol: str,
    sector: str,
    closes: Sequence[float],
    benchmark_closes: Sequence[float],
    average_turnover: float | None = None,
) -> StockSnapshot:
    stock_closes = [float(value) for value in closes if safe_float(value) is not None]
    benchmark = [float(value) for value in benchmark_closes if safe_float(value) is not None]
    snapshot = StockSnapshot(
        symbol=symbol,
        sector=sector,
        closes=stock_closes,
        benchmark_closes=benchmark,
        average_turnover=average_turnover,
    )
    for label, sessions in HORIZONS.items():
        stock_return = period_return(stock_closes, sessions)
        benchmark_return = period_return(benchmark, sessions)
        moving_average = sma(stock_closes, sessions)
        horizon_high = max(stock_closes[-sessions:]) if len(stock_closes) >= sessions else None
        snapshot.returns[label] = stock_return
        snapshot.relative_returns[label] = (
            stock_return - benchmark_return
            if stock_return is not None and benchmark_return is not None
            else None
        )
        snapshot.trend_strength[label] = (
            (stock_closes[-1] / moving_average - 1.0) * 100.0
            if moving_average and stock_closes
            else None
        )
        snapshot.high_proximity[label] = (
            stock_closes[-1] / horizon_high * 100.0 if horizon_high and stock_closes else None
        )
        snapshot.volatility[label] = annualized_volatility(stock_closes, sessions)
    snapshot.rsi_14 = rsi(stock_closes)
    snapshot.above_sma20 = bool(sma(stock_closes, 20) and stock_closes[-1] > sma(stock_closes, 20))
    snapshot.above_sma50 = bool(sma(stock_closes, 50) and stock_closes[-1] > sma(stock_closes, 50))
    snapshot.above_sma100 = bool(
        sma(stock_closes, 100) and stock_closes[-1] > sma(stock_closes, 100)
    )
    return snapshot


def _metric_population(snapshots: Sequence[StockSnapshot], field_name: str, label: str) -> list[float]:
    values = [getattr(snapshot, field_name).get(label) for snapshot in snapshots]
    return [value for value in values if value is not None]


def score_momentum(snapshots: Sequence[StockSnapshot]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for label in HORIZONS:
        populations = {
            field_name: _metric_population(snapshots, field_name, label)
            for field_name in (
                "returns",
                "relative_returns",
                "trend_strength",
                "high_proximity",
                "volatility",
            )
        }
        for snapshot in snapshots:
            components = {
                "return": percentile_rank(snapshot.returns[label], populations["returns"]),
                "relative": percentile_rank(
                    snapshot.relative_returns[label], populations["relative_returns"]
                ),
                "trend": percentile_rank(
                    snapshot.trend_strength[label], populations["trend_strength"]
                ),
                "high": percentile_rank(
                    snapshot.high_proximity[label], populations["high_proximity"]
                ),
                "volatility": 100.0
                - percentile_rank(snapshot.volatility[label], populations["volatility"]),
            }
            score = (
                components["return"] * 0.35
                + components["relative"] * 0.25
                + components["trend"] * 0.20
                + components["high"] * 0.10
                + components["volatility"] * 0.10
            )
            result.setdefault(snapshot.symbol, {})[label] = round(score, 1)
    return result


def _raw_fundamental_groups(row: Mapping[str, Any]) -> dict[str, float | None]:
    pe = safe_float(row.get("pe"))
    industry_pe = safe_float(row.get("industry_pe"))
    valuation_pe_score = None
    if pe is not None and industry_pe and industry_pe > 0:
        valuation_pe_score = linear_score(pe / industry_pe, 1.5, 0.65, lower_is_better=False)

    groups = {
        "growth": mean_available(
            [
                linear_score(safe_float(row.get("sales_yoy")), 0, 25),
                linear_score(safe_float(row.get("ebitda_yoy")), 0, 25),
                linear_score(safe_float(row.get("profit_yoy")), 0, 25),
                linear_score(safe_float(row.get("sales_cagr_3y")), 0, 20),
                linear_score(safe_float(row.get("profit_cagr_3y")), 0, 20),
            ]
        ),
        "profitability": mean_available(
            [
                linear_score(safe_float(row.get("roe")), 5, 22),
                linear_score(safe_float(row.get("roce")), 7, 25),
                linear_score(safe_float(row.get("roa")), 2, 12),
                linear_score(safe_float(row.get("ebitda_margin")), 5, 30),
                linear_score(safe_float(row.get("net_margin")), 2, 18),
            ]
        ),
        "efficiency": mean_available(
            [
                linear_score(safe_float(row.get("cfo_to_pat")), 0.5, 1.2),
                linear_score(safe_float(row.get("fcf_margin")), 0, 15),
                linear_score(safe_float(row.get("asset_turnover")), 0.2, 1.5),
            ]
        ),
        "solvency": mean_available(
            [
                linear_score(
                    safe_float(row.get("debt_to_equity")), 2.0, 0.0, lower_is_better=False
                ),
                linear_score(safe_float(row.get("interest_coverage")), 1.5, 8.0),
                linear_score(safe_float(row.get("current_ratio")), 0.8, 2.0),
                linear_score(safe_float(row.get("quick_ratio")), 0.5, 1.5),
            ]
        ),
        "quality": mean_available(
            [
                linear_score(safe_float(row.get("promoter_holding")), 25, 70),
                linear_score(
                    safe_float(row.get("promoter_pledge")), 25, 0, lower_is_better=False
                ),
                linear_score(safe_float(row.get("fii_holding")), 0, 20),
                linear_score(safe_float(row.get("dii_holding")), 0, 20),
            ]
        ),
        "valuation": mean_available(
            [
                valuation_pe_score,
                linear_score(safe_float(row.get("peg")), 2.5, 0.7, lower_is_better=False),
                linear_score(safe_float(row.get("pb")), 8, 1, lower_is_better=False),
                linear_score(
                    safe_float(row.get("ev_ebitda")), 25, 7, lower_is_better=False
                ),
                linear_score(
                    safe_float(row.get("price_to_fcf")), 40, 10, lower_is_better=False
                ),
                linear_score(safe_float(row.get("dividend_yield")), 0, 3),
            ]
        ),
    }
    for group in FUNDAMENTAL_GROUPS:
        direct_score = safe_float(row.get(f"{group}_score"))
        if direct_score is not None:
            groups[group] = clamp(direct_score)
    return groups


def score_fundamentals(row: Mapping[str, Any] | None) -> FundamentalScore:
    if not row:
        return FundamentalScore(overall=None, groups={}, coverage=0.0)
    groups = _raw_fundamental_groups(row)
    available = [score for score in groups.values() if score is not None]
    coverage = len(available) / len(FUNDAMENTAL_GROUPS)
    red_flags: list[str] = []
    debt_to_equity = safe_float(row.get("debt_to_equity"))
    promoter_pledge = safe_float(row.get("promoter_pledge"))
    interest_coverage = safe_float(row.get("interest_coverage"))
    if debt_to_equity is not None and debt_to_equity > 2:
        red_flags.append("Debt/equity above 2")
    if promoter_pledge is not None and promoter_pledge > 20:
        red_flags.append("Promoter pledge above 20%")
    if interest_coverage is not None and interest_coverage < 1.5:
        red_flags.append("Weak interest coverage")
    return FundamentalScore(
        overall=round(statistics.fmean(available), 1) if available else None,
        groups=groups,
        coverage=round(coverage, 2),
        red_flags=red_flags,
    )


def load_fundamentals_csv(path: str | Path | None) -> dict[str, dict[str, str]]:
    if not path:
        return {}
    csv_path = Path(path).expanduser()
    if not csv_path.exists():
        return {}
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        return {
            row["symbol"].strip().upper(): row
            for row in csv.DictReader(handle)
            if row.get("symbol")
        }


def build_sector_scorecards(stock_cards: Sequence[StockScorecard]) -> list[SectorScorecard]:
    grouped: dict[str, list[StockScorecard]] = {}
    for card in stock_cards:
        grouped.setdefault(card.sector, []).append(card)
    sectors: list[SectorScorecard] = []
    for sector, cards in grouped.items():
        snapshots = [card.snapshot for card in cards if card.snapshot]
        count = len(cards)
        breadth = {
            "rs": 100.0
            * sum((snapshot.relative_returns.get("3m") or 0) > 0 for snapshot in snapshots)
            / max(len(snapshots), 1),
            "rsi": 100.0
            * sum((snapshot.rsi_14 or 0) > 50 for snapshot in snapshots)
            / max(len(snapshots), 1),
            "sma20": 100.0 * sum(snapshot.above_sma20 for snapshot in snapshots) / max(len(snapshots), 1),
            "sma50": 100.0 * sum(snapshot.above_sma50 for snapshot in snapshots) / max(len(snapshots), 1),
            "sma100": 100.0
            * sum(snapshot.above_sma100 for snapshot in snapshots)
            / max(len(snapshots), 1),
        }
        breadth_score = statistics.fmean(breadth.values())
        momentum = {
            label: statistics.median(card.momentum_scores[label] for card in cards)
            for label in HORIZONS
        }
        sector_score = (
            momentum["1m"] * 0.20
            + momentum["3m"] * 0.25
            + momentum["6m"] * 0.20
            + breadth_score * 0.35
        )
        sectors.append(
            SectorScorecard(
                sector=sector,
                stock_count=count,
                momentum_1m=round(momentum["1m"], 1),
                momentum_3m=round(momentum["3m"], 1),
                momentum_6m=round(momentum["6m"], 1),
                breadth_rs_positive=round(breadth["rs"], 1),
                breadth_rsi50=round(breadth["rsi"], 1),
                breadth_sma20=round(breadth["sma20"], 1),
                breadth_sma50=round(breadth["sma50"], 1),
                breadth_sma100=round(breadth["sma100"], 1),
                breadth_score=round(breadth_score, 1),
                sector_score=round(sector_score, 1),
            )
        )
    return sorted(sectors, key=lambda item: item.sector_score, reverse=True)


def build_stock_scorecards(
    snapshots: Sequence[StockSnapshot],
    fundamentals: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[StockScorecard], list[SectorScorecard]]:
    fundamentals = fundamentals or {}
    momentum_by_symbol = score_momentum(snapshots)
    turnover_population = [
        snapshot.average_turnover
        for snapshot in snapshots
        if snapshot.average_turnover is not None
    ]
    cards: list[StockScorecard] = []
    for snapshot in snapshots:
        momentum_scores = momentum_by_symbol[snapshot.symbol]
        momentum_score = (
            momentum_scores["1m"] * 0.35
            + momentum_scores["3m"] * 0.40
            + momentum_scores["6m"] * 0.25
        )
        fundamental = score_fundamentals(fundamentals.get(snapshot.symbol))
        liquidity_score = percentile_rank(snapshot.average_turnover, turnover_population)
        cards.append(
            StockScorecard(
                symbol=snapshot.symbol,
                sector=snapshot.sector,
                momentum_scores=momentum_scores,
                momentum_score=round(momentum_score, 1),
                fundamental_score=fundamental.overall,
                fundamental_groups=fundamental.groups,
                fundamental_coverage=fundamental.coverage,
                liquidity_score=round(liquidity_score, 1),
                red_flags=fundamental.red_flags,
                snapshot=snapshot,
            )
        )
    sectors = build_sector_scorecards(cards)
    sector_lookup = {sector.sector: sector for sector in sectors}
    for card in cards:
        card.sector_score = sector_lookup[card.sector].sector_score
        if card.fundamental_score is None:
            card.final_score = round(
                card.momentum_score * 0.55 + card.sector_score * 0.35 + card.liquidity_score * 0.10,
                1,
            )
            card.reasons.append("Fundamental data required before trade approval")
            if card.final_score >= 60:
                card.reasons.append("Technically promising; complete the fundamental gate")
            else:
                card.reasons.append("Technical score is below the candidate threshold")
            card.status = "RESEARCH_REQUIRED"
        else:
            card.final_score = round(
                card.momentum_score * 0.35
                + card.sector_score * 0.25
                + card.fundamental_score * 0.30
                + card.liquidity_score * 0.10,
                1,
            )
            if card.red_flags:
                card.status = "AVOID"
                card.reasons.extend(card.red_flags)
            elif (
                card.momentum_score >= 60
                and card.sector_score >= 60
                and card.fundamental_score >= 60
            ):
                card.status = "ELIGIBLE"
                card.reasons.append("Strong sector, momentum, and fundamentals")
            elif card.final_score >= 55:
                card.status = "WATCH"
                card.reasons.append("Promising, but one or more gates are below 60")
            else:
                card.status = "AVOID"
                card.reasons.append("Composite score below selection threshold")
    return sorted(cards, key=lambda item: item.final_score, reverse=True), sectors
