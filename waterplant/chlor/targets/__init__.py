"""Time and zone segmented chlorine residual targets."""

from .manager import (
    ACTIVE_KEY,
    AUDIT_TARGET,
    SCHEDULE_KEY,
    Activation,
    ActiveWindow,
    Moment,
    ResidualTargets,
    recommended_schedule,
)
from .schedule import (
    MINUTES_PER_DAY,
    SWITCH_CYCLE_BOUNDARY,
    Schedule,
    ScheduleError,
    Window,
    ZoneSchedule,
    hhmm,
    parse_minute,
    parse_schedule,
    schedule_fingerprint,
    window_for_minute,
)

__all__ = [
    "ACTIVE_KEY",
    "AUDIT_TARGET",
    "MINUTES_PER_DAY",
    "SCHEDULE_KEY",
    "SWITCH_CYCLE_BOUNDARY",
    "Activation",
    "ActiveWindow",
    "Moment",
    "ResidualTargets",
    "Schedule",
    "ScheduleError",
    "Window",
    "ZoneSchedule",
    "hhmm",
    "parse_minute",
    "parse_schedule",
    "recommended_schedule",
    "schedule_fingerprint",
    "window_for_minute",
]
