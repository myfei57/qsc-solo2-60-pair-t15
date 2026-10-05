"""Chlorine dosing controller."""

from __future__ import annotations

import json

from waterplant.audit import Auditor
from waterplant.store.store import Store

from .report import ChlorState
from .target import ANALYTE, BASIS, UNIT, ZONE_PLANT, ActiveMatrix, TargetHolder

CHLORINE = "chlorine"


class Doser:
    """Follows the confirmed time/zone residual target matrix.

    The target snapshot and the implied dose are read from one and the same
    :class:`ActiveMatrix`, so the dose can never be expressed against a
    different analyte, unit, band or version than the target.
    """

    def __init__(self, store: Store, targets: TargetHolder | None = None) -> None:
        self._store = store
        self._targets = targets or TargetHolder(store)
        self._auditor = Auditor(store)

    def targets(self) -> TargetHolder:
        return self._targets

    def active_snapshot(self, zone_id: str = ZONE_PLANT) -> ActiveMatrix:
        """The one confirmed matrix this doser doses against."""

        return self._targets.active(zone_id)

    def current_target(self, zone_id: str = ZONE_PLANT) -> float:
        """Confirmed target for a zone, read fresh on every call."""

        return self._targets.current_target(zone_id)

    def dose_for_residual(self, zone_id: str = ZONE_PLANT) -> float:
        """Dose implied by the confirmed target, from the same snapshot."""

        return self.active_snapshot(zone_id).values[zone_id]

    def apply_residual(self, zone_id: str = ZONE_PLANT) -> float:
        """Record a chlorine dose and return the amount applied.

        Target and dose share a fingerprint; the numeric detail keeps the
        existing dose audit totals working.
        """

        snapshot = self.active_snapshot(zone_id)
        dose = snapshot.values[zone_id]
        self._auditor.record(CHLORINE, f"{dose:.4f}")
        self._auditor.record(
            "chlorine_dose_basis",
            json.dumps(
                {
                    "dose": dose,
                    "target": dose,
                    "zone": zone_id,
                    "analyte": ANALYTE,
                    "unit": UNIT,
                    "basis": BASIS,
                    "band_id": snapshot.band_id,
                    "fingerprint": snapshot.fingerprint,
                    "version": snapshot.version,
                    "sources": snapshot.sources,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        return dose

    def target_for_demand(self, demand: float) -> float:
        return 0.3 + demand * 0.7

    def state(self) -> ChlorState:
        snapshot = self.active_snapshot()
        dose = snapshot.values[ZONE_PLANT]
        return ChlorState(
            target=dose,
            dose=dose,
            zone=ZONE_PLANT,
            analyte=ANALYTE,
            unit=UNIT,
            basis=BASIS,
            band_id=snapshot.band_id,
            fingerprint=snapshot.fingerprint,
        )

    def describe(self) -> str:
        state = self.state()
        return (
            f"chlorine target={state.target:.4f} dose={state.dose:.4f} "
            f"band={state.band_id} zone={state.zone} unit={state.unit}"
        )
