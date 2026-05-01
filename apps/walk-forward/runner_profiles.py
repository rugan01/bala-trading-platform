"""
Runner profile registry for live paper walk-forward runs.

Profiles let the same engine launch different instrument/strategy/position-plan
combinations without editing the orchestration code.
"""

from __future__ import annotations

from models import RunnerProfile


_PROFILES: dict[str, RunnerProfile] = {}


def register_profile(profile: RunnerProfile) -> None:
    if not profile.profile_id:
        raise ValueError("profile_id cannot be empty")
    _PROFILES[profile.profile_id] = profile


def get_profile(profile_id: str) -> RunnerProfile:
    try:
        return _PROFILES[profile_id]
    except KeyError as exc:
        known = ", ".join(sorted(_PROFILES)) or "<none>"
        raise ValueError(f"Unknown runner profile '{profile_id}'. Registered profiles: {known}") from exc


def registered_profile_ids() -> list[str]:
    return sorted(_PROFILES)


def resolve_profiles(
    profile_ids: list[str] | None = None,
    strategy_id: str | None = None,
    position_plan_id: str | None = None,
) -> list[RunnerProfile]:
    selected = profile_ids or ["silvermic_v3_default"]
    return [
        get_profile(profile_id).with_overrides(
            strategy_id=strategy_id,
            position_plan_id=position_plan_id,
        )
        for profile_id in selected
    ]


register_profile(
    RunnerProfile(
        profile_id="silvermic_v3_default",
        instrument_key_prefix="SILVERMIC",
        strategy_id="silvermic_cpr_band_v3",
        position_plan_id="partial_t1_trail",
        display_prefix="WFV",
        runner_label="SILVERMIC V3",
    )
)

register_profile(
    RunnerProfile(
        profile_id="silvermic_breakout_research",
        instrument_key_prefix="SILVERMIC",
        strategy_id="silvermic_cpr_breakout_v1",
        position_plan_id="full_t1_exit",
        display_prefix="WFV",
        runner_label="SILVERMIC Breakout",
    )
)
