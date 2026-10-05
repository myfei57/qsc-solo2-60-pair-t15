"""Residual target manager: segmented targets, confirmation and fallbacks.

The manager owns the authoritative target the doser reads. Each control cycle
calls :meth:`ResidualTargets.refresh` first: it resolves the target for the
current time, persists the *active* snapshot, audits the change, and only then
returns. The doser doses against that confirmed snapshot, so a cycle can never
run without a target nor against an unconfirmed one.

Failure modes are explicit:

* no schedule configured  -> ``legacy`` mode, the plain clear well target is used;
* schedule unreadable/invalid -> ``stale`` mode, the last confirmed target stays
  frozen and the reason is recorded for the console;
* a readable schedule again -> ``active``/``legacy`` resumes, audited once.

Repeated triggers with the same schedule content or within the same effective
window do nothing, so recovery retries and duplicate API calls are idempotent.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Callable

from waterplant.audit import Auditor
from waterplant.store.store import Store

from .schedule import (
    Schedule,
    ScheduleError,
    Window,
    ZoneSchedule,
    hhmm,
    parse_schedule,
    schedule_fingerprint,
    window_for_minute,
)

SCHEDULE_KEY = "chlor:residual-schedule"
ACTIVE_KEY = "chlor:residual-active"

AUDIT_TARGET = "residual-target"

STATUS_LEGACY = "legacy"
STATUS_ACTIVE = "active"
STATUS_STALE = "stale"

REASON_NOT_CONFIGURED = "no schedule configured; using clear well target"


@dataclass(frozen=True)
class Moment:
    """Injectable wall clock instant, measured in minutes from an epoch day."""

    day: int
    minute: int
    iso: str

    @classmethod
    def now(cls) -> "Moment":
        epoch_seconds = time.time()
        struct = time.gmtime(epoch_seconds)
        day = int(epoch_seconds // 86400)
        return cls(
            day=day,
            minute=struct.tm_hour * 60 + struct.tm_min,
            iso=time.strftime("%Y-%m-%dT%H:%M:%SZ", struct),
        )


@dataclass(frozen=True)
class ActiveWindow:
    """The window that governs one zone right now."""

    target: float
    label: str
    start: str
    end: str

    def as_dict(self) -> dict[str, object]:
        return {"target": self.target, "label": self.label, "start": self.start, "end": self.end}


@dataclass(frozen=True)
class Activation:
    """Result of a refresh/confirmation step, consumed by the doser."""

    status: str
    target: float
    zone: str
    label: str
    window_start: str
    window_end: str
    effective_at: str
    reason: str
    changed: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "target": self.target,
            "zone": self.zone,
            "label": self.label,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "effective_at": self.effective_at,
            "reason": self.reason,
            "changed": self.changed,
        }


def _utc_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _occurrence(day: int, window: Window, minute: int) -> int:
    """Absolute day ordinal on which this governing window instance began.

    A wrapping window that owns minutes before its start (the pre midnight
    part) began on the previous day; minutes after its start began today.
    """

    if window.wraps() and minute < window.start:
        return day - 1
    return day


def recommended_schedule() -> Schedule:
    """Built in morning/evening peak plus night retention segmentation.

    Targets are examples at the dosing point (outlet) and the far network
    point; operators tune the numbers. The night window wraps midnight.
    """

    payload = {
        "switch_mode": "cycle_boundary",
        "dose_zone": "outlet",
        "zones": [
            {
                "zone": "outlet",
                "windows": [
                    {"start": "06:00", "end": "09:00", "target": 0.55, "label": "morning peak"},
                    {"start": "09:00", "end": "17:00", "target": 0.45, "label": "day"},
                    {"start": "17:00", "end": "22:00", "target": 0.55, "label": "evening peak"},
                    {"start": "22:00", "end": "06:00", "target": 0.35, "label": "night retention"},
                ],
            },
            {
                "zone": "network-end",
                "windows": [
                    {"start": "06:00", "end": "09:00", "target": 0.40, "label": "morning peak"},
                    {"start": "09:00", "end": "17:00", "target": 0.30, "label": "day"},
                    {"start": "17:00", "end": "22:00", "target": 0.40, "label": "evening peak"},
                    {"start": "22:00", "end": "06:00", "target": 0.25, "label": "night retention"},
                ],
            },
        ],
    }
    return parse_schedule(payload)


class ResidualTargets:
    """Holds the configured schedule and the last confirmed active target."""

    def __init__(
        self,
        store: Store,
        auditor: Auditor,
        fallback_target: Callable[[], float],
        clock: Callable[[], Moment] = Moment.now,
    ) -> None:
        self._store = store
        self._auditor = auditor
        self._fallback_target = fallback_target
        self._clock = clock
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ config

    def installed(self) -> Schedule | None:
        """Load and validate the configured schedule; ``None`` when absent."""

        raw, present = self._store.get(SCHEDULE_KEY)
        if not present:
            return None
        payload = json.loads(raw)
        return parse_schedule(payload)

    def configured(self) -> bool:
        """Whether a readable schedule governs targets right now."""

        try:
            return self.installed() is not None
        except (ValueError, TypeError):
            return True

    def install(self, payload: dict[str, object] | None = None) -> Schedule:
        """Validate, persist and immediately confirm a new schedule.

        A rejected payload leaves both the previous schedule and the currently
        active target untouched (partial failure, audited with the reason).
        """

        with self._lock:
            try:
                schedule = parse_schedule(payload)
            except ScheduleError as exc:
                self._auditor.record(
                    AUDIT_TARGET, json.dumps({"event": "rejected", "reason": str(exc)})
                )
                raise
            schedule = Schedule(
                switch_mode=schedule.switch_mode,
                dose_zone=schedule.dose_zone,
                zones=schedule.zones,
                updated_at=_utc_stamp(),
            )
            fingerprint = schedule_fingerprint(schedule)
            previous = self._load_active()
            idempotent = (
                previous is not None
                and previous.get("status") == STATUS_ACTIVE
                and previous.get("fingerprint") == fingerprint
            )
            self._store.put(
                SCHEDULE_KEY,
                json.dumps(schedule.as_dict(), ensure_ascii=False, sort_keys=True),
            )
            if not idempotent:
                self._auditor.record(
                    AUDIT_TARGET,
                    json.dumps(
                        {
                            "event": "installed",
                            "fingerprint": fingerprint,
                            "dose_zone": schedule.dose_zone,
                            "zones": [zone.zone for zone in schedule.zones],
                            "at": schedule.updated_at,
                        },
                        ensure_ascii=False,
                    ),
                )
            # Confirm against the current time so dosing can follow immediately.
            self.refresh()
            return schedule

    # ------------------------------------------------------------------ active

    def refresh(self) -> Activation:
        """Resolve and confirm the target for right now.

        Safe to call at the start of every cycle and to retry: identical
        schedule content and repeated calls inside one governing window are
        no-ops apart from returning the confirmed activation.
        """

        with self._lock:
            moment = self._clock()
            previous = self._load_active()
            try:
                schedule = self.installed()
            except (ValueError, TypeError) as exc:
                return self._hold_stale(previous, f"schedule unreadable: {exc}")
            if schedule is None:
                return self._resume_legacy(previous)
            return self._activate(schedule, previous, moment)

    def confirmed(self) -> Activation:
        """Read only view of the last confirmed target.

        Never writes and never performs a time switch: switches happen only at
        a cycle boundary (:meth:`refresh`). When no target has ever been
        confirmed the value is derived transiently from the fallback.
        """

        doc = self._load_active()
        if doc is not None:
            return self._activation_from_doc(doc, changed=False)
        moment = self._clock()
        try:
            schedule = self.installed()
        except (ValueError, TypeError) as exc:
            return Activation(
                status=STATUS_STALE,
                target=self._safe_fallback(),
                zone="",
                label="",
                window_start="",
                window_end="",
                effective_at="",
                reason=f"schedule unreadable: {exc}",
                changed=False,
            )
        if schedule is None:
            return self._legacy_activation(self._safe_fallback(), changed=False)
        return self._preview(schedule, moment)

    def current_resolution(self) -> tuple[Activation, bool]:
        """What the next cycle boundary would confirm, without confirming it.

        Returns ``(activation, switch_pending)``. The activation is transient;
        only :meth:`refresh` persists and audits it.
        """

        moment = self._clock()
        try:
            schedule = self.installed()
        except (ValueError, TypeError):
            return self.confirmed(), False
        if schedule is None:
            return self._legacy_activation(self._safe_fallback(), changed=False), False
        preview = self._preview(schedule, moment)
        confirmed = self.confirmed()
        pending = (
            confirmed.target != preview.target
            or confirmed.window_start != preview.window_start
            or confirmed.zone != preview.zone
        )
        return preview, pending

    # ---------------------------------------------------------------- internal

    def _resolve(self, schedule: Schedule, moment: Moment) -> tuple[ZoneSchedule, Window]:
        zone = schedule.zone_named(schedule.dose_zone)
        if zone is None:  # cannot happen for a validated schedule, stay safe
            raise ScheduleError(f"dose_zone {schedule.dose_zone} missing")
        return zone, window_for_minute(zone, moment.minute)

    def _preview(self, schedule: Schedule, moment: Moment) -> Activation:
        zone, window = self._resolve(schedule, moment)
        return Activation(
            status=STATUS_ACTIVE,
            target=window.target,
            zone=zone.zone,
            label=window.label,
            window_start=hhmm(window.start),
            window_end=hhmm(window.end),
            effective_at=moment.iso,
            reason="",
            changed=False,
        )

    def _activate(
        self, schedule: Schedule, previous: dict[str, object] | None, moment: Moment
    ) -> Activation:
        try:
            zone, window = self._resolve(schedule, moment)
        except ScheduleError as exc:
            return self._hold_stale(previous, str(exc))
        bucket = (
            f"{schedule_fingerprint(schedule)}:{zone.zone}:"
            f"{_occurrence(moment.day, window, moment.minute)}:{window.start}"
        )

        already = (
            previous is not None
            and previous.get("status") == STATUS_ACTIVE
            and previous.get("bucket") == bucket
        )
        effective_at = str(previous.get("effective_at", "")) if already else moment.iso
        window_start = hhmm(window.start)
        window_end = hhmm(window.end)
        if already:
            # Same governing window instance: nothing to persist or audit.
            return self._activation_from_doc(previous, changed=False)
        other_zones = self._active_zones(schedule, moment)
        active = {
            "status": STATUS_ACTIVE,
            "fingerprint": schedule_fingerprint(schedule),
            "bucket": bucket,
            "zone": zone.zone,
            "target": window.target,
            "label": window.label,
            "window_start": window_start,
            "window_end": window_end,
            "effective_at": effective_at,
            "switch_mode": schedule.switch_mode,
            "zones": other_zones,
            "confirmed_at": moment.iso,
        }
        self._store.put(ACTIVE_KEY, json.dumps(active, ensure_ascii=False, sort_keys=True))

        recovered = previous is not None and previous.get("status") == STATUS_STALE
        self._auditor.record(
            AUDIT_TARGET,
            json.dumps(
                {
                    "event": "recovered" if recovered else "activated",
                    "zone": zone.zone,
                    "target": window.target,
                    "label": window.label,
                    "window": [window_start, window_end],
                    "effective_at": effective_at,
                },
                ensure_ascii=False,
            ),
        )
        return Activation(
            status=STATUS_ACTIVE,
            target=window.target,
            zone=zone.zone,
            label=window.label,
            window_start=window_start,
            window_end=window_end,
            effective_at=effective_at,
            reason="",
            changed=True,
        )

    def _resume_legacy(self, previous: dict[str, object] | None) -> Activation:
        target = self._safe_fallback()
        if previous is None:
            # Never had a schedule: legacy mode is the default, persist nothing.
            return self._legacy_activation(target, changed=False)
        if previous.get("status") == STATUS_LEGACY:
            return self._activation_from_doc(previous, changed=False)
        # An active/stale schedule was removed: fall back to the clear well
        # target and record it once.
        self._write_legacy(target)
        self._auditor.record(
            AUDIT_TARGET,
            json.dumps({"event": "uninstalled", "target": target}, ensure_ascii=False),
        )
        return self._legacy_activation(target, changed=True)

    def _hold_stale(self, previous: dict[str, object] | None, reason: str) -> Activation:
        """Freeze on the last confirmed target and explain the partial failure."""

        if previous is not None and previous.get("status") == STATUS_STALE:
            # Already holding: retries are pure reads, no extra writes/audits.
            return self._activation_from_doc(previous, changed=False)

        frozen = {
            "status": STATUS_STALE,
            "fingerprint": str(previous.get("fingerprint", "")) if previous else "",
            "zone": str(previous.get("zone", "")) if previous else "",
            "target": previous.get("target") if previous else self._safe_fallback(),
            "label": str(previous.get("label", "")) if previous else "fallback",
            "window_start": str(previous.get("window_start", "")) if previous else "",
            "window_end": str(previous.get("window_end", "")) if previous else "",
            "effective_at": str(previous.get("effective_at", "")) if previous else "",
            "reason": reason,
            "detected_at": self._clock().iso,
        }
        if frozen["target"] is None:
            frozen["target"] = self._safe_fallback()
        self._store.put(ACTIVE_KEY, json.dumps(frozen, ensure_ascii=False, sort_keys=True))
        self._auditor.record(
            AUDIT_TARGET,
            json.dumps(
                {
                    "event": "stale",
                    "reason": reason,
                    "target": frozen["target"],
                    "zone": frozen["zone"],
                },
                ensure_ascii=False,
            ),
        )
        return self._activation_from_doc(frozen, changed=True)

    def _active_zones(self, schedule: Schedule, moment: Moment) -> dict[str, object]:
        zones: dict[str, object] = {}
        for zoned in schedule.zones:
            window = window_for_minute(zoned, moment.minute)
            zones[zoned.zone] = ActiveWindow(
                target=window.target,
                label=window.label,
                start=hhmm(window.start),
                end=hhmm(window.end),
            ).as_dict()
        return zones

    def _write_legacy(self, target: float) -> None:
        doc = {
            "status": STATUS_LEGACY,
            "zone": "clearwell",
            "target": target,
            "label": "clear well target",
            "window_start": "00:00",
            "window_end": "24:00",
            "effective_at": self._clock().iso,
            "reason": REASON_NOT_CONFIGURED,
        }
        self._store.put(ACTIVE_KEY, json.dumps(doc, ensure_ascii=False, sort_keys=True))

    def _legacy_activation(self, target: float, changed: bool) -> Activation:
        return Activation(
            status=STATUS_LEGACY,
            target=target,
            zone="clearwell",
            label="clear well target",
            window_start="00:00",
            window_end="24:00",
            effective_at="",
            reason=REASON_NOT_CONFIGURED,
            changed=changed,
        )

    def _safe_fallback(self) -> float:
        try:
            return float(self._fallback_target())
        except Exception:  # noqa: BLE001 - fallback must never break dosing
            return 0.5

    def _load_active(self) -> dict[str, object] | None:
        raw, present = self._store.get(ACTIVE_KEY)
        if not present:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def _activation_from_doc(
        self, doc: dict[str, object], changed: bool
    ) -> Activation:
        target = doc.get("target")
        return Activation(
            status=str(doc.get("status", STATUS_STALE)),
            target=float(target) if isinstance(target, (int, float)) else self._safe_fallback(),
            zone=str(doc.get("zone", "")),
            label=str(doc.get("label", "")),
            window_start=str(doc.get("window_start", "")),
            window_end=str(doc.get("window_end", "")),
            effective_at=str(doc.get("effective_at", "")),
            reason=str(doc.get("reason", "")),
            changed=changed,
        )

    # -------------------------------------------------------------- projections

    def status(self) -> dict[str, object]:
        """Read only console projection.

        Shows the configured schedule, the *confirmed* activation the current
        cycle is dosing against, what the next cycle boundary would resolve to,
        and whether a switch is pending there. Reading this never switches the
        target.
        """

        active_doc = self._load_active() or {}
        confirmed = self.confirmed()
        schedule: Schedule | None
        try:
            schedule = self.installed()
        except (ValueError, TypeError) as exc:
            schedule = None
            schedule_error = str(exc)
        else:
            schedule_error = ""
        upcoming, switch_pending = self.current_resolution()
        return {
            "switch_mode": "cycle_boundary",
            "schedule": schedule.as_dict() if schedule is not None else None,
            "schedule_error": schedule_error,
            "active": confirmed.as_dict(),
            "upcoming": upcoming.as_dict(),
            "switch_pending": switch_pending,
            "zones": active_doc.get("zones", {}),
        }

    def describe(self) -> str:
        activation = self.confirmed()
        return (
            f"residual-targets status={activation.status} zone={activation.zone} "
            f"target={activation.target:.4f}"
        )
