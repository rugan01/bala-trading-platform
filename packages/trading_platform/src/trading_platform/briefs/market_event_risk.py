from __future__ import annotations

from datetime import date, timedelta
from typing import Any

MSCI_REVIEW_SOURCE_URL = "https://www.msci.com/eqb/pressreleases/archive/ir_dates.pdf"


# Hard-code the currently relevant review calendar so the automation does not
# depend on live scraping during the premarket workflow. Dates are the
# implementation sessions when passive close-flow risk is highest.
MSCI_REVIEW_CALENDAR: tuple[dict[str, Any], ...] = (
    {
        "implementation_date": "2025-02-28",
        "effective_date": "2025-03-03",
        "review_month": "February",
        "review_type": "Quarterly Index Review",
    },
    {
        "implementation_date": "2025-05-30",
        "effective_date": "2025-06-02",
        "review_month": "May",
        "review_type": "Semi-Annual Index Review",
    },
    {
        "implementation_date": "2025-08-29",
        "effective_date": "2025-09-01",
        "review_month": "August",
        "review_type": "Quarterly Index Review",
    },
    {
        "implementation_date": "2025-11-28",
        "effective_date": "2025-12-01",
        "review_month": "November",
        "review_type": "Semi-Annual Index Review",
    },
    {
        "implementation_date": "2026-02-27",
        "effective_date": "2026-03-02",
        "review_month": "February",
        "review_type": "Quarterly Index Review",
    },
    {
        "implementation_date": "2026-05-29",
        "effective_date": "2026-06-01",
        "review_month": "May",
        "review_type": "Semi-Annual Index Review",
    },
    {
        "implementation_date": "2026-08-31",
        "effective_date": "2026-09-01",
        "review_month": "August",
        "review_type": "Quarterly Index Review",
    },
    {
        "implementation_date": "2026-11-30",
        "effective_date": "2026-12-01",
        "review_month": "November",
        "review_type": "Semi-Annual Index Review",
    },
    {
        "implementation_date": "2027-02-26",
        "effective_date": "2027-03-01",
        "review_month": "February",
        "review_type": "Quarterly Index Review",
    },
    {
        "implementation_date": "2027-05-28",
        "effective_date": "2027-05-31",
        "review_month": "May",
        "review_type": "Semi-Annual Index Review",
    },
    {
        "implementation_date": "2027-08-31",
        "effective_date": "2027-09-01",
        "review_month": "August",
        "review_type": "Quarterly Index Review",
    },
    {
        "implementation_date": "2027-11-30",
        "effective_date": "2027-12-01",
        "review_month": "November",
        "review_type": "Semi-Annual Index Review",
    },
)


def _calendar_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in MSCI_REVIEW_CALENDAR:
        implementation_date = date.fromisoformat(row["implementation_date"])
        effective_date = date.fromisoformat(row["effective_date"])
        label = f"MSCI {row['review_month']} {implementation_date.year} {row['review_type']}"
        records.append(
            {
                **row,
                "implementation_date_obj": implementation_date,
                "effective_date_obj": effective_date,
                "label": label,
            }
        )
    return records


def get_msci_review_events(start_date: date, end_date: date) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for item in _calendar_records():
        impl = item["implementation_date_obj"]
        if not (start_date <= impl <= end_date):
            continue
        effective = item["effective_date_obj"]
        review_type = item["review_type"]
        events.append(
            {
                "date": impl.isoformat(),
                "window_end": effective.isoformat(),
                "source": "MSCI",
                "title": f"{review_type} implementation at close",
                "category": "passive_flow",
                "region": "Global / India",
                "market": "India equities / index close",
                "details": (
                    f"{item['label']}. Passive close-flow risk is elevated on the implementation session. "
                    "Avoid fresh index balancing legs after 14:30 IST."
                ),
                "url": MSCI_REVIEW_SOURCE_URL,
                "time_ist": None,
                "review_type": review_type,
                "implementation_date": impl.isoformat(),
                "effective_date": effective.isoformat(),
                "event_code": "MSCI_REBALANCE_CLOSE",
            }
        )
    return events


def get_msci_event_flags(target_date: date, *, lookahead_days: int = 5) -> dict[str, Any]:
    today_flags: list[dict[str, Any]] = []
    upcoming_flags: list[dict[str, Any]] = []

    for item in _calendar_records():
        impl = item["implementation_date_obj"]
        effective = item["effective_date_obj"]
        delta = (impl - target_date).days
        payload = {
            "event_code": "MSCI_REBALANCE_CLOSE",
            "label": item["label"],
            "review_type": item["review_type"],
            "implementation_date": impl.isoformat(),
            "effective_date": effective.isoformat(),
            "days_until": delta,
            "warning": "Avoid fresh index balancing legs after 14:30 IST on this session.",
            "source_url": MSCI_REVIEW_SOURCE_URL,
        }
        if delta == 0:
            today_flags.append(payload)
        elif 0 < delta <= lookahead_days:
            upcoming_flags.append(payload)

    return {
        "today": today_flags,
        "upcoming": sorted(upcoming_flags, key=lambda item: item["implementation_date"]),
    }


def format_msci_summary_lines(target_date: date, *, lookahead_days: int = 5) -> list[str]:
    flags = get_msci_event_flags(target_date, lookahead_days=lookahead_days)
    lines: list[str] = []

    if flags["today"]:
        event = flags["today"][0]
        lines.append(
            "  EVENT RISK: MSCI rebalance close today; no fresh index balancing legs after 14:30 IST"
        )
    elif flags["upcoming"]:
        event = flags["upcoming"][0]
        days = event["days_until"]
        when = "tomorrow" if days == 1 else f"in {days} days"
        lines.append(
            f"  EVENT RISK: MSCI rebalance close {when} ({event['implementation_date']}); plan lighter late-day index risk"
        )

    return lines


def get_passive_flow_day_notes(target_date: date) -> list[str]:
    flags = get_msci_event_flags(target_date)
    if not flags["today"]:
        return []
    return [
        "Passive-flow / MSCI implementation day: no fresh index balancing legs after 14:30 IST.",
        "Prefer reducing risk over rebuilding late-day index option structures.",
    ]
