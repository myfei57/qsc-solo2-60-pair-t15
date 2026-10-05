"""Tests for the time/zone segmented residual chlorine targets."""

from __future__ import annotations

import json
import tempfile
import unittest

from waterplant.audit import Auditor
from waterplant.chlor import (
    Doser,
    MODE_BOUNDARY,
    MODE_IMMEDIATE,
    ZONE_ENTRY,
    ZONE_FARPOINT,
    ZONE_PLANT,
    ConfigError,
    HandoverError,
    TargetHolder,
    build_config,
    config_from_payload,
    default_config,
)
from waterplant.store import Store

BANDS = [
    {"id": "night", "name": "night", "start": 0, "end": 360},
    {"id": "peak", "name": "peak", "start": 360, "end": 540},
    {"id": "day", "name": "day", "start": 540, "end": 1440},
]
ZONES = [
    {"id": ZONE_PLANT, "name": "plant", "order": 0},
    {"id": ZONE_ENTRY, "name": "entry", "order": 1},
    {"id": ZONE_FARPOINT, "name": "far", "order": 2},
]
MATRIX = {
    "night": {ZONE_PLANT: 0.4, ZONE_ENTRY: 0.35, ZONE_FARPOINT: 0.3},
    "peak": {ZONE_PLANT: 0.6, ZONE_ENTRY: 0.45, ZONE_FARPOINT: 0.3},
    "day": {ZONE_PLANT: 0.5, ZONE_ENTRY: 0.4, ZONE_FARPOINT: 0.3},
}


def config_payload(mode: str = MODE_BOUNDARY) -> dict:
    return {"mode": mode, "bands": BANDS, "zones": ZONES, "targets": MATRIX, "version": 1}


class ConfigCase(unittest.TestCase):
    def test_bands_partition_the_day_without_gap_or_overlap(self) -> None:
        config = build_config(MODE_BOUNDARY, BANDS, ZONES, MATRIX)
        # One and only one band owns every minute of the day.
        for minute in range(0, 1440):
            owners = [band for band in config.bands if band.contains(minute)]
            self.assertEqual(len(owners), 1, minute)
        self.assertEqual(config.band_at(359).id, "night")
        self.assertEqual(config.band_at(360).id, "peak")
        self.assertEqual(config.band_at(539).id, "peak")
        self.assertEqual(config.band_at(540).id, "day")
        self.assertEqual(config.band_at(1439).id, "day")

    def test_gaps_and_overlaps_are_rejected(self) -> None:
        gap = [
            {"id": "a", "start": 0, "end": 100},
            {"id": "b", "start": 120, "end": 1440},
        ]
        with self.assertRaises(ConfigError):
            build_config(MODE_BOUNDARY, gap, ZONES, {})
        overlap = [
            {"id": "a", "start": 0, "end": 120},
            {"id": "b", "start": 100, "end": 1440},
        ]
        with self.assertRaises(ConfigError):
            build_config(MODE_BOUNDARY, overlap, ZONES, {})
        wrong_start = [{"id": "a", "start": 5, "end": 1440}]
        with self.assertRaises(ConfigError):
            build_config(MODE_BOUNDARY, wrong_start, ZONES, {})
        wrong_end = [{"id": "a", "start": 0, "end": 1400}]
        with self.assertRaises(ConfigError):
            build_config(MODE_BOUNDARY, wrong_end, ZONES, {})

    def test_targets_must_cover_every_zone_and_be_in_range(self) -> None:
        missing_zone = {**MATRIX, "day": {ZONE_PLANT: 0.5, ZONE_ENTRY: 0.4}}
        with self.assertRaises(ConfigError):
            build_config(MODE_BOUNDARY, BANDS, ZONES, missing_zone)
        bad_value = {
            band: {**values, ZONE_PLANT: 0.0 if band == "day" else values[ZONE_PLANT]}
            for band, values in MATRIX.items()
        }
        with self.assertRaises(ConfigError):
            build_config(MODE_BOUNDARY, BANDS, ZONES, bad_value)

    def test_round_trip_document_parses(self) -> None:
        config = build_config(MODE_IMMEDIATE, BANDS, ZONES, MATRIX, version=3)
        reparsed = config_from_payload(config.as_dict())
        self.assertEqual(reparsed.fingerprint, config.fingerprint)
        self.assertEqual(reparsed.mode, MODE_IMMEDIATE)
        self.assertEqual(reparsed.version, 3)


class HolderCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.holder = TargetHolder(self.store)
        self.holder.replace_config(config_payload())

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_bootstrap_is_baseline_and_unchanged_is_idempotent(self) -> None:
        matrix = self.holder.bootstrap()
        self.assertEqual(matrix.values[ZONE_PLANT], 0.4)
        self.assertEqual(matrix.band_id, "night")
        # Re-evaluating the same band changes nothing and writes no audit.
        before = Auditor(self.store).count_by_kind()
        result = self.holder.tick(100)
        self.assertFalse(result.changed)
        self.assertEqual(result.reason, "already_in_force")
        self.assertEqual(Auditor(self.store).count_by_kind(), before)

    def test_switch_has_no_gap_and_audits_change(self) -> None:
        self.holder.bootstrap()
        result = self.holder.tick(360)
        self.assertTrue(result.changed)
        self.assertEqual(result.previous.band_id, "night")
        self.assertEqual(result.current.band_id, "peak")
        self.assertEqual(result.current.values[ZONE_PLANT], 0.6)
        # The single confirmed active value never disappears.
        self.assertEqual(self.holder.current_target(), 0.6)
        kinds = Auditor(self.store).count_by_kind()
        self.assertEqual(kinds["target_activated"], 1)
        activated = [
            e for e in Auditor(self.store).entries() if e.kind == "target_activated"
        ][-1]
        detail = json.loads(activated.detail)
        self.assertEqual(detail["from_band"], "night")
        self.assertEqual(detail["to_band"], "peak")
        self.assertTrue(detail["at"])

    def test_repeated_switch_trigger_after_change_is_idempotent(self) -> None:
        self.holder.bootstrap()
        self.holder.tick(360)
        once = Auditor(self.store).count_by_kind()["target_activated"]
        again = self.holder.tick(360)
        self.assertFalse(again.changed)
        self.assertEqual(
            Auditor(self.store).count_by_kind().get("target_activated"), once
        )
        third = self.holder.tick(400)
        self.assertFalse(third.changed)

    def test_zones_resolve_independently(self) -> None:
        self.holder.tick(360)
        self.assertEqual(self.holder.current_target(ZONE_PLANT), 0.6)
        self.assertEqual(self.holder.current_target(ZONE_ENTRY), 0.45)
        self.assertEqual(self.holder.current_target(ZONE_FARPOINT), 0.3)

    def test_override_wins_zone_by_zone_then_resumes_schedule(self) -> None:
        self.holder.tick(360)
        result = self.holder.set_override(
            {ZONE_FARPOINT: 0.5}, operator="ops", minute=360
        )
        self.assertTrue(result.changed)
        self.assertEqual(result.current.values[ZONE_FARPOINT], 0.5)
        self.assertEqual(result.current.sources[ZONE_FARPOINT], "override")
        # Other zones keep the schedule; there is no overlap tie to break.
        self.assertEqual(result.current.values[ZONE_PLANT], 0.6)
        self.assertEqual(result.current.sources[ZONE_PLANT], "schedule")
        released = self.holder.clear_override(360)
        self.assertTrue(released.changed)
        self.assertEqual(released.current.values[ZONE_FARPOINT], 0.3)
        self.assertEqual(released.current.sources[ZONE_FARPOINT], "schedule")

    def test_duplicate_override_request_is_idempotent(self) -> None:
        self.holder.tick(360)
        first = self.holder.set_override(
            {ZONE_PLANT: 0.9}, request_id="req-7", minute=360
        )
        self.assertTrue(first.changed)
        second = self.holder.set_override(
            {ZONE_PLANT: 0.9}, request_id="req-7", minute=360
        )
        self.assertFalse(second.changed)
        self.assertEqual(second.reason, "duplicate_request")

    def test_override_expires(self) -> None:
        clock = {"now": 1000.0}
        holder = TargetHolder(self.store, clock=lambda: clock["now"])
        holder.replace_config(config_payload())
        holder.bootstrap()
        holder.tick(360)
        holder.set_override({ZONE_PLANT: 0.9}, ttl_minutes=30, minute=360)
        self.assertEqual(holder.current_target(), 0.9)
        clock["now"] += 31 * 60
        result = holder.tick(360)
        self.assertTrue(result.changed)
        self.assertEqual(result.current.values[ZONE_PLANT], 0.6)

    def test_unreadable_config_holds_last_target_then_recovers_once(self) -> None:
        self.holder.bootstrap()
        self.holder.tick(360)
        self.assertEqual(self.holder.current_target(), 0.6)
        # Corrupt the document: partial failure, last confirmed target is held.
        self.store.put("chlor:target-config", "{not json")
        result = self.holder.tick(700)
        self.assertTrue(result.stale)
        self.assertEqual(result.reason, "config_unreadable_held_last")
        self.assertEqual(self.holder.current_target(), 0.6)
        self.assertEqual(self.holder.status()["state"], "STALE")
        stale_entries = len(Auditor(self.store).filter("target_stale"))
        # Repeating while still corrupt adds nothing new.
        self.holder.tick(800)
        self.assertEqual(len(Auditor(self.store).filter("target_stale")), stale_entries)
        # Restore: recovery is audited once and schedule resumes.
        self.store.put(
            "chlor:target-config", json.dumps(config_payload(), ensure_ascii=False)
        )
        recovered = self.holder.tick(700)
        self.assertFalse(recovered.stale)
        self.assertEqual(recovered.current.band_id, "day")
        self.assertEqual(self.holder.status()["state"], "ACTIVE")
        self.assertEqual(len(Auditor(self.store).filter("target_recovered")), 1)
        self.holder.tick(800)
        self.assertEqual(len(Auditor(self.store).filter("target_recovered")), 1)

    def test_replace_config_rejects_bad_document_and_keeps_running(self) -> None:
        good_fingerprint = self.holder.config().fingerprint
        bad = config_payload()
        bad["bands"] = [{"id": "x", "start": 0, "end": 100}]
        with self.assertRaises(ConfigError):
            self.holder.replace_config(bad)
        self.assertEqual(self.holder.config().fingerprint, good_fingerprint)
        self.assertTrue(Auditor(self.store).filter("target_config_rejected"))

    def test_replace_same_config_is_idempotent(self) -> None:
        first = self.holder.replace_config(config_payload())
        before = Auditor(self.store).count_by_kind().get("target_config_accepted", 0)
        second = self.holder.replace_config(config_payload())
        self.assertEqual(second.fingerprint, first.fingerprint)
        self.assertEqual(
            Auditor(self.store).count_by_kind().get("target_config_accepted", 0), before
        )

    def test_new_config_is_not_dosed_until_confirmed(self) -> None:
        self.holder.bootstrap()
        new = config_payload(MODE_IMMEDIATE)
        new["version"] = 2
        # Change the day plant target so the fingerprint differs.
        new["targets"]["day"][ZONE_PLANT] = 0.55
        self.holder.replace_config(new)
        # Before the handover the confirmed matrix is unchanged.
        self.assertEqual(self.holder.active().values[ZONE_PLANT], 0.4)
        promoted = self.holder.tick(700)
        self.assertTrue(promoted.changed)
        self.assertEqual(self.holder.active().values[ZONE_PLANT], 0.55)
        self.assertEqual(self.holder.active().version, 2)


