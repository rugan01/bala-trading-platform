from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from trading_platform.paths import RISK_RUNTIME_ROOT


TRADING_SYSTEM_ROOT = Path(
    os.getenv("TRADING_SYSTEM_ROOT", "/Users/rugan/balas-product-os/Projects/trading-system")
)
DAILY_PLAN_TEMPLATE = TRADING_SYSTEM_ROOT / "daily-trading-planning-sheet.md"
DAILY_PLAN_OUTPUT_ROOT = TRADING_SYSTEM_ROOT / "daily-plans"
WEEKLY_PLAN_LATEST_JSON = TRADING_SYSTEM_ROOT / "premarket" / "reports" / "weekly" / "weekly_review_and_plan_latest.json"
SUPPORTED_ACCOUNTS = ("BALA", "NIMMY")

STANDARD_CAMPAIGN_RISK = 1_000
A_PLUS_CAMPAIGN_RISK = 1_500
HARD_CAMPAIGN_CAP = 2_000
OPEN_RISK_CAP = 3_000
SOFT_DAILY_STOP = 2_000
HARD_DAILY_STOP = 3_000
WEEKLY_STOP = 8_000
MAX_POSITIONAL_STOCK_OPTION_CAMPAIGNS = 2
CASH_TRADING_CAPITAL = int(os.getenv("TRADING_CASH_CAPITAL", "200000"))
COLLATERAL_REFERENCE_VALUE = int(os.getenv("TRADING_COLLATERAL_REFERENCE_VALUE", "3000000"))
EXPIRY_PILOT_HARD_STOP = int(os.getenv("EXPIRY_PILOT_HARD_STOP", "5000"))
EXPIRY_FULL_SYSTEM_HARD_STOP = int(os.getenv("EXPIRY_FULL_SYSTEM_HARD_STOP", "10000"))
EXPIRY_SYMBOL_ALIASES = {
    "NIFTY": ("NIFTY",),
    "SENSEX": ("SENSEX", "BSX"),
    "BANKNIFTY": ("BANKNIFTY",),
}


WEEKDAY_RULES: dict[str, dict[str, Any]] = {
    "Monday": {
        "tier1_segment": "FO",
        "tier2_segment": "COM",
        "banned_segments": ["EQ"],
        "preferred_sessions": ["Morning Open", "Evening"],
        "main_session": "Morning Open",
        "commodity_allowed": True,
        "warnings": ["Commodity trades are satellite only; no reactive metals averaging."],
    },
    "Tuesday": {
        "tier1_segment": "FO",
        "tier2_segment": "COM",
        "banned_segments": ["EQ"],
        "preferred_sessions": ["Morning Open", "Evening"],
        "main_session": "Morning Open",
        "commodity_allowed": True,
        "warnings": ["Commodity idea must be probationary/minimum size after written setup."],
    },
    "Wednesday": {
        "tier1_segment": "FO",
        "tier2_segment": "",
        "banned_segments": ["EQ"],
        "preferred_sessions": ["Morning Open", "Late Morning"],
        "main_session": "Morning Open",
        "commodity_allowed": False,
        "warnings": ["Discretionary commodities are disabled unless explicitly approved in the plan."],
    },
    "Thursday": {
        "tier1_segment": "FO",
        "tier2_segment": "",
        "banned_segments": ["COM", "EQ"],
        "preferred_sessions": ["Morning Open", "Late Morning"],
        "main_session": "Morning Open",
        "commodity_allowed": False,
        "warnings": ["Thursday commodity ban active. Protect profitable F&O mornings from afternoon repair trades."],
    },
    "Friday": {
        "tier1_segment": "COM",
        "tier2_segment": "FO",
        "banned_segments": ["EQ"],
        "preferred_sessions": ["Evening", "Morning Open"],
        "main_session": "Evening",
        "commodity_allowed": True,
        "warnings": ["Friday F&O is reduced-size only and must be unusually clean."],
    },
}

