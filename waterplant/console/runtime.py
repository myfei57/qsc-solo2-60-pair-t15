"""Wiring of every control component behind the console."""

from __future__ import annotations

from typing import Callable

from waterplant.audit import Auditor
from waterplant.backwash import Controller
from waterplant.chlor import Doser as ChlorDoser
from waterplant.chlor.targets import Moment
from waterplant.clearwell import Well
from waterplant.coag import Doser as CoagDoser
from waterplant.filter import Bank
from waterplant.flow import Calibration
from waterplant.intake import FlowRepository, InletController, Trend
from waterplant.inventory import Inventory
from waterplant.ph import Stabilizer
from waterplant.quota import Accumulator
from waterplant.scheduler import Scheduler
from waterplant.store import Store
from waterplant.turb import Sampler


class Runtime:
    """Owns one instance of each control component for a single store."""

    def __init__(
        self,
        store: Store,
        clock: Callable[[], Moment] = Moment.now,
    ) -> None:
        bank = Bank()
        coag_doser = CoagDoser(store)
        self.store = store
        self.flow_repository = FlowRepository(store)
        self.inlet = InletController()
        self.outlet = InletController()
        self.coag_doser = coag_doser
        self.chlor_doser = ChlorDoser(store, clock)
        self.residual_targets = self.chlor_doser.targets
        self.bank = bank
        self.backwash = Controller(bank, store)
        self.sampler = Sampler(coag_doser)
        self.calibration = Calibration(store)
        self.well = Well(store)
        self.accumulator = Accumulator(store)
        self.auditor = Auditor(store)
        self.stabilizer = Stabilizer(store)
        self.scheduler = Scheduler(store)
        self.trend = Trend(store)
        self.inventory = Inventory(store)
