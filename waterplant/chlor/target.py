"""Time and zone segmented residual chlorine targets.

The target for residual chlorine is no longer one plant wide number. It is a
matrix keyed by:

* a half open time band ``[start, end)`` over the 24h day (morning peak, day,
  evening peak, night ...) and
* a network zone (plant outlet, network entry, far point ...).

Exactly one value per zone is effective at any instant: a valid schedule is a
partition of the day with no gaps and no overlaps, and precedence between
sources is fixed (manual override wins over the schedule for the zones it
covers). Target switches are staged in a pending key and only promoted to the
active key once the promoted value has been read back and confirmed, so the
doser never follows an unconfirmed target.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from waterplant.audit import Auditor
from waterplant.store.store import Store

# --- measurement basis: target and dose always speak the same unit ---------
ANALYTE = "free_residual_chlorine"
UNIT = "mg/L"
BASIS = "network_residual"

# --- switch timing policy ---------------------------------------------------
# boundary: a band crossing takes effect once the running control cycle has
#           finished (plant operating habit: never change a set point midway
#           through a cycle). The next cycle doses against the new target.
# immediate: at the exact boundary instant the target is replaced before the
#            next dose, even mid-cycle.
MODE_BOUNDARY = "boundary"
MODE_IMMEDIATE = "immediate"
SWITCH_MODES = (MODE_BOUNDARY, MODE_IMMEDIATE)

# --- overlap policy, fixed by design ----------------------------------------
# Inside one schedule, gaps and overlaps are rejected when the configuration is
# loaded. Across sources there is never a tie to break: a manual override wins
# over the schedule, zone by zone, so at most one source owns each zone.
OVERLAP_POLICY = "reject_in_schedule;manual_override_wins_over_schedule"

CONFIG_KEY = "chlor:target-config"
ACTIVE_KEY = "chlor:target-active"
PENDING_KEY = "chlor:target-pending"
STATUS_KEY = "chlor:target-status"
OVERRIDE_KEY = "chlor:target-override"
REQUEST_KEY = "chlor:target-request"

STATE_ACTIVE = "ACTIVE"
STATE_STALE = "STALE"

SOURCE_SCHEDULE = "schedule"
SOURCE_OVERRIDE = "override"

ZONE_PLANT = "plant"
ZONE_ENTRY = "entry"
ZONE_FARPOINT = "farpoint"

MINUTES_PER_DAY = 1440
MAX_TARGET = 10.0

ISO = "%Y-%m-%dT%H:%M:%SZ"


def utc_now() -> str:
    return time.strftime(ISO, time.gmtime())


def current_minute(epoch: float | None = None) -> int:
    """Minute of the local operating day, 0..1439."""

    moment = time.time() if epoch is None else epoch
    struct = time.localtime(moment)
    return struct.tm_hour * 60 + struct.tm_min


def hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


class ConfigError(ValueError):
    """A target configuration cannot be parsed or validated."""


class HandoverError(RuntimeError):
    """A staged target could not be confirmed as active."""


@dataclass(frozen=True)
class Zone:
    """One dosing/measurement location in the network."""

    id: str
    name: str
    order: int

    def as_dict(self) -> dict[str, object]:
        return {"id": self.id, "name": self.name, "order": self.order}


@dataclass(frozen=True)
class Band:
    """A half open ``[start, end)`` time band in minutes."""

    id: str
    name: str
    start: int
    end: int

    def contains(self, minute: int) -> bool:
        return self.start <= minute < self.end

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "start": self.start,
            "end": self.end,
            "start_label": hhmm(self.start),
            "end_label": hhmm(self.end),
        }


@dataclass(frozen=True)
class TargetConfig:
    """A validated schedule: bands partition the day, zones partition the network."""

    version: int
    mode: str
    bands: tuple[Band, ...]
    zones: tuple[Zone, ...]
    targets: Mapping[str, Mapping[str, float]]
    fingerprint: str

    def zone_ids(self) -> list[str]:
        return [zone.id for zone in sorted(self.zones, key=lambda zone: zone.order)]

    def band_at(self, minute: int) -> Band:
        moment = minute % MINUTES_PER_DAY
        for band in self.bands:
            if band.contains(moment):
                return band
        return self.bands[0]

    def value(self, band_id: str, zone_id: str) -> float:
        return float(self.targets[band_id][zone_id])

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "mode": self.mode,
            "overlap_policy": OVERLAP_POLICY,
            "fingerprint": self.fingerprint,
            "zones": [zone.as_dict() for zone in sorted(self.zones, key=lambda z: z.order)],
            "bands": [
                {
                    **band.as_dict(),
                    "targets": {zone: self.value(band.id, zone) for zone in self.zone_ids()},
                }
                for band in self.bands
            ],
        }


def _fingerprint(mode: str, bands: list[Band], zones: list[Zone], matrix: dict[str, dict[str, float]]) -> str:
    canonical = json.dumps(
        {
            "mode": mode,
            "bands": sorted((b.id, b.start, b.end) for b in bands),
            "zones": sorted((z.id, z.order) for z in zones),
            "targets": {band_id: dict(values) for band_id, values in matrix.items()},
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def build_config(
    mode: str,
    band_rows: list[dict[str, object]],
    zone_rows: list[dict[str, object]],
    matrix: dict[str, dict[str, float]],
    version: int = 1,
) -> TargetConfig:
    """Validate raw schedule rows and build an immutable config.

    Bands must be half open segments that cover 0..1440 exactly: first start at
    0, last end at 1440 and each end equal to the next start. Therefore the
    validated schedule can never have a gap or two bands in force at once.
    """

    if mode not in SWITCH_MODES:
        raise ConfigError(f"mode must be one of {','.join(SWITCH_MODES)}")
    if not isinstance(version, int) or version < 1:
        raise ConfigError("version must be a positive integer")

    bands: list[Band] = []
    seen_band: set[str] = set()
    for row in band_rows:
        band_id = str(row.get("id", "")).strip()
        name = str(row.get("name", band_id))
        if not band_id:
            raise ConfigError("every band needs an id")
        if band_id in seen_band:
            raise ConfigError(f"duplicate band id {band_id}")
        seen_band.add(band_id)
        try:
            start = int(row["start"])
            end = int(row["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"band {band_id} start/end must be integers") from exc
        if not 0 <= start < end <= MINUTES_PER_DAY:
            raise ConfigError(f"band {band_id} needs 0 <= start < end <= 1440")
        bands.append(Band(id=band_id, name=name, start=start, end=end))

    if not bands:
        raise ConfigError("at least one time band is required")
    bands.sort(key=lambda band: band.start)
    if bands[0].start != 0:
        raise ConfigError("the first band must start at 00:00")
    previous_end = 0
    for band in bands:
        if band.start != previous_end:
            raise ConfigError(
                f"bands are not contiguous around {hhmm(previous_end)}: gap or overlap"
            )
        previous_end = band.end
    if previous_end != MINUTES_PER_DAY:
        raise ConfigError("the last band must end at 24:00")

    zones: list[Zone] = []
    seen_zone: set[str] = set()
    for row in zone_rows:
        zone_id = str(row.get("id", "")).strip()
        if not zone_id:
            raise ConfigError("every zone needs an id")
        if zone_id in seen_zone:
            raise ConfigError(f"duplicate zone id {zone_id}")
        seen_zone.add(zone_id)
        order = int(row.get("order", 0))
        zones.append(Zone(id=zone_id, name=str(row.get("name", zone_id)), order=order))
    if not zones:
        raise ConfigError("at least one network zone is required")

    clean_matrix: dict[str, dict[str, float]] = {}
    zone_ids = {zone.id for zone in zones}
    for band in bands:
        row = matrix.get(band.id)
        if not isinstance(row, dict):
            raise ConfigError(f"band {band.id} is missing target values")
        if set(row) != zone_ids:
            raise ConfigError(
                f"band {band.id} must give exactly one target for every zone "
                f"({sorted(zone_ids)})"
            )
        values: dict[str, float] = {}
        for zone_id, raw in row.items():
            try:
                value = float(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"target {band.id}/{zone_id} must be numeric") from exc
            if not value > 0.0 or value > MAX_TARGET:
                raise ConfigError(
                    f"target {band.id}/{zone_id} must be in (0, {MAX_TARGET}] {UNIT}"
                )
            values[zone_id] = value
        clean_matrix[band.id] = values

    return TargetConfig(
        version=version,
        mode=mode,
        bands=tuple(bands),
        zones=tuple(zones),
        targets=clean_matrix,
        fingerprint=_fingerprint(mode, bands, zones, clean_matrix),
    )


def config_from_payload(payload: object) -> TargetConfig:
    """Parse a JSON config document as sent by the console.

    Accepts both the canonical flat shape (``targets`` matrix keyed by band id)
    and the documented :meth:`TargetConfig.as_dict` shape (targets nested on
    each band row).
    """

    if not isinstance(payload, dict):
        raise ConfigError("config must be a JSON object")
    band_rows = payload.get("bands") if isinstance(payload.get("bands"), list) else []
    matrix = payload.get("targets")
    if not isinstance(matrix, dict):
        matrix = {
            str(row.get("id", "")): row.get("targets", {})
            for row in band_rows
            if isinstance(row, dict)
        }
    return build_config(
        mode=str(payload.get("mode", MODE_BOUNDARY)),
        band_rows=band_rows,
        zone_rows=payload.get("zones") if isinstance(payload.get("zones"), list) else [],
        matrix=matrix,
        version=int(payload.get("version", 1)),
    )


@dataclass(frozen=True)
class ActiveMatrix:
    """The one matrix of values the doser is allowed to follow."""

    band_id: str
    values: dict[str, float]
    sources: dict[str, str]
    fingerprint: str
    version: int
    minute: int
    activated_at: str

    def identity(self) -> str:
        return json.dumps(
            {
                "band": self.band_id,
                "fingerprint": self.fingerprint,
                "values": self.values,
                "sources": self.sources,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "analyte": ANALYTE,
            "unit": UNIT,
            "basis": BASIS,
            "band_id": self.band_id,
            "values": dict(self.values),
            "sources": dict(self.sources),
            "fingerprint": self.fingerprint,
            "version": self.version,
            "resolved_minute": self.minute,
            "activated_at": self.activated_at,
        }


@dataclass(frozen=True)
class HandoverResult:
    """Outcome of one switch evaluation, returned to the console."""

    changed: bool
    reason: str
    stale: bool
    previous: ActiveMatrix | None
    current: ActiveMatrix

    def as_dict(self) -> dict[str, object]:
        return {
            "changed": self.changed,
            "reason": self.reason,
            "stale": self.stale,
            "previous": None if self.previous is None else self.previous.as_dict(),
            "current": self.current.as_dict(),
        }


def default_config() -> TargetConfig:
    """Built in operating schedule used on cold boot and as documentation.

    Night residence is long so the plant outlet target is held down; the morning
    and evening demand peaks raise the outlet target to protect the far point.
    """

    return build_config(
        mode=MODE_BOUNDARY,
        band_rows=[
            {"id": "night_a", "name": "夜间（凌晨）", "start": 0, "end": 360},
            {"id": "morning", "name": "早高峰", "start": 360, "end": 540},
            {"id": "day", "name": "白天平峰", "start": 540, "end": 1020},
            {"id": "evening", "name": "晚高峰", "start": 1020, "end": 1320},
            {"id": "night_b", "name": "夜间", "start": 1320, "end": 1440},
        ],
        zone_rows=[
            {"id": ZONE_PLANT, "name": "出厂点（清水池出口）", "order": 0},
            {"id": ZONE_ENTRY, "name": "管网入口", "order": 1},
            {"id": ZONE_FARPOINT, "name": "管网末梢", "order": 2},
        ],
        matrix={
            "night_a": {ZONE_PLANT: 0.40, ZONE_ENTRY: 0.35, ZONE_FARPOINT: 0.30},
            "morning": {ZONE_PLANT: 0.60, ZONE_ENTRY: 0.45, ZONE_FARPOINT: 0.30},
            "day": {ZONE_PLANT: 0.50, ZONE_ENTRY: 0.40, ZONE_FARPOINT: 0.30},
            "evening": {ZONE_PLANT: 0.60, ZONE_ENTRY: 0.45, ZONE_FARPOINT: 0.30},
            "night_b": {ZONE_PLANT: 0.40, ZONE_ENTRY: 0.35, ZONE_FARPOINT: 0.30},
        },
    )


class TargetHolder:
    """Owns the effective residual target matrix and its handover protocol."""

    def __init__(
        self,
        store: Store,
        clock: Callable[[], float] | None = None,
        auditor: Auditor | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or time.time
        self._auditor = auditor or Auditor(store)

    # ------------------------------------------------------------------ config
    def _read_json(self, key: str) -> tuple[dict[str, object] | None, bool]:
        raw, present = self._store.get(key)
        if not present:
            return None, False
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None, False
        if not isinstance(parsed, dict):
            return None, False
        return parsed, True

    def load_config(self) -> TargetConfig | None:
        """Stored config when present and valid; ``None`` when unreadable.

        A missing or corrupt document is a *partial* failure: callers keep the
        last confirmed target instead of guessing.
        """

        data, present = self._read_json(CONFIG_KEY)
        if not present or data is None:
            return None
        try:
            return config_from_payload(data)
        except ConfigError:
            return None

    def config(self) -> TargetConfig:
        return self.load_config() or default_config()

    def config_present(self) -> bool:
        """Whether a config document is stored (readable or not)."""

        _, present = self._store.get(CONFIG_KEY)
        return present

    def replace_config(self, payload: object, operator: str = "operator") -> TargetConfig:
        """Validate then adopt a schedule; adoption never touches the active target.

        The new matrix only becomes effective through the next handover. An
        invalid document is rejected, audited and leaves everything running.
        Re-posting the same document is idempotent.
        """

        try:
            candidate = config_from_payload(payload)
        except ConfigError as exc:
            self._audit("target_config_rejected", {"reason": str(exc), "operator": operator})
            raise
        current = self.load_config()
        if current is not None and current.fingerprint == candidate.fingerprint:
            return current
        self._store.put(CONFIG_KEY, json.dumps(candidate.as_dict(), ensure_ascii=False))
        self._audit(
            "target_config_accepted",
            {
                "fingerprint": candidate.fingerprint,
                "version": candidate.version,
                "mode": candidate.mode,
                "operator": operator,
            },
        )
        return candidate

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, object]:
        data, _ = self._read_json(STATUS_KEY)
        if data is None:
            return {"state": STATE_ACTIVE, "reason": "cold boot", "since": "", "fingerprint": ""}
        return {
            "state": str(data.get("state", STATE_ACTIVE)),
            "reason": str(data.get("reason", "")),
            "since": str(data.get("since", "")),
            "fingerprint": str(data.get("fingerprint", "")),
        }

    def _set_status(self, state: str, reason: str, fingerprint: str) -> None:
        self._store.put(
            STATUS_KEY,
            json.dumps(
                {
                    "state": state,
                    "reason": reason,
                    "since": utc_now(),
                    "fingerprint": fingerprint,
                },
                ensure_ascii=False,
            ),
        )

    # ------------------------------------------------------------------ active
    def _load_active(self) -> ActiveMatrix | None:
        data, present = self._read_json(ACTIVE_KEY)
        if not present or data is None:
            return None
        return self._active_from_dict(data)

    def _active_from_dict(self, data: Mapping[str, object]) -> ActiveMatrix | None:
        try:
            values = {str(k): float(v) for k, v in data["values"].items()}  # type: ignore[union-attr]
            sources = {str(k): str(v) for k, v in data["sources"].items()}  # type: ignore[union-attr]
            return ActiveMatrix(
                band_id=str(data["band_id"]),
                values=values,
                sources=sources,
                fingerprint=str(data.get("fingerprint", "")),
                version=int(data.get("version", 1)),
                minute=int(data.get("resolved_minute", 0)),
                activated_at=str(data.get("activated_at", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def bootstrap(self) -> ActiveMatrix:
        """Make sure a confirmed target exists. Deterministic 00:00 baseline.

        Bootstrapping is not a target change, so it writes no audit entry.
        """

        active = self._load_active()
        if active is not None:
            return active
        config = self.config()
        matrix = self._resolve(config, minute=0, override=None)
        self._store.put(ACTIVE_KEY, json.dumps(matrix.as_dict(), ensure_ascii=False))
        self._set_status(STATE_ACTIVE, "bootstrap baseline at 00:00", config.fingerprint)
        return matrix

    def active(self, zone_id: str = ZONE_PLANT) -> ActiveMatrix:
        """The one confirmed matrix.

        A replaced config is never served here until a handover has promoted
        and read it back, so the doser cannot follow an unconfirmed target.
        """

        matrix = self._load_active()
        if matrix is None:
            matrix = self.bootstrap()
        if zone_id not in matrix.values:
            raise KeyError(f"no confirmed target for zone {zone_id}; run a switch first")
        return matrix

    def current_target(self, zone_id: str = ZONE_PLANT) -> float:
        return self.active(zone_id).values[zone_id]

    # -------------------------------------------------------------- resolution
    def _load_override(self) -> dict[str, object] | None:
        data, present = self._read_json(OVERRIDE_KEY)
        if not present or data is None:
            return None
        try:
            expire = data.get("expire_epoch")
            if expire is not None and float(expire) <= self._clock():
                return None
        except (TypeError, ValueError):
            return None
        return data

    def _resolve(
        self,
        config: TargetConfig,
        minute: int,
        override: Mapping[str, object] | None,
    ) -> ActiveMatrix:
        band = config.band_at(minute)
        values = {zone: config.value(band.id, zone) for zone in config.zone_ids()}
        sources = {zone: SOURCE_SCHEDULE for zone in values}
        if override is not None:
            raw_values = override.get("values")
            if isinstance(raw_values, dict):
                for zone_id, raw in raw_values.items():
                    if zone_id in values:
                        values[zone_id] = float(raw)  # type: ignore[arg-type]
                        sources[zone_id] = SOURCE_OVERRIDE
        return ActiveMatrix(
            band_id=band.id,
            values=values,
            sources=sources,
            fingerprint=config.fingerprint,
            version=config.version,
            minute=minute % MINUTES_PER_DAY,
            activated_at=utc_now(),
        )

    # ---------------------------------------------------------------- handover
    def _commit(self, desired: ActiveMatrix, previous: ActiveMatrix | None) -> ActiveMatrix:
        raw = json.dumps(desired.as_dict(), ensure_ascii=False)
        # Stage first; a crash between the two puts can never expose a half
        # written active value because the doser only reads ACTIVE_KEY.
        self._store.put(PENDING_KEY, raw)
        staged = self._active_from_dict(self._read_json(PENDING_KEY)[0] or {})
        if staged is None or staged.identity() != desired.identity():
            raise HandoverError("staged target could not be confirmed")
        try:
            self._store.put(ACTIVE_KEY, raw)
            live = self._load_active()
            if live is None or live.identity() != desired.identity():
                raise HandoverError("active target does not match confirmed target")
        except HandoverError:
            if previous is None:
                self._store.delete(ACTIVE_KEY)
            else:
                self._store.put(
                    ACTIVE_KEY, json.dumps(previous.as_dict(), ensure_ascii=False)
                )
            raise
        finally:
            self._store.delete(PENDING_KEY)
        self._set_status(STATE_ACTIVE, f"confirmed {desired.band_id}", desired.fingerprint)
        return live

    def evaluate(self, minute: int | None = None) -> ActiveMatrix:
        """Resolve what the target ought to be right now, without promoting it."""

        moment = current_minute(self._clock()) if minute is None else minute
        return self._resolve(self.config(), moment, self._load_override())

    def tick(self, minute: int | None = None, reason: str = "band_change") -> HandoverResult:
        """Run one switch evaluation and promote the target when it changed.

        Repeating the evaluation after the target is already in force is a
        no-op and writes no duplicate audit entry.
        """

        moment = current_minute(self._clock()) if minute is None else minute
        config = self.load_config()
        active = self._load_active()
        if active is None:
            active = self.bootstrap()

        if config is None and self.config_present():
            # Partial failure: hold the last confirmed target, say so, once.
            if self.status()["state"] != STATE_STALE:
                self._set_status(STATE_STALE, "target config unreadable; holding last confirmed target", active.fingerprint)
                self._audit(
                    "target_stale",
                    {"held_band": active.band_id, "values": active.values},
                )
            return HandoverResult(
                changed=False, reason="config_unreadable_held_last", stale=True,
                previous=active, current=active,
            )

        if config is None:
            config = default_config()

        if self.status()["state"] == STATE_STALE:
            self._set_status(STATE_ACTIVE, "config restored", config.fingerprint)
            self._audit("target_recovered", {"fingerprint": config.fingerprint})

        override = self._load_override()
        desired = self._resolve(config, moment, override)
        if desired.identity() == active.identity():
            return HandoverResult(
                changed=False, reason="already_in_force", stale=False,
                previous=active, current=active,
            )
        promoted = self._commit(desired, active)
        self._audit(
            "target_activated",
            {
                "reason": reason,
                "mode": config.mode,
                "at": promoted.activated_at,
                "minute": promoted.minute,
                "from_band": active.band_id,
                "to_band": promoted.band_id,
                "values": promoted.values,
                "sources": promoted.sources,
                "fingerprint": promoted.fingerprint,
            },
        )
        return HandoverResult(
            changed=True, reason=reason, stale=False,
            previous=active, current=promoted,
        )

    # ---------------------------------------------------------------- override
    def set_override(
        self,
        values: Mapping[str, float],
        ttl_minutes: int = 0,
        operator: str = "operator",
        reason: str = "manual override",
        minute: int | None = None,
        request_id: str = "",
    ) -> HandoverResult:
        """Force zone targets immediately; ``ttl_minutes=0`` holds until cleared."""

        config = self.load_config()
        if config is None and self.config_present():
            raise ConfigError("cannot set override while target config is unreadable")
        if config is None:
            config = default_config()
        if not values:
            raise ConfigError("override needs at least one zone value")
        moment = current_minute(self._clock()) if minute is None else minute
        for zone_id, value in values.items():
            if zone_id not in config.zone_ids():
                raise ConfigError(f"unknown zone {zone_id}")
            if not 0.0 < float(value) <= MAX_TARGET:
                raise ConfigError(f"override for {zone_id} must be in (0, {MAX_TARGET}] {UNIT}")
        if ttl_minutes < 0:
            raise ConfigError("ttl_minutes must be non-negative")

        if request_id:
            tracked, present = self._read_json(REQUEST_KEY)
            if present and tracked is not None and tracked.get("request_id") == request_id:
                active = self.active()
                return HandoverResult(
                    changed=False, reason="duplicate_request", stale=False,
                    previous=active, current=active,
                )

        expire_epoch = None if ttl_minutes == 0 else self._clock() + ttl_minutes * 60
        self._store.put(
            OVERRIDE_KEY,
            json.dumps(
                {
                    "values": {zone: float(value) for zone, value in values.items()},
                    "expire_epoch": expire_epoch,
                    "expire_at": "" if expire_epoch is None else time.strftime(ISO, time.gmtime(expire_epoch)),
                    "operator": operator,
                    "reason": reason,
                    "set_at": utc_now(),
                },
                ensure_ascii=False,
            ),
        )
        if request_id:
            self._store.put(REQUEST_KEY, json.dumps({"request_id": request_id}, ensure_ascii=False))
        return self.tick(moment, reason="override")

    def clear_override(self, minute: int | None = None) -> HandoverResult:
        """Release the override and immediately resume the schedule."""

        moment = current_minute(self._clock()) if minute is None else minute
        self._store.delete(OVERRIDE_KEY)
        return self.tick(moment, reason="override_cleared")

    def override(self) -> dict[str, object] | None:
        return self._load_override()

    # ----------------------------------------------------------------- console
    def state(self) -> dict[str, object]:
        config = self.config()
        active = self._load_active() or self.bootstrap()
        moment = current_minute(self._clock())
        return {
            "analyte": ANALYTE,
            "unit": UNIT,
            "basis": BASIS,
            "switch_mode": config.mode,
            "overlap_policy": OVERLAP_POLICY,
            "config": config.as_dict(),
            "active": active.as_dict(),
            "status": self.status(),
            "override": self._load_override(),
            "current_minute": moment,
            "current_band": config.band_at(moment).as_dict(),
        }

    def describe(self) -> str:
        config = self.config()
        active = self._load_active() or self.bootstrap()
        moment = current_minute(self._clock())
        value = active.values.get(ZONE_PLANT, 0.0)
        return (
            f"chlorine target plant={value:.4f} {UNIT} band={active.band_id} "
            f"clock_band={config.band_at(moment).id} mode={config.mode} "
            f"status={self.status()['state']}"
        )

    def _audit(self, kind: str, detail: dict[str, object]) -> None:
        self._auditor.record(kind, json.dumps(detail, ensure_ascii=False, sort_keys=True))
