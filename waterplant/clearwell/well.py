"""Clear well level and residual target."""

from __future__ import annotations

from waterplant.chlor import ZONE_PLANT, TargetHolder
from waterplant.intake import InletController
from waterplant.store.epoch import load_float, save_float
from waterplant.store.store import Store

from .report import WellState

LEVEL_KEY = "clearwell:level"
RESIDUAL_KEY = "clearwell:residual-target"
DEFAULT_RESIDUAL_TARGET = 0.5


class Well:
    """Holds the stored water level and the chlorine residual target."""

    def __init__(self, store: Store, targets: TargetHolder | None = None) -> None:
        self._store = store
        self._targets = targets or TargetHolder(store)

    def level(self) -> float:
        value, _ = load_float(self._store, LEVEL_KEY)
        return value

    def set_level(self, value: float) -> None:
        save_float(self._store, LEVEL_KEY, value)

    def residual_target(self) -> float:
        """Confirmed plant outlet residual target (segmented schedule)."""

        return self._targets.current_target(ZONE_PLANT)

    def set_residual_target(self, value: float) -> None:
        """Force the plant outlet target as a manual override.

        The override takes effect immediately and outranks the time schedule
        until it is released; it is confirmed through the same handover path.
        """

        if value <= 0:
            raise ValueError("residual target must be positive")
        self._targets.bootstrap()
        self._targets.set_override(
            {ZONE_PLANT: value}, operator="clearwell", reason="set_residual_target"
        )
        save_float(self._store, RESIDUAL_KEY, value)

    def update_residual_demand(self, demand: float) -> float:
        """Translate an operator demand into a residual target."""

        target = 0.3 + demand * 0.7
        self.set_residual_target(target)
        return target

    def adjust_level(
        self, target: float, inlet: InletController, outlet: InletController
    ) -> float:
        """Move the inlet first, then the outlet, and report the low mark."""

        start = self.level()
        delta = target - start
        if delta >= 0:
            intermediate = inlet.raise_level(start, delta)
            final = outlet.lower_level(intermediate, 0.0)
        else:
            amount = -delta
            intermediate = outlet.lower_level(start, amount)
            final = inlet.raise_level(intermediate, 0.0)
        self.set_level(final)
        return min(start, intermediate, final)

    def state(self) -> WellState:
        return WellState(level=self.level(), residual_target=self.residual_target())

    def describe(self) -> str:
        state = self.state()
        return (
            f"clearwell level={state.level:.4f} residual_target={state.residual_target:.4f}"
        )