WEEKEND_RULE = {
    "tier1_segment": "NO_TRADE",
    "tier2_segment": "",
    "banned_segments": ["FO", "COM", "EQ"],
    "preferred_sessions": [],
    "main_session": "Planning only",
    "commodity_allowed": False,
    "warnings": ["Weekend/non-trading day: planning only. Do not use this as a live-trading plan."],
}


@dataclass(frozen=True)
class DailyPlanWriteResult:
    date: str
    markdown_plan_path: str
    markdown_created: bool
    runtime_plan_paths: dict[str, str]
    runtime_archive_paths: dict[str, str]
    planning_hints: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "markdown_plan_path": self.markdown_plan_path,
            "markdown_created": self.markdown_created,
            "runtime_plan_paths": self.runtime_plan_paths,
            "runtime_archive_paths": self.runtime_archive_paths,
            "planning_hints": self.planning_hints,
        }


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def weekday_rule(plan_date: date) -> dict[str, Any]:
    return dict(WEEKDAY_RULES.get(plan_date.strftime("%A"), WEEKEND_RULE))


def _weekly_context(weekly_payload: dict[str, Any], plan_date: date) -> dict[str, Any]:
    week_ahead = weekly_payload.get("week_ahead_plan") or {}
    weekday_name = plan_date.strftime("%A")
    weekday_focus = ""
    for item in week_ahead.get("weekday_calendar") or []:
        if str(item.get("weekday") or "").lower() == weekday_name.lower():
            weekday_focus = str(item.get("focus") or "")
            break

    events = weekly_payload.get("weekly_events") or {}
    next_week_events = events.get("next_week_event_package") or {}
    return {
        "weekly_plan_generated_at": weekly_payload.get("generated_at"),
        "weekly_plan_path": str(WEEKLY_PLAN_LATEST_JSON) if weekly_payload else None,
        "weekday_focus": weekday_focus,
        "weekly_focus_lines": list(week_ahead.get("focus_lines") or []),
        "weekly_risk_focus": list(week_ahead.get("risk_focus") or []),
        "watchlist_earnings": list(week_ahead.get("watchlist_earnings") or []),
        "next_week_event_summary": next_week_events,
        "special_risk_flags": list(events.get("next_week_event_package", {}).get("special_risk_flags") or []),
    }


