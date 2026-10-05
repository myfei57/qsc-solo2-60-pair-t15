"""End to end tests for the HTTP control console."""

from __future__ import annotations

import json
import tempfile
import unittest

from waterplant.console.routes import route_table
from waterplant.console.seed import seed_defaults
from waterplant.console.server import Server
from waterplant.store import Store


class ConsoleCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        seed_defaults(self.store)
        self.server = Server(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None):
        response = self.server.dispatch(method, path, payload)
        body = response.body.decode("utf-8")
        parsed = json.loads(body) if response.content_type.startswith("application/json") else body
        return response.status, parsed

    def test_health_snapshot_and_reports(self) -> None:
        status, health = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "ok")
        self.assertIn("intake:flow", health["keys"])

        status, checks = self.call("GET", "/health/checks")
        self.assertEqual(status, 200)
        self.assertEqual({check["name"] for check in checks["checks"]}, {
            "store",
            "calibration",
            "residual",
            "quota",
            "intake",
            "ph",
            "inventory",
        })

        status, snapshot = self.call("GET", "/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(
            set(snapshot),
            {
                "pipeline",
                "store",
                "intake",
                "coag",
                "chlor",
                "residual_targets",
                "filter",
                "backwash",
                "turbidity",
                "flow",
                "clearwell",
                "quota",
                "audit",
                "ph",
                "schedule",
                "trend",
                "inventory",
            },
        )

        status, describe = self.call("GET", "/describe")
        self.assertEqual(status, 200)
        self.assertIn("pipeline", describe)

        status, report = self.call("GET", "/report")
        self.assertEqual(status, 200)
        self.assertIn("waterplant control report", report)

    def test_metadata_routes(self) -> None:
        status, pipeline = self.call("GET", "/pipeline")
        self.assertEqual(status, 200)
        self.assertTrue(pipeline["ordered"])
        self.assertEqual(pipeline["last_stage"], "audit")

        status, catalog = self.call("GET", "/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(len(catalog["routes"]), len(route_table()))

        status, ops = self.call("GET", "/ops")
        self.assertEqual(status, 200)
        self.assertTrue(ops["flow_present"])

        status, telemetry = self.call("GET", "/telemetry")
        self.assertEqual(status, 200)
        self.assertEqual(telemetry["filter_beds"], 0)

        status, version = self.call("GET", "/version")
        self.assertEqual(status, 200)
        self.assertEqual(version["name"], "waterplant-control")

        status, system = self.call("GET", "/system")
        self.assertEqual(status, 200)
        self.assertIn("python", system)

    def test_history_round_trip(self) -> None:
        status, _ = self.call("POST", "/history", {"kind": "manual", "value": "start"})
        self.assertEqual(status, 200)
        status, listing = self.call("GET", "/history")
        self.assertEqual(status, 200)
        self.assertTrue(listing["has_events"])
        self.assertEqual(listing["count"], 2)
        status, _ = self.call("POST", "/history/clear")
        self.assertEqual(status, 200)
        _, listing = self.call("GET", "/history")
        self.assertEqual(listing["count"], 0)
        status, error = self.call("POST", "/history", {})
        self.assertEqual(status, 400)
        self.assertIn("kind", error["error"])

    def test_intake_and_dosing_routes(self) -> None:
        status, body = self.call("POST", "/intake/flow", {"flow": 1200, "turbidity": 3.0})
        self.assertEqual(status, 200)
        self.assertEqual(body["flow"], 1200.0)
        status, body = self.call("GET", "/intake/flow")
        self.assertEqual(body["flow"], 1200.0)

        status, body = self.call("POST", "/coag/dose", {"flow": 1500})
        self.assertEqual(status, 200)
        self.assertEqual(body["dose"], 1500.0)
        status, body = self.call("POST", "/coag/turbidity", {"samples": [4.0, 6.0]})
        self.assertEqual(body["dose"], 5.0)
        status, body = self.call("GET", "/coag/ratio")
        self.assertEqual(body["ratio"], 1.0)

        status, body = self.call("POST", "/flow/replace", {"serial": "fm-7", "factor": 2.0})
        self.assertEqual(body["meter"]["factor"], 2.0)
        _, body = self.call("GET", "/coag/ratio")
        self.assertEqual(body["ratio"], 2.0)

        status, body = self.call("POST", "/chlor/target", {"demand": 1.0})
        self.assertEqual(body["target"], 1.0)
        status, body = self.call("POST", "/chlor/dose", {})
        self.assertEqual(body["dose"], 1.0)

        status, body = self.call("POST", "/clearwell/level", {"target": 12.0})
        self.assertEqual(body["level"], 12.0)
        self.assertEqual(body["min_level"], 0.0)

    def test_filter_and_backwash_routes(self) -> None:
        for bed_id, zone, load in (("b1", 1, 4.0), ("b2", 2, 9.0), ("b3", 3, 1.0)):
            status, _ = self.call(
                "POST", "/filter/add", {"id": bed_id, "zone": zone, "load": load}
            )
            self.assertEqual(status, 200)

        status, body = self.call("POST", "/filter/close", {"id": "b1"})
        self.assertEqual(body["closed"], "b1")
        status, body = self.call("GET", "/backwash/order")
        self.assertEqual(body["order"], ["b2", "b1", "b3"])
        self.assertEqual(body["on_duty"], "b2")

        status, body = self.call("POST", "/backwash/select", {"zone": 3})
        self.assertEqual(body["bed"], "b3")
        status, body = self.call("POST", "/filter/zone", {"id": "b3"})
        self.assertTrue(body["ok"])

        status, body = self.call("POST", "/filter/renumber", {"id": "b3", "zone": 7})
        self.assertEqual(body["zone"], 7)
        _, body = self.call("POST", "/backwash/select", {"zone": 7})
        self.assertEqual(body["bed"], "b3")

        status, _ = self.call("POST", "/backwash/enqueue", {"id": "b1"})
        self.assertEqual(status, 200)
        _, body = self.call("POST", "/backwash/replay")
        self.assertEqual(body["replayed"], ["b1"])
        status, _ = self.call("POST", "/backwash/recover")
        self.assertEqual(status, 200)

        status, body = self.call("POST", "/filter/load", {"id": "b1", "load": 20.0})
        self.assertEqual(body["load"], 20.0)
        status, _ = self.call("POST", "/filter/reset")
        self.assertEqual(status, 200)
        status, _ = self.call("POST", "/filter/open", {"id": "b1"})
        self.assertEqual(status, 200)
        status, body = self.call("POST", "/filter/remove", {"id": "b3"})
        self.assertEqual(body["removed"], "b3")

    def test_quota_and_audit_routes(self) -> None:
        status, body = self.call("POST", "/quota/add", {"amount": 40.0})
        self.assertEqual(body["value"], 40.0)
        status, body = self.call("GET", "/quota")
        self.assertEqual(body["value"], 40.0)
        status, body = self.call("POST", "/quota/check", {"chemical": "chlorine", "used": 40.0, "limit": 100.0})
        self.assertEqual(body["remaining"], 60.0)
        self.assertTrue(body["ok"])

        self.call("POST", "/coag/dose", {"flow": 5.0})
        self.call("POST", "/chlor/dose", {})
        status, body = self.call("GET", "/audit")
        self.assertEqual(len(body["entries"]), 2)
        status, body = self.call("GET", "/audit/summary")
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["by_kind"], {"coagulant": 1, "chlorine": 1})
        status, body = self.call("POST", "/audit/filter", {"kind": "chlorine"})
        self.assertEqual(len(body["entries"]), 1)

    def test_cycle_and_simulation(self) -> None:
        self.call("POST", "/filter/add", {"id": "b1", "zone": 1, "load": 3.0})
        status, body = self.call(
            "POST",
            "/cycle",
            {
                "flow": 800.0,
                "samples": [2.0, 4.0],
                "demand": 0.5,
                "level": 10.0,
                "bed_id": "b1",
                "zone": 1,
                "amount": 12.0,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["coag_dose"], 800.0)
        self.assertEqual(body["turb_dose"], 3.0)
        self.assertEqual(body["chlor_dose"], 0.65)
        self.assertEqual(body["rotation"], ["b1"])
        self.assertEqual(body["drains"], 1)
        self.assertEqual(body["audit_count"], 2)
        self.assertTrue(body["ph_stable"])
        self.assertEqual(body["ph_adjustment"], 0.0)

        status, body = self.call("POST", "/simulate", {"ticks": 3})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["ticks"]), 3)
        self.assertEqual(body["last_stage"], "audit")
        status, body = self.call("POST", "/simulate", {})
        self.assertEqual(len(body["ticks"]), 5)

    def test_ph_and_schedule_routes(self) -> None:
        status, body = self.call("GET", "/ph")
        self.assertEqual(status, 200)
        self.assertTrue(body["stable"])
        self.assertEqual(body["band_low"], 6.5)

        status, body = self.call("POST", "/ph/read", {"value": 5.0})
        self.assertEqual(status, 200)
        self.assertFalse(body["stable"])
        self.assertEqual(body["direction"], "raise")
        self.assertAlmostEqual(body["adjustment"], 1.5)

        status, body = self.call("GET", "/ph/verdict")
        self.assertEqual(body["direction"], "raise")
        status, body = self.call("POST", "/ph/read", {"value": 9.5})
        self.assertEqual(body["direction"], "lower")

        status, body = self.call("GET", "/schedule")
        self.assertEqual(status, 200)
        self.assertEqual(body["threshold"], 6.0)
        self.assertEqual(body["due"], [])

        for bed_id, load in (("b1", 2.0), ("b2", 9.0)):
            self.call("POST", "/filter/add", {"id": bed_id, "zone": 1, "load": load})
        status, body = self.call("GET", "/schedule")
        self.assertEqual(body["due"], ["b2"])
        status, body = self.call("POST", "/schedule/plan", {})
        self.assertEqual([entry["bed_id"] for entry in body["entries"]], ["b2", "b1"])
        self.assertEqual(body["entries"][0]["priority"], 1)
        self.assertEqual(body["entries"][0]["drain_seconds"], 75)
        status, body = self.call("POST", "/schedule/threshold", {"threshold": 2.5})
        self.assertEqual(body["threshold"], 2.5)
        _, body = self.call("GET", "/schedule")
        self.assertEqual(body["due"], ["b2"])

    def test_trend_and_audit_totals_routes(self) -> None:
        for flow in (100.0, 200.0, 300.0):
            self.call("POST", "/intake/flow", {"flow": flow})
        status, body = self.call("GET", "/intake/trend")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["samples"], 3)
        self.assertGreater(body["maximum"], body["minimum"])
        status, body = self.call("POST", "/intake/trend/window", {"window": 2})
        self.assertEqual(body["window"], 2)
        _, body = self.call("GET", "/intake/trend")
        self.assertEqual(body["samples"], 2)
        status, _ = self.call("POST", "/intake/trend/reset")
        self.assertEqual(status, 200)
        _, body = self.call("GET", "/intake/trend")
        self.assertEqual(body["samples"], 0)

        self.call("POST", "/coag/dose", {"flow": 5.0})
        self.call("POST", "/coag/dose", {"flow": 7.0})
        self.call("POST", "/chlor/dose", {})
        status, body = self.call("GET", "/audit/totals")
        self.assertEqual(status, 200)
        self.assertEqual(body["totals"]["coagulant"], 12.0)
        self.assertEqual(body["totals"]["chlorine"], 0.5)

    def test_inventory_and_metrics_routes(self) -> None:
        status, body = self.call("POST", "/inventory/receive", {"chemical": "chlorine", "lot_id": "L1", "amount": 40.0})
        self.assertEqual(status, 200)
        self.assertEqual(body["lot"]["remaining"], 40.0)
        self.call("POST", "/inventory/receive", {"chemical": "chlorine", "lot_id": "L2", "amount": 30.0})

        status, body = self.call("GET", "/inventory")
        self.assertEqual(status, 200)
        self.assertEqual(body["balance"], 70.0)
        self.assertEqual(body["needs_reorder"], [])

        status, body = self.call("POST", "/inventory/consume", {"chemical": "chlorine", "amount": 50.0})
        self.assertEqual(body["balance"], 20.0)
        _, body = self.call("GET", "/inventory")
        self.assertEqual(body["needs_reorder"], ["chlorine"])
        self.assertEqual(body["lots"][0]["consumed"], 40.0)
        self.assertEqual(body["lots"][1]["consumed"], 10.0)

        status, body = self.call("POST", "/inventory/reorder-level", {"level": 10.0})
        self.assertEqual(body["reorder_level"], 10.0)
        _, checks = self.call("GET", "/health/checks")
        inventory_check = [c for c in checks["checks"] if c["name"] == "inventory"][0]
        self.assertEqual(inventory_check["status"], "ok")

        status, body = self.call("POST", "/inventory/consume", {"chemical": "chlorine", "amount": 100.0})
        self.assertEqual(status, 400)
        self.assertIn("insufficient", body["error"])

        status, body = self.call("GET", "/metrics")
        self.assertGreater(body["requests"], 0)
        self.assertIn("GET /inventory", body["by_route"])
        status, body = self.call("POST", "/metrics/reset", {})
        self.assertTrue(body["reset"])
        _, body = self.call("GET", "/metrics")
        self.assertEqual(body["requests"], 1)

    def test_export_routes(self) -> None:
        self.call("POST", "/coag/dose", {"flow": 5.0})
        status, body = self.call("GET", "/export/audit")
        self.assertEqual(status, 200)
        self.assertIn("time,kind,detail,id", body)
        self.assertIn("coagulant", body)

        status, body = self.call("GET", "/export/telemetry")
        self.assertEqual(status, 200)
        self.assertIn("metric,value", body)
        self.assertIn("filter_beds", body)

    def test_reset_store_and_errors(self) -> None:
        status, body = self.call("POST", "/ops/reset-store", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["reset"])
        self.assertEqual(body["keys"], 0)

        status, body = self.call("GET", "/missing")
        self.assertEqual(status, 404)
        self.assertIn("no route", body["error"])

        status, body = self.call("DELETE", "/health")
        self.assertEqual(status, 405)
        self.assertIn("not allowed", body["error"])

        status, body = self.call("POST", "/coag/dose", {"flow": -5})
        self.assertEqual(status, 400)
        self.assertIn("non-negative", body["error"])

        status, body = self.call("POST", "/coag/dose", {"flow": "abc"})
        self.assertEqual(status, 400)
        self.assertIn("must be a number", body["error"])

        status, body = self.call("POST", "/filter/close", {"id": "ghost"})
        self.assertEqual(status, 400)
        self.assertIn("not found", body["error"])


