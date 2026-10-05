"""One full treatment control cycle."""

from __future__ import annotations

from waterplant.chlor import MODE_IMMEDIATE, ZONE_PLANT
from waterplant.clearwell import validate_level
from waterplant.filter import validate_zone
from waterplant.flow import validate_factor
from waterplant.intake import Sensor, mix, validate_flow
from waterplant.quota import validate_amount

from .http import Request, Response, json_response
from .runtime import Runtime


def begin_target_cycle(rt: Runtime, request: Request):
    """Confirm exactly one residual target for this cycle, before dosing.

    ``minute`` is optional for deterministic control: when absent the cycle
    leaves the confirmed target untouched (a running cycle is never changed
    underneath). When present:

    * ``boundary`` mode (default): a band crossing is promoted at the cycle
      boundary, so the whole cycle doses against one confirmed target;
    * ``immediate`` mode: the target for the given instant is promoted before
      the first dose, even mid-cycle.
    """

    if "minute" not in request.payload:
        snapshot = rt.chlor_targets.active()
        return snapshot, {"changed": False, "reason": "cycle_runs_on_confirmed_target", "stale": False}
    minute = request.int_field("minute")
    mode = rt.chlor_targets.config().mode
    reason = "boundary_switch" if mode != MODE_IMMEDIATE else "immediate_switch"
    result = rt.chlor_targets.tick(minute, reason=reason)
    return result.current, result.as_dict()


def end_target_cycle(rt: Runtime, request: Request):
    """In boundary mode evaluate the next band at end of cycle (no dose follows).

    The promotion lands in the confirmed slot; the following cycle confirms it
    before dosing. In immediate mode there is nothing to stage at the end.
    """

    mode = rt.chlor_targets.config().mode
    if mode == MODE_IMMEDIATE or "minute" not in request.payload:
        return None
    end_minute = request.int_field("minute_end", request.int_field("minute"))
    result = rt.chlor_targets.tick(end_minute, reason="boundary_switch")
    return result.as_dict()


def run_cycle(rt: Runtime, request: Request) -> Response:
    """Walk the whole line once, in production order."""

    flow = request.float_field("flow")
    samples = request.float_list("samples")
    demand = request.float_field("demand")
    level = request.float_field("level")
    bed_id = request.str_field("bed_id")
    zone = request.int_field("zone")
    amount = request.float_field("amount")

    validate_flow(flow)
    validate_factor(rt.calibration.current())
    validate_zone(zone)
    validate_amount(amount)
    validate_level(level)

    target_snapshot, target_handover = begin_target_cycle(rt, request)

    rt.flow_repository.record(Sensor(flow=flow, turbidity=mix(samples)))
    rt.trend.record(flow)
    coag_dose = rt.coag_doser.update_flow_and_dose(flow)
    turb_dose = rt.sampler.judge(samples)
    ph_verdict = rt.stabilizer.stabilize()
    chlor_dose = rt.chlor_doser.apply_residual() if ph_verdict.stable else 0.0
    min_level = rt.well.adjust_level(level, rt.inlet, rt.outlet)
    rotation = rt.backwash.order_rotation()
    duty = rt.bank.on_duty()
    if bed_id:
        rt.backwash.start(bed_id)
    quota_value = rt.accumulator.add(amount)

    end_handover = end_target_cycle(rt, request)

    return json_response(
        {
            "coag_dose": coag_dose,
            "turb_dose": turb_dose,
            "chlor_dose": chlor_dose,
            "min_level": min_level,
            "level": rt.well.level(),
            "quota": quota_value,
            "rotation": rotation,
            "on_duty": duty or "",
            "backwash": bed_id,
            "drains": rt.backwash.drain_count(),
            "audit_count": rt.auditor.count(),
            "ph_stable": ph_verdict.stable,
            "ph_adjustment": ph_verdict.adjustment,
            "chlor_target": {
                "value": target_snapshot.values[ZONE_PLANT],
                "band_id": target_snapshot.band_id,
                "fingerprint": target_snapshot.fingerprint,
                "sources": target_snapshot.sources,
                "handover": target_handover,
                "end_handover": end_handover,
            },
        }
    )