def build_planning_hints(
    plan_date: date,
    *,
    weekly_payload: dict[str, Any] | None = None,
    morning_brief_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rule = weekday_rule(plan_date)
    weekly_payload = weekly_payload or load_json_file(WEEKLY_PLAN_LATEST_JSON)
    morning_brief_payload = morning_brief_payload or {}
    weekly_context = _weekly_context(weekly_payload, plan_date)

    weekday_name = plan_date.strftime("%A")
    fno_allowed = "FO" not in set(rule["banned_segments"]) and rule["tier1_segment"] != "NO_TRADE"
    afternoon_lock_enabled = bool(fno_allowed)
    expiry_underlying = ""
    if weekday_name == "Tuesday":
        expiry_underlying = "NIFTY"
    elif weekday_name == "Thursday":
        expiry_underlying = "SENSEX"

    expiry_pilot_enabled = bool(expiry_underlying)

    hints = {
        "date": plan_date.isoformat(),
        "weekday": weekday_name,
        "tier1_segment": rule["tier1_segment"],
        "tier2_segment": rule["tier2_segment"],
        "banned_segments": rule["banned_segments"],
        "preferred_sessions": rule["preferred_sessions"],
        "main_session": rule["main_session"],
        "commodity_allowed": bool(rule["commodity_allowed"]),
        "commodity_thursday_ban": weekday_name == "Thursday",
        "friday_reduced_size_fno": weekday_name == "Friday",
        "morning_profit_lock_enabled": afternoon_lock_enabled,
        "max_afternoon_fno_giveback": 1_500,
        "max_equity_index_ideas": 2,
        "max_positional_stock_option_campaigns": MAX_POSITIONAL_STOCK_OPTION_CAMPAIGNS,
        "max_commodity_ideas": 2,
        "max_total_ideas": 4,
        "cash_trading_capital": CASH_TRADING_CAPITAL,
        "collateral_reference_value": COLLATERAL_REFERENCE_VALUE,
        "standard_campaign_risk": STANDARD_CAMPAIGN_RISK,
        "a_plus_campaign_risk": A_PLUS_CAMPAIGN_RISK,
        "hard_campaign_cap": HARD_CAMPAIGN_CAP,
        "open_risk_cap": OPEN_RISK_CAP,
        "soft_daily_stop": SOFT_DAILY_STOP,
        "hard_daily_stop": HARD_DAILY_STOP,
        "weekly_stop": WEEKLY_STOP,
        "collateral_policy": {
            "enabled": COLLATERAL_REFERENCE_VALUE > 0,
            "reference_value": COLLATERAL_REFERENCE_VALUE,
            "role": "margin_buffer_only",
            "risk_basis": "cash_trading_capital",
            "cash_trading_capital": CASH_TRADING_CAPITAL,
            "allowed_uses": [
                "intraday_defined_exit_expiry_structures",
                "hedged_short_option_margin",
                "margin_headroom_for_preplanned_positions",
                "defined_risk_positional_stock_option_spreads",
            ],
            "banned_uses": [
                "increase_lot_size_beyond_cash_risk_limit",
                "average_losing_positions",
                "carry_unplanned_overnight_futures",
                "fund_recovery_trades",
            ],
        },
        "expiry_pilot": {
            "enabled": expiry_pilot_enabled,
            "underlying": expiry_underlying,
            "phase": "pilot",
            "collateral_may_be_used_for_margin": expiry_pilot_enabled,
            "session_hard_stop": EXPIRY_PILOT_HARD_STOP,
            "full_system_hard_stop_after_automation": EXPIRY_FULL_SYSTEM_HARD_STOP,
            "max_new_entries": 2,
            "max_reentries": 1,
            "no_new_entries_after": "14:00",
            "hard_close_time": "15:00",
            "minimum_cooldown_after_stop_minutes": 30,
            "required_structure": "defined_exit_short_premium_or_defined_risk_spread",
            "notes": [
                "Collateral can satisfy margin, but losses remain limited by cash-risk rules.",
                "The Rs 10,000 full expiry cap is locked until auto-close / hard-stop automation is live.",
                "No third re-entry, no widening stops, no selling more premium into a losing short.",
            ],
        },
        "warnings": rule["warnings"],
        "weekly_context": weekly_context,
        "morning_context": {
            "brief_run_id": morning_brief_payload.get("brief_run_id"),
            "generated_at": morning_brief_payload.get("generated_at"),
            "market_phase": morning_brief_payload.get("market_phase"),
            "quick_summary_lines": list(morning_brief_payload.get("quick_summary_lines") or []),
            "event_risk": dict(morning_brief_payload.get("event_risk") or {}),
            "passive_flow_notes": list(morning_brief_payload.get("passive_flow_notes") or []),
        },
    }
    return hints


def render_markdown_plan(template: str, plan_date: date, hints: dict[str, Any]) -> str:
    rendered = template
    replacements = {
        "**Date:** __________": f"**Date:** {plan_date.isoformat()}",
        "**Weekday:** __________": f"**Weekday:** {plan_date.strftime('%A')}",
        "**Today's Tier 1 segment:** __________": f"**Today's Tier 1 segment:** `{hints['tier1_segment']}`",
        "**Today's Tier 2 segment (optional):** __________": f"**Today's Tier 2 segment (optional):** `{hints['tier2_segment'] or 'None'}`",
        "**Segments banned today:** __________": f"**Segments banned today:** `{', '.join(hints['banned_segments']) or 'None'}`",
        "**Main session to focus on:** `Morning Open / Late Morning / Afternoon / Evening`": (
            f"**Main session to focus on:** `{hints['main_session']}`"
        ),
        "**Max allowed afternoon giveback:** ₹__________": (
            f"**Max allowed afternoon giveback:** ₹{hints['max_afternoon_fno_giveback']:,}"
        ),
        "| Standard campaign risk | `₹1,000` | ₹__________ |": (
            f"| Standard campaign risk | `₹1,000` | ₹{hints['standard_campaign_risk']:,} |"
        ),
        "| A+ campaign risk | `₹1,500` | ₹__________ |": (
            f"| A+ campaign risk | `₹1,500` | ₹{hints['a_plus_campaign_risk']:,} |"
        ),
        "| Absolute hard max per campaign | `₹2,000` | ₹__________ |": (
            f"| Absolute hard max per campaign | `₹2,000` | ₹{hints['hard_campaign_cap']:,} |"
        ),
        "| Total open risk cap | `₹3,000` | ₹__________ |": (
            f"| Total open risk cap | `₹3,000` | ₹{hints['open_risk_cap']:,} |"
        ),
        "| Soft daily stop | `₹2,000` | ₹__________ |": (
            f"| Soft daily stop | `₹2,000` | ₹{hints['soft_daily_stop']:,} |"
        ),
        "| Hard daily stop | `₹3,000` | ₹__________ |": (
            f"| Hard daily stop | `₹3,000` | ₹{hints['hard_daily_stop']:,} |"
        ),
        "| Weekly stop | `₹8,000` | ₹__________ |": (
            f"| Weekly stop | `₹8,000` | ₹{hints['weekly_stop']:,} |"
        ),
    }
    for old, new in replacements.items():
        rendered = rendered.replace(old, new)

    context_lines = [
        "",
        "---",
        "",
        "## Automation Prefill",
        "",
        f"- Runtime plan generated at: `{datetime.now().astimezone().isoformat()}`",
        f"- Preferred sessions: `{', '.join(hints['preferred_sessions']) or 'None'}`",
        f"- Commodity allowed today: `{hints['commodity_allowed']}`",
        f"- Morning-profit lock enabled: `{hints['morning_profit_lock_enabled']}`",
        (
            f"- Positional stock-option campaign bucket: max "
            f"`{hints['max_positional_stock_option_campaigns']}` active defined-risk campaigns; "
            "separate from the intraday index F&O idea limit"
        ),
        (
            f"- Collateral policy: `margin buffer only` on reference collateral "
            f"`₹{hints['collateral_reference_value']:,}`; risk remains based on "
            f"`₹{hints['cash_trading_capital']:,}` cash capital"
        ),
    ]
    expiry_pilot = hints.get("expiry_pilot") or {}
    if expiry_pilot.get("enabled"):
        context_lines.extend(
            [
                (
                    f"- Expiry pilot today: `{expiry_pilot.get('underlying')}` | "
                    f"pilot hard stop `₹{int(expiry_pilot.get('session_hard_stop') or 0):,}` | "
                    f"hard close `{expiry_pilot.get('hard_close_time')}`"
                ),
                (
                    f"- Expiry guardrails: max `{expiry_pilot.get('max_new_entries')}` new entries, "
                    f"max `{expiry_pilot.get('max_reentries')}` re-entry, no new entries after "
                    f"`{expiry_pilot.get('no_new_entries_after')}`"
                ),
            ]
        )
    weekday_focus = hints.get("weekly_context", {}).get("weekday_focus")
    if weekday_focus:
        context_lines.append(f"- Weekly focus for this weekday: {weekday_focus}")
    for warning in hints.get("warnings") or []:
        context_lines.append(f"- Guardrail: {warning}")
    passive_flow_notes = hints.get("morning_context", {}).get("passive_flow_notes") or []
    for note in passive_flow_notes:
        context_lines.append(f"- Event-risk guardrail: {note}")
    risk_focus = hints.get("weekly_context", {}).get("weekly_risk_focus") or []
    if risk_focus:
        context_lines.append(f"- Weekly risk focus: {risk_focus[0]}")
    special_risk_flags = hints.get("weekly_context", {}).get("special_risk_flags") or []
    if special_risk_flags:
        first_flag = special_risk_flags[0]
        context_lines.append(f"- Upcoming special risk day: {first_flag.get('date')} | {first_flag.get('message')}")
    context_lines.append("")
    context_lines.append("Actual trade ideas, entry zones, stops, and re-entry triggers must still be filled manually before trading.")
    context_lines.append("")
    return rendered.rstrip() + "\n" + "\n".join(context_lines)


def _empty_idea_slots() -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []
    for segment in ("FO", "FO", "STOCK_OPTIONS", "STOCK_OPTIONS", "COM", "COM"):
        segment_slots = [slot for slot in slots if slot["segment"] == segment]
        slots.append(
            {
                "slot": len(segment_slots) + 1,
                "segment": segment,
                "symbol": "",
                "structure": "",
                "thesis": "",
                "entry_zone": "",
                "stop_rule": "",
                "max_loss": None,
                "hold_rule": "",
                "reentry_rule": "",
                "used": False,
                "reentry_used": False,
            }
        )
    return slots


def build_runtime_plan(
    account: str,
    plan_date: date,
    hints: dict[str, Any],
    *,
    markdown_plan_path: Path,
    source: str,
) -> dict[str, Any]:
    account = account.upper()
    return {
        "date": plan_date.isoformat(),
        "account": account,
        "weekday": plan_date.strftime("%A"),
        "generated_at": datetime.now().astimezone().isoformat(),
        "source": source,
        "markdown_plan_path": str(markdown_plan_path),
        "tier1_segment": hints["tier1_segment"],
        "tier2_segment": hints["tier2_segment"],
        "banned_segments": hints["banned_segments"],
        "main_session": hints["main_session"],
        "preferred_sessions": hints["preferred_sessions"],
        "commodity_allowed": hints["commodity_allowed"],
        "cash_trading_capital": hints["cash_trading_capital"],
        "collateral_reference_value": hints["collateral_reference_value"],
        "collateral_policy": hints["collateral_policy"],
        "expiry_pilot": hints["expiry_pilot"],
        "morning_profit_lock_enabled": hints["morning_profit_lock_enabled"],
        "max_afternoon_fno_giveback": hints["max_afternoon_fno_giveback"],
        "max_equity_index_ideas": hints["max_equity_index_ideas"],
        "max_positional_stock_option_campaigns": hints["max_positional_stock_option_campaigns"],
        "max_commodity_ideas": hints["max_commodity_ideas"],
        "max_total_ideas": hints["max_total_ideas"],
        "standard_campaign_risk": hints["standard_campaign_risk"],
        "a_plus_campaign_risk": hints["a_plus_campaign_risk"],
        "hard_campaign_cap": hints["hard_campaign_cap"],
        "open_risk_cap": hints["open_risk_cap"],
        "soft_daily_stop": hints["soft_daily_stop"],
        "hard_daily_stop": hints["hard_daily_stop"],
        "weekly_stop": hints["weekly_stop"],
        "ideas": _empty_idea_slots(),
        "state": {
            "frozen_for_new_trades": False,
            "freeze_reason": None,
            "hard_stop_reached": False,
            "morning_fno_profit_anchor": None,
            "afternoon_fno_ideas_used": 0,
            "idea_counts": {"FO": 0, "STOCK_OPTIONS": 0, "COM": 0, "EQ": 0, "TOTAL": 0},
            "alerts_sent": [],
        },
        "source_context": {
            "weekly_context": hints.get("weekly_context") or {},
            "morning_context": hints.get("morning_context") or {},
            "warnings": hints.get("warnings") or [],
        },
    }


def normalize_accounts(accounts: Iterable[str] | None) -> tuple[str, ...]:
    if not accounts:
        return SUPPORTED_ACCOUNTS
    normalized = tuple(str(account).upper() for account in accounts)
    invalid = [account for account in normalized if account not in SUPPORTED_ACCOUNTS]
    if invalid:
        raise ValueError(f"Unsupported account(s): {', '.join(invalid)}")
    return normalized


def create_daily_trade_plan_files(
    *,
    plan_date: date,
    accounts: Iterable[str] | None = None,
    source: str = "daily_plan_autofill",
    force_markdown: bool = False,
    template_path: Path = DAILY_PLAN_TEMPLATE,
    output_root: Path = DAILY_PLAN_OUTPUT_ROOT,
    runtime_root: Path = RISK_RUNTIME_ROOT,
    weekly_payload: dict[str, Any] | None = None,
    morning_brief_payload: dict[str, Any] | None = None,
) -> DailyPlanWriteResult:
    if not template_path.exists():
        raise FileNotFoundError(f"Missing daily plan template: {template_path}")

    hints = build_planning_hints(
        plan_date,
        weekly_payload=weekly_payload,
        morning_brief_payload=morning_brief_payload,
    )
    year_dir = output_root / str(plan_date.year)
    year_dir.mkdir(parents=True, exist_ok=True)
    markdown_plan_path = year_dir / f"{plan_date.isoformat()}-daily-trading-plan.md"

    markdown_created = False
    if force_markdown or not markdown_plan_path.exists():
        rendered = render_markdown_plan(template_path.read_text(encoding="utf-8"), plan_date, hints)
        markdown_plan_path.write_text(rendered, encoding="utf-8")
        markdown_created = True

    runtime_root.mkdir(parents=True, exist_ok=True)
    runtime_plan_paths: dict[str, str] = {}
    runtime_archive_paths: dict[str, str] = {}
    for account in normalize_accounts(accounts):
        runtime_payload = build_runtime_plan(
            account,
            plan_date,
            hints,
            markdown_plan_path=markdown_plan_path,
            source=source,
        )
        latest_path = runtime_root / f"daily_trade_plan_{account.lower()}.json"
        archive_path = runtime_root / f"daily_trade_plan_{plan_date.isoformat()}_{account.lower()}.json"
        payload_text = json.dumps(runtime_payload, indent=2, ensure_ascii=False)
        latest_path.write_text(payload_text, encoding="utf-8")
        archive_path.write_text(payload_text, encoding="utf-8")
        runtime_plan_paths[account] = str(latest_path)
        runtime_archive_paths[account] = str(archive_path)

    return DailyPlanWriteResult(
        date=plan_date.isoformat(),
        markdown_plan_path=str(markdown_plan_path),
        markdown_created=markdown_created,
        runtime_plan_paths=runtime_plan_paths,
        runtime_archive_paths=runtime_archive_paths,
        planning_hints=hints,
    )


def load_runtime_plan(account: str, *, runtime_root: Path = RISK_RUNTIME_ROOT, plan_date: date | None = None) -> dict[str, Any]:
    path = runtime_root / f"daily_trade_plan_{account.lower()}.json"
    payload = load_json_file(path)
    if not payload:
        return {}
    expected_date = (plan_date or date.today()).isoformat()
    if payload.get("date") != expected_date:
        return {}
    return payload


def runtime_plan_path(account: str, *, runtime_root: Path = RISK_RUNTIME_ROOT) -> Path:
    return runtime_root / f"daily_trade_plan_{account.lower()}.json"


def write_runtime_plan(payload: dict[str, Any], *, runtime_root: Path = RISK_RUNTIME_ROOT) -> None:
    account = str(payload.get("account") or "").lower()
    if not account:
        raise ValueError("Runtime plan payload is missing account")
    path = runtime_plan_path(account, runtime_root=runtime_root)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def update_runtime_plan_state(
    account: str,
    updates: dict[str, Any],
    *,
    runtime_root: Path = RISK_RUNTIME_ROOT,
    plan_date: date | None = None,
) -> dict[str, Any]:
    payload = load_runtime_plan(account, runtime_root=runtime_root, plan_date=plan_date)
    if not payload:
        return {}
    state = payload.setdefault("state", {})
    state.update(updates)
    state["updated_at"] = datetime.now().astimezone().isoformat()
    write_runtime_plan(payload, runtime_root=runtime_root)
    return payload


def normalize_trading_symbol(symbol: str) -> str:
    return "".join(ch for ch in str(symbol or "").upper() if ch.isalnum())


def symbol_matches_expiry_underlying(symbol: str, underlying: str) -> bool:
    normalized = normalize_trading_symbol(symbol)
    aliases = EXPIRY_SYMBOL_ALIASES.get(str(underlying or "").upper(), (str(underlying or "").upper(),))
    return any(normalized.startswith(alias) for alias in aliases if alias)