class ResidualTargetConsoleCase(ConsoleCase):
    def _payload(self) -> dict:
        return {
            "dose_zone": "outlet",
            "zones": [
                {
                    "zone": "outlet",
                    "windows": [
                        {"start": "06:00", "end": "22:00", "target": 0.55, "label": "day"},
                        {"start": "22:00", "end": "06:00", "target": 0.35, "label": "night"},
                    ],
                },
                {
                    "zone": "network-end",
                    "windows": [
                        {"start": "06:00", "end": "22:00", "target": 0.4, "label": "day"},
                        {"start": "22:00", "end": "06:00", "target": 0.25, "label": "night"},
                    ],
                },
            ],
        }

    def test_status_defaults_to_legacy(self) -> None:
        status, body = self.call("GET", "/chlor/targets")
        self.assertEqual(status, 200)
        self.assertIsNone(body["schedule"])
        self.assertEqual(body["active"]["status"], "legacy")
        self.assertIn("no schedule", body["active"]["reason"])

    def test_install_then_dose_uses_segment_target(self) -> None:
        status, body = self.call("POST", "/chlor/targets", self._payload())
        self.assertEqual(status, 200)
        self.assertTrue(body["installed"])
        self.assertEqual(body["active"]["status"], "active")
        self.assertEqual(body["active"]["zone"], "outlet")
        self.assertIn("network-end", body["zones"])

        status, body = self.call("GET", "/chlor/targets")
        self.assertEqual(status, 200)
        self.assertEqual(body["schedule"]["switch_mode"], "cycle_boundary")
        self.assertTrue(body["active"]["effective_at"])

    def test_invalid_payload_is_rejected_and_explained(self) -> None:
        bad = self._payload()
        bad["zones"][0]["windows"][0]["end"] = "23:00"  # creates an overlap/gap
        status, body = self.call("POST", "/chlor/targets", bad)
        self.assertEqual(status, 400)
        self.assertIn("windows", body["error"])

    def test_demand_target_is_blocked_while_schedule_governs(self) -> None:
        self.call("POST", "/chlor/targets", self._payload())
        status, body = self.call("POST", "/chlor/target", {"demand": 1.0})
        self.assertEqual(status, 409)
        self.assertIn("segmented schedule", body["error"])

    def test_recommended_schedule_installs(self) -> None:
        status, body = self.call("POST", "/chlor/targets/recommended", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["schedule"]["dose_zone"], "outlet")
        self.assertEqual(body["active"]["status"], "active")

    def test_audit_trail_carries_target_change_and_effective_time(self) -> None:
        self.call("POST", "/chlor/targets", self._payload())
        status, body = self.call("POST", "/audit/filter", {"kind": "residual-target"})
        self.assertEqual(status, 200)
        events = [json.loads(entry["detail"])["event"] for entry in body["entries"]]
        self.assertIn("installed", events)
        self.assertIn("activated", events)
        activated = [
            json.loads(entry["detail"])
            for entry in body["entries"]
            if json.loads(entry["detail"])["event"] == "activated"
        ][0]
        self.assertEqual(activated["zone"], "outlet")
        self.assertTrue(activated["effective_at"])

    def test_stale_schedule_is_visible_in_health_checks(self) -> None:
        self.call("POST", "/chlor/targets", self._payload())
        # Corrupt the configured schedule directly in the persisted document.
        raw = json.loads(self.store.get("chlor:residual-schedule")[0])
        raw["zones"][0]["windows"][0]["target"] = -2.0
        self.store.put("chlor:residual-schedule", json.dumps(raw))

        # A read-only dashboard view flags the corruption before dosing runs.
        status, body = self.call("GET", "/chlor/targets")
        self.assertTrue(body["schedule_error"])

        # The partial failure is detected at the cycle boundary / dose; the
        # last confirmed target is then frozen and reported as stale.
        self.call("POST", "/chlor/dose", {})
        status, body = self.call("GET", "/chlor/targets")
        self.assertEqual(body["active"]["status"], "stale")
        self.assertTrue(body["active"]["target"] > 0)
        self.assertTrue(body["schedule_error"])

        status, body = self.call("GET", "/health/checks")
        residual = [row for row in body["checks"] if row["name"] == "residual"][0]
        self.assertEqual(residual["status"], "warn")
        self.assertIn("holding last target", residual["detail"])

    def test_repeated_install_is_idempotent(self) -> None:
        payload = self._payload()
        self.call("POST", "/chlor/targets", payload)
        self.call("POST", "/chlor/targets", payload)
        status, body = self.call("POST", "/audit/filter", {"kind": "residual-target"})
        installs = [
            entry for entry in body["entries"]
            if json.loads(entry["detail"])["event"] == "installed"
        ]
        self.assertEqual(len(installs), 1)


    def test_cycle_doses_against_segmented_target_not_demand(self) -> None:
        from waterplant.chlor.targets import Moment
        from waterplant.console.server import Server as _Server

        holder = {"minute": 12 * 60}

        def clock() -> Moment:
            return Moment(day=1, minute=holder["minute"], iso="fixed")

        server = _Server(self.store, clock)
        call = lambda method, path, payload=None: self._call(server, method, path, payload)
        call("POST", "/chlor/targets", self._payload())

        # Noon is the day segment (0.55 day window in this payload starts 06:00).
        body = call(
            "POST",
            "/cycle",
            {
                "flow": 800.0,
                "samples": [2.0, 4.0],
                "demand": 0.99,  # must be ignored while a schedule governs
                "level": 10.0,
                "bed_id": "",
                "zone": 1,
                "amount": 1.0,
            },
        )[1]
        self.assertEqual(body["chlor_dose"], 0.55)
        self.assertEqual(body["residual_target"]["status"], "active")
        self.assertEqual(body["residual_target"]["window_start"], "06:00")

        # Night segment at 23:00: same target holds for the whole cycle.
        holder["minute"] = 23 * 60
        body = call(
            "POST",
            "/cycle",
            {"flow": 800.0, "samples": [], "demand": 0.99, "level": 10.0, "zone": 1, "amount": 1.0},
        )[1]
        self.assertEqual(body["chlor_dose"], 0.35)
        self.assertEqual(body["residual_target"]["window_start"], "22:00")

    @staticmethod
    def _call(server, method: str, path: str, payload: dict | None = None):
        response = server.dispatch(method, path, payload)
        parsed = json.loads(response.body.decode("utf-8"))
        return response.status, parsed


class WsgiCase(unittest.TestCase):
    def test_wsgi_entry_point(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            seed_defaults(store)
            server = Server(store)
            captured: dict[str, object] = {}

            def start_response(status, headers):
                captured["status"] = status
                captured["headers"] = headers

            payload = json.dumps({"flow": 5.0}).encode("utf-8")

            class Stream:
                def read(self, size: int) -> bytes:
                    return payload

            environ = {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": "/coag/dose",
                "QUERY_STRING": "",
                "CONTENT_LENGTH": str(len(payload)),
                "wsgi.input": Stream(),
            }
            body = b"".join(server(environ, start_response))
            self.assertEqual(captured["status"], "200 OK")
            self.assertEqual(json.loads(body)["dose"], 5.0)
            store.close()


if __name__ == "__main__":
    unittest.main()
