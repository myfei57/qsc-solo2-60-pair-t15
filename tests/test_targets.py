"""Segmented residual target behaviour."""

from __future__ import annotations

import json
import tempfile
import unittest

from waterplant.audit import Auditor
from waterplant.chlor import Doser as ChlorDoser
from waterplant.chlor.targets import (
    ACTIVE_KEY,
    AUDIT_TARGET,
    SCHEDULE_KEY,
    Moment,
    ResidualTargets,
    ScheduleError,
    Window,
    hhmm,
    parse_minute,
    parse_schedule,
    recommended_schedule,
    window_for_minute,
)
from waterplant.clearwell import Well
from waterplant.store import Store


def schedule_payload() -> dict[str, object]:
    return {
        "switch_mode": "cycle_boundary",
        "dose_zone": "outlet",
        "zones": [
            {
                "zone": "outlet",
                "windows": [
                    {"start": "06:00", "end": "09:00", "target": 0.55, "label": "morning"},
                    {"start": "09:00", "end": "17:00", "target": 0.45, "label": "day"},
                    {"start": "17:00", "end": "22:00", "target": 0.55, "label": "evening"},
                    {"start": "22:00", "end": "06:00", "target": 0.35, "label": "night"},
                ],
            },
            {
                "zone": "network-end",
                "windows": [
                    {"start": "06:00", "end": "22:00", "target": 0.4, "label": "peaks"},
                    {"start": "22:00", "end": "06:00", "target": 0.25, "label": "night"},
                ],
            },
        ],
    }


class Clock:
    def __init__(self, day: int = 100, minute: int = 0) -> None:
        self.day = day
        self.minute = minute

    def __call__(self) -> Moment:
        return Moment(day=self.day, minute=self.minute, iso=f"d{self.day}T{hhmm(self.minute)}")


def make_manager(store: Store, clock: Clock | None = None) -> ResidualTargets:
    auditor = Auditor(store)
    well = Well(store)
    return ResidualTargets(store, auditor, well.residual_target, clock or Clock())


class ScheduleValidationCase(unittest.TestCase):
    def test_minute_parsing_and_wrapping_window(self) -> None:
        self.assertEqual(parse_minute("06:30", "x"), 390)
        self.assertEqual(parse_minute(390, "x"), 390)
        with self.assertRaises(ScheduleError):
            parse_minute("25:00", "x")
        window = Window(start=22 * 60, end=24 * 60 + 6 * 60, target=0.3)
        self.assertTrue(window.wraps())
        self.assertTrue(window.contains(23 * 60))
        self.assertTrue(window.contains(0))
        self.assertFalse(window.contains(6 * 60))

    def test_valid_schedule_covers_full_day_including_night_wrap(self) -> None:
        schedule = parse_schedule(schedule_payload())
        self.assertEqual(schedule.dose_zone, "outlet")
        self.assertTrue(schedule.zone_named("outlet").dose_zone)
        outlet = schedule.zone_named("outlet")
        selected = window_for_minute(outlet, 23 * 60)
        self.assertEqual(selected.label, "night")
        self.assertEqual(window_for_minute(outlet, 6 * 60).label, "morning")
        self.assertEqual(window_for_minute(outlet, 9 * 60).label, "day")
        self.assertEqual(window_for_minute(outlet, 22 * 60 - 1).label, "evening")

    def test_gap_is_rejected(self) -> None:
        payload = schedule_payload()
        zones = payload["zones"]  # type: ignore[assignment]
        zones[0]["windows"][0]["end"] = "08:00"  # type: ignore[index]
        zones[0]["windows"][1]["start"] = "09:00"  # type: ignore[index]
        with self.assertRaises(ScheduleError) as ctx:
            parse_schedule(payload)
        self.assertIn("gap", str(ctx.exception))

    def test_overlap_is_rejected(self) -> None:
        payload = schedule_payload()
        zones = payload["zones"]  # type: ignore[assignment]
        zones[0]["windows"][1]["end"] = "18:00"  # type: ignore[index]
        with self.assertRaises(ScheduleError) as ScheduleErrorCtx:
            parse_schedule(payload)
        self.assertIn("overlap", str(ScheduleErrorCtx.exception))

    def test_missing_night_wrap_is_rejected_as_gap(self) -> None:
        payload = {
            "dose_zone": "outlet",
            "zones": [
                {
                    "zone": "outlet",
                    "windows": [
                        {"start": "06:00", "end": "22:00", "target": 0.5},
                    ],
                }
            ],
        }
        with self.assertRaises(ScheduleError):
            parse_schedule(payload)

    def test_target_range_and_dose_zone_validation(self) -> None:
        with self.assertRaises(ScheduleError):
            parse_schedule({"dose_zone": "outlet", "zones": []})
        payload = schedule_payload()
        payload["zones"][0]["windows"][0]["target"] = 0.0  # type: ignore[index]
        with self.assertRaises(ScheduleError):
            parse_schedule(payload)
        with self.assertRaises(ScheduleError):
            parse_schedule(
                {
                    "dose_zone": "missing",
                    "zones": [
                        {
                            "zone": "outlet",
                            "windows": [{"start": "00:00", "end": "24:00", "target": 0.5}],
                        }
                    ],
                }
            )

    def test_full_day_single_window_is_valid(self) -> None:
        schedule = parse_schedule(
            {
                "dose_zone": "outlet",
                "zones": [
                    {
                        "zone": "outlet",
                        "windows": [{"start": "00:00", "end": "24:00", "target": 0.5}],
                    }
                ],
            }
        )
        self.assertEqual(len(schedule.zone_named("outlet").windows), 1)


class TargetManagerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.clock = Clock(minute=12 * 60)
        self.manager = make_manager(self.store, self.clock)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def audit_details(self) -> list[dict[str, object]]:
        return [
            json.loads(entry.detail)
            for entry in Auditor(self.store).filter(AUDIT_TARGET)
        ]

    def test_legacy_mode_without_schedule_uses_well_target_and_writes_nothing(self) -> None:
        Well(self.store).set_residual_target(0.5)
        keys_before = set(self.store.keys())
        activation = self.manager.refresh()
        self.assertEqual(activation.status, "legacy")
        self.assertEqual(activation.target, 0.5)
        self.assertEqual(activation.reason, "no schedule configured; using clear well target")
        self.assertEqual(set(self.store.keys()), keys_before)
        self.assertEqual(self.audit_details(), [])

    def test_install_confirms_target_before_dosing(self) -> None:
        self.clock.minute = 12 * 60
        schedule = self.manager.install(schedule_payload())
        self.assertEqual(schedule.dose_zone, "outlet")
        activation = self.manager.confirmed()
        self.assertEqual(activation.status, "active")
        self.assertEqual(activation.target, 0.45)
        self.assertEqual(activation.zone, "outlet")
        self.assertEqual(activation.window_start, "09:00")
        self.assertEqual(activation.window_end, "17:00")
        events = [row["event"] for row in self.audit_details()]
        self.assertIn("installed", events)
        self.assertIn("activated", events)

    def test_segments_switch_on_time_with_exactly_one_target(self) -> None:
        self.manager.install(schedule_payload())
        for minute, expected in (
            (5 * 60 + 59, 0.35),
            (6 * 60, 0.55),
            (12 * 60, 0.45),
            (17 * 60, 0.55),
            (22 * 60, 0.35),
            (0, 0.35),
        ):
            self.clock.minute = minute
            self.assertEqual(self.manager.refresh().target, expected)

    def test_repeated_refresh_in_same_window_is_idempotent(self) -> None:
        self.manager.install(schedule_payload())
        self.clock.minute = 10 * 60
        self.manager.refresh()
        self.manager.refresh()
        activations = [row for row in self.audit_details() if row["event"] == "activated"]
        self.assertEqual(len(activations), 1)

    def test_window_change_audits_new_effective_time_once_per_day(self) -> None:
        self.manager.install(schedule_payload())
        self.clock.minute = 8 * 60 + 59
        self.manager.refresh()
        self.clock.minute = 9 * 60
        first = self.manager.refresh()
        self.assertTrue(first.changed)
        self.assertEqual(first.target, 0.45)
        self.assertEqual(first.effective_at, "d100T09:00")
        # The same window instance on later refreshes never re-activates.
        self.clock.minute = 10 * 60
        self.assertFalse(self.manager.refresh().changed)
        # Next day the morning window is a new occurrence -> audited again.
        self.clock.day = 101
        self.clock.minute = 6 * 60
        self.assertTrue(self.manager.refresh().changed)
        activations = [row for row in self.audit_details() if row["event"] == "activated"]
        self.assertEqual(len(activations), 4)

    def test_reinstall_identical_schedule_is_idempotent(self) -> None:
        self.manager.install(schedule_payload())
        self.manager.install(schedule_payload())
        installs = [row for row in self.audit_details() if row["event"] == "installed"]
        self.assertEqual(len(installs), 1)

    def test_rejected_payload_keeps_previous_target(self) -> None:
        self.manager.install(schedule_payload())
        self.clock.minute = 12 * 60
        good = self.manager.confirmed().target
        bad = schedule_payload()
        bad["zones"][0]["windows"][1]["end"] = "18:00"  # type: ignore[index]
        with self.assertRaises(ScheduleError):
            self.manager.install(bad)
        self.assertEqual(self.manager.confirmed().target, good)
        rejected = [row for row in self.audit_details() if row["event"] == "rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertIn("overlap", rejected[0]["reason"])

    def test_corrupt_config_freezes_last_target_and_then_recovers(self) -> None:
        self.manager.install(schedule_payload())
        self.clock.minute = 12 * 60
        self.assertEqual(self.manager.confirmed().target, 0.45)

        self.store.put(SCHEDULE_KEY, "{not json")
        stale = self.manager.refresh()
        self.assertEqual(stale.status, "stale")
        self.assertEqual(stale.target, 0.45)
        self.assertTrue(stale.changed)
        self.assertIn("unreadable", stale.reason)
        # Even at a time that would have switched segment, target stays frozen.
        self.clock.minute = 23 * 60
        again = self.manager.refresh()
        self.assertEqual(again.status, "stale")
        self.assertEqual(again.target, 0.45)
        self.assertFalse(again.changed)
        stale_events = [row for row in self.audit_details() if row["event"] == "stale"]
        self.assertEqual(len(stale_events), 1)

        # Restore: recovery activates once and is idempotent on retry.
        self.store.put(
            SCHEDULE_KEY,
            json.dumps(parse_schedule(schedule_payload()).as_dict()),
        )
        recovered = self.manager.refresh()
        self.assertEqual(recovered.status, "active")
        self.assertEqual(recovered.target, 0.35)
        self.assertTrue(recovered.changed)
        self.assertFalse(self.manager.refresh().changed)
        recoveries = [row for row in self.audit_details() if row["event"] == "recovered"]
        self.assertEqual(len(recoveries), 1)

    def test_invalid_stored_schedule_is_also_stale(self) -> None:
        self.manager.install(schedule_payload())
        invalid = schedule_payload()
        invalid["zones"][0]["windows"][0]["target"] = -1.0
        self.store.put(SCHEDULE_KEY, json.dumps(invalid))
        stale = self.manager.refresh()
        self.assertEqual(stale.status, "stale")
        self.assertGreater(stale.target, 0)

    def test_other_zones_are_reported_for_console(self) -> None:
        self.manager.install(schedule_payload())
        self.clock.minute = 12 * 60
        status = self.manager.status()
        self.assertEqual(status["active"]["target"], 0.45)
        zones = status["zones"]
        self.assertEqual(zones["outlet"]["target"], 0.45)
        self.assertEqual(zones["network-end"]["target"], 0.4)

    def test_night_window_occurrence_bucket_rolls_over_midnight(self) -> None:
        self.manager.install(schedule_payload())
        self.clock.day = 100
        self.clock.minute = 23 * 60
        self.manager.refresh()
        self.clock.day = 101
        self.clock.minute = 30
        self.assertFalse(self.manager.refresh().changed)  # same night occurrence
        self.clock.minute = 6 * 60
        self.assertTrue(self.manager.refresh().changed)  # morning begins

    def test_persistence_survives_restart_with_same_active_snapshot(self) -> None:
        self.manager.install(schedule_payload())
        self.clock.minute = 12 * 60
        self.manager.refresh()
        raw = self.store.get(ACTIVE_KEY)[0]
        self.assertTrue(raw)
        reopened = make_manager(self.store, self.clock)
        self.assertFalse(reopened.refresh().changed)

    def test_read_only_views_do_not_switch_target(self) -> None:
        self.manager.install(schedule_payload())
        self.clock.minute = 8 * 60 + 59
        self.manager.refresh()  # confirm morning segment at cycle boundary
        confirmed_target = self.manager.confirmed().target
        self.assertEqual(confirmed_target, 0.55)

        # Move into the day segment; a pure read keeps the confirmed target.
        self.clock.minute = 9 * 60 + 30
        self.assertEqual(self.manager.confirmed().target, 0.55)
        status = self.manager.status()
        self.assertEqual(status["active"]["target"], 0.55)
        self.assertTrue(status["switch_pending"])
        self.assertEqual(status["upcoming"]["target"], 0.45)

        # No activation audit from the read; the next boundary confirms once.
        before = len(
            [row for row in self.audit_details() if row["event"] == "activated"]
        )
        self.manager.status()
        self.assertEqual(
            len([row for row in self.audit_details() if row["event"] == "activated"]),
            before,
        )
        activated = self.manager.refresh()
        self.assertTrue(activated.changed)
        self.assertEqual(activated.target, 0.45)

    def test_recommended_schedule_tiles_the_day(self) -> None:
        schedule = recommended_schedule()
        self.assertEqual(schedule.dose_zone, "outlet")
        outlet = schedule.zone_named("outlet")
        self.assertEqual(window_for_minute(outlet, 2 * 60).label, "night retention")
        self.assertEqual(window_for_minute(outlet, 7 * 60).label, "morning peak")


class DoserIntegrationCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.clock = Clock(minute=12 * 60)
        auditor = Auditor(self.store)
        well = Well(self.store)
        self.targets = ResidualTargets(self.store, auditor, well.residual_target, self.clock)
        self.doser = ChlorDoser(self.store, self.clock)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_dose_matches_confirmed_target_single_read_path(self) -> None:
        self.targets.install(schedule_payload())
        self.clock.minute = 12 * 60
        self.assertEqual(self.doser.apply_residual(), 0.45)
        self.clock.minute = 23 * 60
        self.assertEqual(self.doser.apply_residual(), 0.35)
        last = Auditor(self.store).last()
        self.assertEqual(last.kind, "chlorine")
        self.assertEqual(last.detail, "0.3500")

    def test_doser_without_schedule_follows_well_target(self) -> None:
        Well(self.store).set_residual_target(0.6)
        self.assertEqual(self.doser.current_target(), 0.6)
        self.assertEqual(self.doser.apply_residual(), 0.6)

    def test_doser_holds_frozen_target_when_schedule_corrupt(self) -> None:
        self.targets.install(schedule_payload())
        self.clock.minute = 12 * 60
        self.assertEqual(self.doser.apply_residual(), 0.45)
        self.store.put(SCHEDULE_KEY, "garbage")
        self.clock.minute = 7 * 60
        self.assertEqual(self.doser.apply_residual(), 0.45)


if __name__ == "__main__":
    unittest.main()