class HandoverCase(unittest.TestCase):
    def test_promotion_requires_readback_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            holder = TargetHolder(store)
            holder.replace_config(config_payload())
            holder.bootstrap()

            real_put = store.put
            calls = {"n": 0}

            def flaky_put(key, value):
                if key == "chlor:target-active":
                    calls["n"] += 1
                    if calls["n"] == 1:
                        # Persist a different value than what was staged.
                        payload = json.loads(value)
                        payload["band_id"] = "day"
                        real_put(key, json.dumps(payload))
                        return
                real_put(key, value)

            store.put = flaky_put  # type: ignore[method-assign]
            with self.assertRaises(HandoverError):
                holder.tick(360)
            # Rolled back: doser still sees the previous confirmed target.
            self.assertEqual(holder.current_target(), 0.4)


class DoserCase(unittest.TestCase):
    def test_dose_and_target_share_one_confirmed_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            holder = TargetHolder(store)
            holder.replace_config(config_payload())
            holder.bootstrap()
            holder.tick(360)
            doser = Doser(store, targets=holder)
            snapshot = doser.active_snapshot()
            self.assertEqual(doser.apply_residual(), snapshot.values[ZONE_PLANT])
            basis = [e for e in Auditor(store).entries() if e.kind == "chlorine_dose_basis"][-1]
            detail = json.loads(basis.detail)
            self.assertEqual(detail["target"], detail["dose"])
            self.assertEqual(detail["unit"], "mg/L")
            self.assertEqual(detail["analyte"], "free_residual_chlorine")
            self.assertEqual(detail["fingerprint"], snapshot.fingerprint)
            self.assertEqual(detail["band_id"], "peak")


class DefaultCase(unittest.TestCase):
    def test_default_schedule_is_contiguous_and_night_is_lower(self) -> None:
        config = default_config()
        self.assertEqual(config.mode, MODE_BOUNDARY)
        for minute in range(1440):
            self.assertEqual(len([b for b in config.bands if b.contains(minute)]), 1)
        self.assertLess(config.value("night_a", ZONE_PLANT), config.value("morning", ZONE_PLANT))
        self.assertGreaterEqual(
            config.value("morning", ZONE_FARPOINT), config.value("night_a", ZONE_FARPOINT)
        )


if __name__ == "__main__":
    unittest.main()
