"""Time of day windows and schedule validation.

A schedule assigns residual targets to named network zones. Each zone holds a
list of windows covering the whole day with half open ``[start, end)``
boundaries, so at any minute exactly one window (and therefore one target) is
in force. Windows may wrap past midnight for the night period.
"""

from __future__ import annotations

from dataclasses import dataclass

MINUTES_PER_DAY = 1440


class ScheduleError(ValueError):
    """Raised when a residual target schedule is not usable."""


@dataclass(frozen=True)
class Window:
    """One half open time window ``[start, end)`` carrying a residual target.

    ``end`` may be up to ``2 * MINUTES_PER_DAY`` when the window wraps past
    midnight. ``start`` is always a minute of the current day.
    """

    start: int
    end: int
    target: float
    label: str = ""
    priority: int = 0

    def wraps(self) -> bool:
        return self.end > MINUTES_PER_DAY

    def is_full_day(self) -> bool:
        return self.start == 0 and self.end == MINUTES_PER_DAY

    def contains(self, minute: int) -> bool:
        if self.wraps():
            return minute >= self.start or minute < self.end - MINUTES_PER_DAY
        return self.start <= minute < self.end

    def as_dict(self) -> dict[str, object]:
        return {
            "start": hhmm(self.start),
            "end": hhmm(self.end),
            "target": self.target,
            "label": self.label,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class ZoneSchedule:
    """The windows assigned to one network zone."""

    zone: str
    dose_zone: bool
    windows: tuple[Window, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "zone": self.zone,
            "dose_zone": self.dose_zone,
            "windows": [window.as_dict() for window in self.windows],
        }


@dataclass(frozen=True)
class Schedule:
    """A validated, installable set of per zone target windows."""

    switch_mode: str
    dose_zone: str
    zones: tuple[ZoneSchedule, ...]
    updated_at: str = ""

    def zone_named(self, zone: str) -> ZoneSchedule | None:
        for candidate in self.zones:
            if candidate.zone == zone:
                return candidate
        return None

    def as_dict(self) -> dict[str, object]:
        return {
            "switch_mode": self.switch_mode,
            "dose_zone": self.dose_zone,
            "updated_at": self.updated_at,
            "zones": [zone.as_dict() for zone in self.zones],
        }


def hhmm(minute: int) -> str:
    """Render a possibly past midnight minute count as ``HH:MM``."""

    wrapped = minute % MINUTES_PER_DAY
    return f"{wrapped // 60:02d}:{wrapped % 60:02d}"


def parse_minute(value: object, field: str, *, allow_next_day: bool = False) -> int:
    """Accept ``HH:MM`` text or a raw minute count.

    Window ends may reach ``24:00`` (or, for a wrapping night window, the small
    hours of the next day); starts must stay within the current day.
    """

    if isinstance(value, bool):
        raise ScheduleError(f"{field} must be HH:MM or a minute count")
    if isinstance(value, int):
        minute = value
    elif isinstance(value, str) and ":" in value:
        text = value.strip()
        parts = text.split(":")
        if len(parts) != 2:
            raise ScheduleError(f"{field} must use HH:MM")
        try:
            hour, minute_of_hour = int(parts[0]), int(parts[1])
        except ValueError as exc:
            raise ScheduleError(f"{field} must use HH:MM") from exc
        if not 0 <= minute_of_hour < 60:
            raise ScheduleError(f"{field} is outside the supported range")
        minute = hour * 60 + minute_of_hour
    else:
        try:
            minute = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ScheduleError(f"{field} must be HH:MM or a minute count") from exc
    maximum = 2 * MINUTES_PER_DAY if allow_next_day else MINUTES_PER_DAY
    if not 0 <= minute <= maximum:
        raise ScheduleError(f"{field} is outside the supported range")
    return minute


def _validate_target(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            target = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ScheduleError("target must be a number") from exc
    else:
        target = float(value)
    if target <= 0:
        raise ScheduleError("target must be positive")
    if target > 10:
        raise ScheduleError("target exceeds the supported residual range")
    return target


def _validate_windows(raw_windows: object, zone: str) -> tuple[Window, ...]:
    if not isinstance(raw_windows, list) or not raw_windows:
        raise ScheduleError(f"zone {zone} needs at least one window")
    windows: list[Window] = []
    for item in raw_windows:
        if not isinstance(item, dict):
            raise ScheduleError(f"zone {zone} windows must be objects")
        start = parse_minute(item.get("start"), f"zone {zone} window start")
        end = parse_minute(
            item.get("end"), f"zone {zone} window end", allow_next_day=True
        )
        if not 0 <= start < MINUTES_PER_DAY:
            raise ScheduleError(f"zone {zone} window start must be within one day")
        if end <= start:
            # Natural night notation, e.g. 22:00-06:00 means 06:00 next day.
            end %= MINUTES_PER_DAY
            end += MINUTES_PER_DAY
        if end <= start or end > start + MINUTES_PER_DAY:
            raise ScheduleError(
                f"zone {zone} window {hhmm(start)}-{hhmm(end)} must be within 24h"
            )
        windows.append(
            Window(
                start=start,
                end=end,
                target=_validate_target(item.get("target")),
                label=str(item.get("label", "")),
                priority=int(item.get("priority", 0)),
            )
        )
    _assert_tiles_full_day(windows)
    return tuple(windows)


def _assert_tiles_full_day(windows: list[Window]) -> None:
    """Confirm the windows cover exactly one day with no gap or overlap."""

    full_day = [window for window in windows if window.is_full_day()]
    if full_day:
        if len(windows) > 1:
            raise ScheduleError("a full day window cannot be combined with others")
        return

    wrapping = [window for window in windows if window.wraps()]
    if len(wrapping) > 1:
        raise ScheduleError("only one window may wrap past midnight")
    if not wrapping:
        raise ScheduleError(
            "windows leave an uncovered overnight gap; the last window must wrap "
            "past midnight (e.g. 22:00-06:00)"
        )
    night = wrapping[0]

    # Shift every window onto a line whose zero is the first morning minute
    # (the night window's end). Each window keeps its duration; a start that
    # lands before the origin belongs to the following day.
    origin = night.end - MINUTES_PER_DAY  # e.g. 06:00 for a 22:00-06:00 window

    def offset(window: Window) -> tuple[int, int]:
        if window is night:
            start = window.start - origin
            if start < 0:
                start += MINUTES_PER_DAY
            return start, start + (window.end - window.start)
        start = window.start - origin
        if start < 0:
            start += MINUTES_PER_DAY
        return start, start + (window.end - window.start)

    ordered = sorted(
        (window for window in windows if window is not night),
        key=offset,
    )
    cursor = 0
    for window in ordered:
        start, end = offset(window)
        if start != cursor:
            kind = "overlap" if start < cursor else "leave a gap"
            raise ScheduleError(f"windows {kind} near {hhmm((cursor + origin) % MINUTES_PER_DAY)}")
        cursor = end
    night_start, night_end = offset(night)
    if night_start != cursor:
        kind = "overlap" if night_start < cursor else "leave a gap"
        raise ScheduleError(f"windows {kind} before the night window")
    if night_end != MINUTES_PER_DAY:
        raise ScheduleError("windows must cover exactly 24 hours")

def parse_schedule(payload: object) -> Schedule:
    """Validate raw console/config payload and build a schedule."""

    if not isinstance(payload, dict):
        raise ScheduleError("schedule must be an object")
    switch_mode = str(payload.get("switch_mode", SWITCH_CYCLE_BOUNDARY))
    if switch_mode not in SUPPORTED_SWITCH_MODES:
        raise ScheduleError(f"unsupported switch_mode {switch_mode}")
    dose_zone = str(payload.get("dose_zone", "")).strip()
    if not dose_zone:
        raise ScheduleError("dose_zone is required")
    raw_zones = payload.get("zones")
    if not isinstance(raw_zones, list) or not raw_zones:
        raise ScheduleError("schedule needs at least one zone")

    zones: list[ZoneSchedule] = []
    seen: set[str] = set()
    dose_seen = False
    for item in raw_zones:
        if not isinstance(item, dict):
            raise ScheduleError("zones must be objects")
        zone = str(item.get("zone", "")).strip()
        if not zone:
            raise ScheduleError("zone name is required")
        if zone in seen:
            raise ScheduleError(f"zone {zone} appears more than once")
        seen.add(zone)
        is_dose = zone == dose_zone
        if is_dose:
            dose_seen = True
        zones.append(
            ZoneSchedule(
                zone=zone,
                dose_zone=is_dose,
                windows=_validate_windows(item.get("windows"), zone),
            )
        )
    if not dose_seen:
        raise ScheduleError(f"dose_zone {dose_zone} has no windows")
    zones.sort(key=lambda candidate: (not candidate.dose_zone, candidate.zone))
    return Schedule(
        switch_mode=switch_mode,
        dose_zone=dose_zone,
        zones=tuple(zones),
        updated_at=str(payload.get("updated_at", "")),
    )


def window_for_minute(zone: ZoneSchedule, minute: int) -> Window:
    """Return the single window in force at ``minute``.

    Selection is deterministic even for malformed stored schedules: highest
    priority wins, then earliest start, then label, so two targets can never be
    "in force" at once.
    """

    matches = [window for window in zone.windows if window.contains(minute)]
    if not matches:  # corrupt schedule without tiling: fall back deterministically
        matches = list(zone.windows)
    return sorted(
        matches,
        key=lambda window: (-window.priority, window.start, window.end, window.label),
    )[0]


def schedule_fingerprint(schedule: Schedule) -> str:
    """Stable hash of the schedule content, excluding bookkeeping fields."""

    import hashlib

    content = [
        schedule.switch_mode,
        schedule.dose_zone,
        *[
            (
                zoned.zone,
                [
                    (
                        window.start,
                        window.end,
                        f"{window.target:.4f}",
                        window.label,
                        window.priority,
                    )
                    for window in zoned.windows
                ],
            )
            for zoned in schedule.zones
        ],
    ]
    return hashlib.sha256(repr(content).encode("utf-8")).hexdigest()[:16]


SWITCH_CYCLE_BOUNDARY = "cycle_boundary"
SUPPORTED_SWITCH_MODES = (SWITCH_CYCLE_BOUNDARY,)
