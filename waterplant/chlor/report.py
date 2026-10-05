"""Chlorine projections and validation rules."""

from __future__ import annotations

from dataclasses import dataclass

from .target import ANALYTE, BASIS, UNIT, ZONE_PLANT


@dataclass(frozen=True)
class ChlorState:
    """Residual target together with the dose it implies."""

    target: float
    dose: float
    zone: str = ZONE_PLANT
    analyte: str = ANALYTE
    unit: str = UNIT
    basis: str = BASIS
    band_id: str = ""
    fingerprint: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "dose": self.dose,
            "zone": self.zone,
            "analyte": self.analyte,
            "unit": self.unit,
            "basis": self.basis,
            "band_id": self.band_id,
            "fingerprint": self.fingerprint,
        }


def validate_demand(demand: float) -> None:
    """Reject residual demands outside the supported range."""

    if demand < 0:
        raise ValueError("demand must be non-negative")
    if demand > 1000:
        raise ValueError("demand exceeds the supported range")
