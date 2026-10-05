"""Chlorine dosing controller."""

from __future__ import annotations

from typing import Callable

from waterplant.audit import Auditor
from waterplant.clearwell import Well
from waterplant.store.store import Store

from .report import ChlorState
from .targets import Moment, ResidualTargets

CHLORINE = "chlorine"


class Doser:
    """Follows the residual target confirmed by the target manager.

    The manager is the single read path for a target: it resolves the
    time/zone segment for the current cycle boundary, falls back to the clear
    well target when no schedule is configured, and freezes on the last good
    target when the configuration cannot be read. The dose always equals the
    value the manager confirmed, so the target and the dosing口径 cannot drift.
    """

    def __init__(
        self, store: Store, clock: Callable[[], Moment] = Moment.now
    ) -> None:
        self._well = Well(store)
        self._auditor = Auditor(store)
        self._targets = ResidualTargets(
            store, self._auditor, self._well.residual_target, clock
        )

    @property
    def targets(self) -> ResidualTargets:
        return self._targets

    def current_target(self) -> float:
        """Residual target confirmed for the current time, read on every call."""

        return self._targets.confirmed().target

    def dose_for_residual(self) -> float:
        return self.current_target()

    def apply_residual(self) -> float:
        """Confirm the new target first, then record the dose it implies."""

        activation = self._targets.refresh()
        dose = activation.target
        self._auditor.record(CHLORINE, f"{dose:.4f}")
        return dose

    def target_for_demand(self, demand: float) -> float:
        return 0.3 + demand * 0.7

    def state(self) -> ChlorState:
        return ChlorState(target=self.current_target(), dose=self.dose_for_residual())

    def describe(self) -> str:
        state = self.state()
        return f"chlorine target={state.target:.4f} dose={state.dose:.4f}"
