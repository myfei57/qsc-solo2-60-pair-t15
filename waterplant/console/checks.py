"""Component health checks."""

from __future__ import annotations

from .runtime import Runtime


def run_checks(rt: Runtime) -> list[dict[str, str]]:
    """Return one check row per mutable component."""

    checks = [{"name": "store", "status": "ok", "detail": f"keys={rt.store.count()}"}]
    if rt.calibration.current() <= 0:
        checks.append({"name": "calibration", "status": "fail", "detail": "factor must be positive"})
    else:
        checks.append({"name": "calibration", "status": "ok", "detail": "factor set"})
    status = rt.residual_targets.status()
    active = status["active"]
    upcoming = status["upcoming"]
    if active["status"] == "stale" or status["schedule_error"]:
        reason = active["reason"] or status["schedule_error"]
        checks.append(
            {
                "name": "residual",
                "status": "warn",
                "detail": f"holding last target {active['target']:.4f}: {reason}",
            }
        )
    elif active["target"] <= 0:
        checks.append({"name": "residual", "status": "fail", "detail": "target must be positive"})
    else:
        pending = " switch pending at cycle boundary" if status["switch_pending"] else ""
        detail = (
            f"target {active['target']:.4f} at {active['zone']} "
            f"({active['status']}){pending}"
        )
        checks.append({"name": "residual", "status": "ok", "detail": detail})
    if rt.accumulator.value() < 0:
        checks.append({"name": "quota", "status": "fail", "detail": "accumulator is negative"})
    else:
        checks.append({"name": "quota", "status": "ok", "detail": "accumulator set"})
    if rt.flow_repository.load_flow()[1]:
        checks.append({"name": "intake", "status": "ok", "detail": "flow reading present"})
    else:
        checks.append({"name": "intake", "status": "warn", "detail": "no flow reading yet"})
    if rt.stabilizer.is_stable():
        checks.append({"name": "ph", "status": "ok", "detail": "ph in the stable band"})
    else:
        checks.append({"name": "ph", "status": "warn", "detail": "ph outside the stable band"})
    alerts = rt.inventory.needs_reorder()
    if alerts:
        checks.append(
            {"name": "inventory", "status": "warn", "detail": "reorder " + ", ".join(alerts)}
        )
    else:
        checks.append({"name": "inventory", "status": "ok", "detail": "stock above reorder level"})
    return checks
