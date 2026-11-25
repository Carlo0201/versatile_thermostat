"""Pure collector of the boiler orchestration inputs (M2-A).

This module ports the ``raw`` collector of the Home Assistant automation
``automation.caldaia_controllo_stabile_delle_richieste`` (baseline snapshot of
24/11/2026). It maps a snapshot of entity states to:

* ``raw``: the exact dictionary built by the Jinja template, whose keys are the
  fields of the M1 ``BoilerSnapshot`` (``ok``, ``master``, ``count``, ``rooms``,
  ``manual``, ``pilot``, ``pilot_ready``, ``consent``);
* per-room diagnostics: eligible rooms with their physical valves and, for each
  configured room that is not eligible, every stable exclusion code observed.

The module is pure: no Home Assistant import, no I/O, no global clock, no
service call. ``now`` is an explicit absolute Unix timestamp and the state
mapping is read only. All entity ids come from the configuration.

Semantics reproduced from the template (see ``M2-A-ESITO.md``):

* ``count`` is a finite non negative integer equal to the number of unique
  ``active_device_ids``. Uniqueness follows the Jinja ``unique`` filter, which
  compares strings case-insensitively.
* a room is eligible only with a valid count, climate ``heat``/``auto``,
  ``is_ready``, ``is_initialized``, ``is_used_by_central_boiler``, window
  closed (sensor and manager ``off``) or explicit bypass, PDC session and split
  both confirmed ``off`` when the room has a PDC, no inhibition (a missing
  ``inhibited`` key means not inhibited), safety and overpowering ``off`` (a
  missing state means ``off``), Avatto demand not explicitly false, numeric
  current and target temperature and a sample age within ``[0, 3600]`` s. A
  missing or unparsable sample time counts as Unix time 0, as with
  ``as_timestamp(value, 0)``.
* a valve counts only when listed in ``active_device_ids``, in ``heat``/``auto``
  and reporting ``hvac_action == 'heating'``. The gap is ``round(target -
  current, 3)`` and ``pilot_ok`` is true unless the room is subject to the pilot
  filter and its Avatto demand is not ``true``.
* boolean comparisons use Python ``==`` like Jinja, so ``1 == true``.

Deliberate hardenings, where the template would raise or leak non JSON values,
are documented in ``M2-A-ESITO.md``: non finite numbers are invalid, malformed
attribute sections are read as empty, and unhashable active ids invalidate the
count. They never change a decision for well formed VTherm attributes.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from typing import Any, Final

SCHEMA_VERSION: Final = 1
# Compatible additions of M2-B (control fields): schema 1.1.
SCHEMA_MINOR: Final = 1

# Configuration section stored in the central configuration entry.
CONF_SECTION: Final = "heating_orchestration"
KEY_MODE: Final = "mode"
KEY_MASTER: Final = "master_entity_id"
KEY_CENTRAL_MODE: Final = "central_mode_entity_id"
KEY_ACTIVE_COUNT: Final = "active_count_entity_id"
KEY_MANUAL: Final = "manual_request_entity_id"
KEY_PILOT_FILTER: Final = "pilot_filter_entity_id"
KEY_PILOT_READY: Final = "pilot_ready_entity_id"
KEY_CONSENTS: Final = "consent_entity_ids"
KEY_LEGACY_CYCLE: Final = "legacy_cycle_entity_id"
KEY_EXTERNAL_OWNERS: Final = "external_owner_entity_ids"
KEY_CONTROL_CONFIRMED: Final = "control_confirmed"
KEY_ROOMS: Final = "rooms"
KEY_ROOM_KEY: Final = "room_key"
KEY_ROOM_CLIMATE: Final = "climate_entity_id"
KEY_ROOM_PILOT_FILTER: Final = "pilot_filter"
KEY_ROOM_PDC: Final = "pdc"
KEY_PDC_SPLIT: Final = "split_entity_id"
KEY_PDC_SESSION: Final = "session_entity_id"

CENTRAL_ENTITY_KEYS: Final = (
    KEY_MASTER,
    KEY_CENTRAL_MODE,
    KEY_ACTIVE_COUNT,
    KEY_MANUAL,
    KEY_PILOT_FILTER,
    KEY_PILOT_READY,
    KEY_LEGACY_CYCLE,
)

MODE_DISABLED: Final = "disabled"
MODE_OBSERVE: Final = "observe"
MODE_CONTROL: Final = "control"
KNOWN_MODES: Final = (MODE_DISABLED, MODE_OBSERVE, MODE_CONTROL)
# Modes accepted by the parser. ``control`` also needs the external owners and an
# explicit confirmation stored by the options flow (``control_confirmed``).
SELECTABLE_MODES: Final = (MODE_DISABLED, MODE_OBSERVE, MODE_CONTROL)
# Modes offered when a new central configuration is created (never ``control``).
CONFIG_FLOW_MODES: Final = (MODE_DISABLED, MODE_OBSERVE)

# Release gate for the boiler ``control`` mode. The first installable release is observe-only:
# while False the options flow does not offer ``control`` and the manager refuses a stored
# ``control`` section (inactive with an explicit error, never control). Set to True only in a
# release whose control path has been validated in Home Assistant.
BOILER_CONTROL_AVAILABLE: Final = False
CONFIG_ERROR_CONTROL_NOT_AVAILABLE: Final = "control_not_available"

# Expected domains of the configured bindings.
MASTER_DOMAINS: Final = ("climate",)
CENTRAL_MODE_DOMAINS: Final = ("select", "input_select")
ACTIVE_COUNT_DOMAINS: Final = ("sensor",)
BINARY_INPUT_DOMAINS: Final = ("binary_sensor", "input_boolean", "switch")
LEGACY_CYCLE_DOMAINS: Final = ("input_text",)
EXTERNAL_OWNER_DOMAINS: Final = ("automation", "input_boolean", "switch")
ROOM_DOMAINS: Final = ("climate",)
SPLIT_DOMAINS: Final = ("climate",)
SESSION_DOMAINS: Final = BINARY_INPUT_DOMAINS

MAX_ROOMS: Final = 32
MAX_CONSENTS: Final = 8
MAX_EXTERNAL_OWNERS: Final = 16
ROOM_KEY_PATTERN: Final = re.compile(r"^[a-z0-9_]{1,64}$")
ENTITY_ID_PATTERN: Final = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_)\.(?!_)[\da-z_]+(?<!_)$")

# Values of the baseline template.
CENTRAL_MODES_ALLOWING_HEAT: Final = ("Auto", "Heat only")
HEATING_HVAC_MODES: Final = ("heat", "auto")
MAX_TEMPERATURE_AGE_S: Final = 3600.0
SUPPORTED_TEMPERATURE_UNIT: Final = "°C"
STATE_ON: Final = "on"
STATE_OFF: Final = "off"
STATE_UNKNOWN: Final = "unknown"
STATE_UNAVAILABLE: Final = "unavailable"
HVAC_ACTION_HEATING: Final = "heating"

# Stable exclusion codes (CONTRATTI.md section 2), in reporting order.
EXCLUSION_ENTITY_UNAVAILABLE: Final = "entity_unavailable"
EXCLUSION_HVAC_NOT_HEATING: Final = "hvac_not_heating"
EXCLUSION_NOT_READY: Final = "not_ready"
EXCLUSION_NOT_INITIALIZED: Final = "not_initialized"
EXCLUSION_NOT_BOILER_PARTICIPANT: Final = "not_boiler_participant"
EXCLUSION_WINDOW_OPEN: Final = "window_open"
EXCLUSION_WINDOW_UNKNOWN: Final = "window_unknown"
EXCLUSION_INHIBITED: Final = "inhibited"
EXCLUSION_SAFETY: Final = "safety"
EXCLUSION_OVERPOWERING: Final = "overpowering"
EXCLUSION_AVATTO_NO_DEMAND: Final = "avatto_no_demand"
EXCLUSION_TEMPERATURE_MISSING: Final = "temperature_missing"
EXCLUSION_TEMPERATURE_STALE: Final = "temperature_stale"
EXCLUSION_TEMPERATURE_FUTURE: Final = "temperature_future"
EXCLUSION_PDC_SESSION_ACTIVE: Final = "pdc_session_active"
EXCLUSION_PDC_SESSION_UNKNOWN: Final = "pdc_session_unknown"
EXCLUSION_SPLIT_NOT_OFF: Final = "split_not_off"
EXCLUSION_SPLIT_UNKNOWN: Final = "split_unknown"
EXCLUSION_NO_ACTIVE_VALVE: Final = "no_active_valve"
EXCLUSION_UNIT_UNSUPPORTED: Final = "unit_unsupported"
EXCLUSION_COUNT_INVALID: Final = "count_invalid"

EXCLUSION_CODES: Final = (
    EXCLUSION_ENTITY_UNAVAILABLE,
    EXCLUSION_HVAC_NOT_HEATING,
    EXCLUSION_NOT_READY,
    EXCLUSION_NOT_INITIALIZED,
    EXCLUSION_NOT_BOILER_PARTICIPANT,
    EXCLUSION_WINDOW_OPEN,
    EXCLUSION_WINDOW_UNKNOWN,
    EXCLUSION_INHIBITED,
    EXCLUSION_SAFETY,
    EXCLUSION_OVERPOWERING,
    EXCLUSION_AVATTO_NO_DEMAND,
    EXCLUSION_TEMPERATURE_MISSING,
    EXCLUSION_TEMPERATURE_STALE,
    EXCLUSION_TEMPERATURE_FUTURE,
    EXCLUSION_PDC_SESSION_ACTIVE,
    EXCLUSION_PDC_SESSION_UNKNOWN,
    EXCLUSION_SPLIT_NOT_OFF,
    EXCLUSION_SPLIT_UNKNOWN,
    EXCLUSION_NO_ACTIVE_VALVE,
    EXCLUSION_UNIT_UNSUPPORTED,
    EXCLUSION_COUNT_INVALID,
)

# Configuration error codes (also used as config flow error keys, with prefix).
CONFIG_ERROR_INVALID_SECTION: Final = "invalid_section"
CONFIG_ERROR_UNSUPPORTED_MODE: Final = "unsupported_mode"
CONFIG_ERROR_CONTROL_NOT_CONFIRMED: Final = "control_not_confirmed"
CONFIG_ERROR_REQUIRED: Final = "required"
CONFIG_ERROR_INVALID_ENTITY: Final = "invalid_entity"
CONFIG_ERROR_INVALID_DOMAIN: Final = "invalid_domain"
CONFIG_ERROR_DUPLICATE_BINDING: Final = "duplicate_binding"
CONFIG_ERROR_INVALID_ROOM_KEY: Final = "invalid_room_key"
CONFIG_ERROR_DUPLICATE_ROOM: Final = "duplicate_room"
CONFIG_ERROR_PDC_INCOMPLETE: Final = "pdc_incomplete"
CONFIG_ERROR_TOO_MANY_ROOMS: Final = "too_many_rooms"
CONFIG_ERROR_TOO_MANY_CONSENTS: Final = "too_many_consents"
CONFIG_ERROR_TOO_MANY_EXTERNAL_OWNERS: Final = "too_many_external_owners"
CONFIG_ERROR_INVALID_ROOM: Final = "invalid_room"


class OrchestrationConfigError(ValueError):
    """Invalid heating orchestration configuration.

    ``code`` is one of the stable ``CONFIG_ERROR_*`` values and ``key`` names the
    offending configuration key (or ``rooms``) when known.
    """

    def __init__(self, code: str, key: str | None = None, detail: str | None = None) -> None:
        super().__init__(f"{code}:{key}" if key else code)
        self.code = code
        self.key = key
        self.detail = detail


@dataclass(frozen=True, slots=True)
class RoomBinding:
    """Association between a VTherm room and the optional PDC entities."""

    room_key: str
    climate_entity_id: str
    pilot_filter: bool = False
    split_entity_id: str | None = None
    session_entity_id: str | None = None

    @property
    def has_pdc(self) -> bool:
        """True when the room radiator depends on a PDC session and split."""
        return self.split_entity_id is not None


@dataclass(frozen=True, slots=True)
class OrchestrationConfig:
    """Validated configuration of the boiler orchestration."""

    mode: str = MODE_DISABLED
    master_entity_id: str | None = None
    central_mode_entity_id: str | None = None
    active_count_entity_id: str | None = None
    manual_request_entity_id: str | None = None
    pilot_filter_entity_id: str | None = None
    pilot_ready_entity_id: str | None = None
    consent_entity_ids: tuple[str, ...] = ()
    legacy_cycle_entity_id: str | None = None
    external_owner_entity_ids: tuple[str, ...] = ()
    control_confirmed: bool = False
    rooms: tuple[RoomBinding, ...] = ()

    def required_entity_ids(self) -> tuple[str, ...]:
        """Central entities that must exist before the first observation."""
        ids = [self.master_entity_id, self.central_mode_entity_id, self.active_count_entity_id]
        ids.extend(self.consent_entity_ids)
        return tuple(entity_id for entity_id in ids if entity_id)

    def configured_entity_ids(self) -> tuple[str, ...]:
        """All entity ids written in the configuration (without the legacy seed)."""
        ids: list[str] = [
            entity_id
            for entity_id in (
                self.master_entity_id,
                self.central_mode_entity_id,
                self.active_count_entity_id,
                self.manual_request_entity_id,
                self.pilot_filter_entity_id,
                self.pilot_ready_entity_id,
            )
            if entity_id
        ]
        ids.extend(self.consent_entity_ids)
        for room in self.rooms:
            ids.append(room.climate_entity_id)
            if room.split_entity_id:
                ids.append(room.split_entity_id)
            if room.session_entity_id:
                ids.append(room.session_entity_id)
        return tuple(dict.fromkeys(ids))


def _check_entity_id(value: Any, key: str, domains: Sequence[str], *, required: bool) -> str | None:
    """Validate one entity id binding."""
    if value in (None, ""):
        if required:
            raise OrchestrationConfigError(CONFIG_ERROR_REQUIRED, key)
        return None
    if not isinstance(value, str) or not ENTITY_ID_PATTERN.match(value):
        raise OrchestrationConfigError(CONFIG_ERROR_INVALID_ENTITY, key)
    if value.split(".", 1)[0] not in domains:
        raise OrchestrationConfigError(CONFIG_ERROR_INVALID_DOMAIN, key)
    return value


def _parse_room(item: Any) -> RoomBinding:
    """Validate one room element ``{room_key, climate_entity_id, pilot_filter, pdc}``."""
    if not isinstance(item, Mapping):
        raise OrchestrationConfigError(CONFIG_ERROR_INVALID_ROOM, KEY_ROOMS)
    room_key = item.get(KEY_ROOM_KEY)
    if not isinstance(room_key, str) or not ROOM_KEY_PATTERN.match(room_key):
        raise OrchestrationConfigError(CONFIG_ERROR_INVALID_ROOM_KEY, KEY_ROOM_KEY)
    climate = _check_entity_id(item.get(KEY_ROOM_CLIMATE), KEY_ROOM_CLIMATE, ROOM_DOMAINS, required=True)
    pilot_filter = item.get(KEY_ROOM_PILOT_FILTER, False)
    if not isinstance(pilot_filter, bool):
        raise OrchestrationConfigError(CONFIG_ERROR_INVALID_ROOM, KEY_ROOM_PILOT_FILTER)
    pdc = item.get(KEY_ROOM_PDC)
    split = session = None
    if pdc not in (None, {}):
        if not isinstance(pdc, Mapping):
            raise OrchestrationConfigError(CONFIG_ERROR_PDC_INCOMPLETE, KEY_ROOM_PDC)
        split = _check_entity_id(pdc.get(KEY_PDC_SPLIT), KEY_PDC_SPLIT, SPLIT_DOMAINS, required=False)
        session = _check_entity_id(pdc.get(KEY_PDC_SESSION), KEY_PDC_SESSION, SESSION_DOMAINS, required=False)
        if (split is None) != (session is None):
            raise OrchestrationConfigError(CONFIG_ERROR_PDC_INCOMPLETE, KEY_ROOM_PDC)
        if split is not None and split == climate:
            raise OrchestrationConfigError(CONFIG_ERROR_DUPLICATE_BINDING, KEY_PDC_SPLIT)
    assert climate is not None
    return RoomBinding(room_key=room_key, climate_entity_id=climate, pilot_filter=pilot_filter, split_entity_id=split, session_entity_id=session)


def parse_room_bindings(raw_rooms: Any) -> tuple[RoomBinding, ...]:
    """Validate the room list alone: shape, keys, domains and duplicates among rooms.

    Bindings shared with the central entities are checked by
    ``parse_orchestration_config``.
    """
    if raw_rooms is None:
        raw_rooms = []
    if isinstance(raw_rooms, (str, bytes, Mapping)) or not isinstance(raw_rooms, Sequence):
        raise OrchestrationConfigError(CONFIG_ERROR_INVALID_ROOM, KEY_ROOMS)
    if len(raw_rooms) > MAX_ROOMS:
        raise OrchestrationConfigError(CONFIG_ERROR_TOO_MANY_ROOMS, KEY_ROOMS)
    rooms = tuple(_parse_room(item) for item in raw_rooms)
    room_keys: set[str] = set()
    entities: set[str] = set()
    for room in rooms:
        if room.room_key in room_keys:
            raise OrchestrationConfigError(CONFIG_ERROR_DUPLICATE_ROOM, KEY_ROOM_KEY)
        room_keys.add(room.room_key)
        for key, value in ((KEY_ROOM_CLIMATE, room.climate_entity_id), (KEY_PDC_SPLIT, room.split_entity_id), (KEY_PDC_SESSION, room.session_entity_id)):
            if value is None:
                continue
            if value in entities:
                raise OrchestrationConfigError(CONFIG_ERROR_DUPLICATE_BINDING, key)
            entities.add(value)
    return rooms


def parse_orchestration_config(section: Any) -> OrchestrationConfig:
    """Validate the stored configuration section.

    An absent section means ``disabled``. A ``disabled`` section is accepted
    whatever the other keys contain: they are kept for a later activation but
    never read. ``control`` and unknown modes raise, so the caller stays
    inactive with an explicit error instead of controlling implicitly.
    """
    if section is None:
        return OrchestrationConfig()
    if not isinstance(section, Mapping):
        raise OrchestrationConfigError(CONFIG_ERROR_INVALID_SECTION)
    mode = section.get(KEY_MODE, MODE_DISABLED)
    if mode not in KNOWN_MODES:
        raise OrchestrationConfigError(CONFIG_ERROR_UNSUPPORTED_MODE, KEY_MODE)
    if mode == MODE_DISABLED:
        return OrchestrationConfig()

    master = _check_entity_id(section.get(KEY_MASTER), KEY_MASTER, MASTER_DOMAINS, required=True)
    central = _check_entity_id(section.get(KEY_CENTRAL_MODE), KEY_CENTRAL_MODE, CENTRAL_MODE_DOMAINS, required=True)
    count = _check_entity_id(section.get(KEY_ACTIVE_COUNT), KEY_ACTIVE_COUNT, ACTIVE_COUNT_DOMAINS, required=True)
    manual = _check_entity_id(section.get(KEY_MANUAL), KEY_MANUAL, BINARY_INPUT_DOMAINS, required=False)
    pilot = _check_entity_id(section.get(KEY_PILOT_FILTER), KEY_PILOT_FILTER, BINARY_INPUT_DOMAINS, required=False)
    pilot_ready = _check_entity_id(section.get(KEY_PILOT_READY), KEY_PILOT_READY, BINARY_INPUT_DOMAINS, required=False)
    legacy = _check_entity_id(section.get(KEY_LEGACY_CYCLE), KEY_LEGACY_CYCLE, LEGACY_CYCLE_DOMAINS, required=False)

    raw_consents = section.get(KEY_CONSENTS)
    if isinstance(raw_consents, str):
        raw_consents = [raw_consents]
    if not isinstance(raw_consents, Sequence) or len(raw_consents) == 0:
        raise OrchestrationConfigError(CONFIG_ERROR_REQUIRED, KEY_CONSENTS)
    if len(raw_consents) > MAX_CONSENTS:
        raise OrchestrationConfigError(CONFIG_ERROR_TOO_MANY_CONSENTS, KEY_CONSENTS)
    consents = tuple(_check_entity_id(value, KEY_CONSENTS, BINARY_INPUT_DOMAINS, required=True) or "" for value in raw_consents)

    central_bindings: list[tuple[str, str]] = [
        (key, value)
        for key, value in (
            (KEY_MASTER, master),
            (KEY_CENTRAL_MODE, central),
            (KEY_ACTIVE_COUNT, count),
            (KEY_MANUAL, manual),
            (KEY_PILOT_FILTER, pilot),
            (KEY_PILOT_READY, pilot_ready),
            (KEY_LEGACY_CYCLE, legacy),
        )
        if value is not None
    ]
    central_bindings.extend((KEY_CONSENTS, value) for value in consents)

    # External controllers that must not be active while this one controls. Required
    # for ``control``; when present in ``observe`` they are validated and only shown.
    raw_owners = section.get(KEY_EXTERNAL_OWNERS)
    if isinstance(raw_owners, str):
        raw_owners = [raw_owners]
    if raw_owners is None:
        raw_owners = []
    if isinstance(raw_owners, (bytes, Mapping)) or not isinstance(raw_owners, Sequence):
        raise OrchestrationConfigError(CONFIG_ERROR_INVALID_ENTITY, KEY_EXTERNAL_OWNERS)
    if mode == MODE_CONTROL and len(raw_owners) == 0:
        raise OrchestrationConfigError(CONFIG_ERROR_REQUIRED, KEY_EXTERNAL_OWNERS)
    if len(raw_owners) > MAX_EXTERNAL_OWNERS:
        raise OrchestrationConfigError(CONFIG_ERROR_TOO_MANY_EXTERNAL_OWNERS, KEY_EXTERNAL_OWNERS)
    owners = tuple(_check_entity_id(value, KEY_EXTERNAL_OWNERS, EXTERNAL_OWNER_DOMAINS, required=True) or "" for value in raw_owners)
    central_bindings.extend((KEY_EXTERNAL_OWNERS, value) for value in owners)

    confirmed = section.get(KEY_CONTROL_CONFIRMED, False)
    if mode == MODE_CONTROL and confirmed is not True:
        raise OrchestrationConfigError(CONFIG_ERROR_CONTROL_NOT_CONFIRMED, KEY_CONTROL_CONFIRMED)
    seen: dict[str, str] = {}
    for key, value in central_bindings:
        if value in seen:
            raise OrchestrationConfigError(CONFIG_ERROR_DUPLICATE_BINDING, key)
        seen[value] = key

    rooms = parse_room_bindings(section.get(KEY_ROOMS, []))
    for room in rooms:
        for key, value in ((KEY_ROOM_CLIMATE, room.climate_entity_id), (KEY_PDC_SPLIT, room.split_entity_id), (KEY_PDC_SESSION, room.session_entity_id)):
            if value is None:
                continue
            if value in seen:
                raise OrchestrationConfigError(CONFIG_ERROR_DUPLICATE_BINDING, key)
            seen[value] = key

    return OrchestrationConfig(
        mode=mode,
        master_entity_id=master,
        central_mode_entity_id=central,
        active_count_entity_id=count,
        manual_request_entity_id=manual,
        pilot_filter_entity_id=pilot,
        pilot_ready_entity_id=pilot_ready,
        consent_entity_ids=consents,
        legacy_cycle_entity_id=legacy,
        external_owner_entity_ids=owners,
        control_confirmed=confirmed is True,
        rooms=rooms,
    )


# ---------------------------------------------------------------------------
# Snapshot access with the semantics of the Home Assistant template helpers.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _StateView:
    state: Any
    attributes: Mapping[str, Any]


def _lookup(states: Mapping[str, Any], entity_id: str | None) -> _StateView | None:
    """Return the state of ``entity_id`` or ``None`` when it does not exist.

    The mapping values may be Home Assistant ``State`` objects or plain
    ``{"state": ..., "attributes": {...}}`` dictionaries (fixtures).
    """
    if not entity_id:
        return None
    try:
        value = states.get(entity_id)
    except (TypeError, KeyError):
        return None
    if value is None:
        return None
    if isinstance(value, Mapping):
        state = value.get("state")
        attributes = value.get("attributes")
    else:
        state = getattr(value, "state", None)
        attributes = getattr(value, "attributes", None)
    if not isinstance(attributes, Mapping):
        attributes = {}
    return _StateView(state=state, attributes=attributes)


def _states(states: Mapping[str, Any], entity_id: str | None) -> str:
    """``states(entity_id)``: the state string, ``unknown`` when absent."""
    view = _lookup(states, entity_id)
    if view is None or view.state is None:
        return STATE_UNKNOWN
    return str(view.state)


def _is_state(states: Mapping[str, Any], entity_id: str | None, values: Sequence[str] | str) -> bool:
    """``is_state(entity_id, value_or_list)``."""
    view = _lookup(states, entity_id)
    if view is None:
        return False
    if isinstance(values, str):
        return view.state == values
    return view.state in values


def _has_value(view: _StateView | None) -> bool:
    """``has_value(entity_id)``: the entity exists and is not unknown/unavailable."""
    return view is not None and view.state not in (None, STATE_UNKNOWN, STATE_UNAVAILABLE)


def _section(attributes: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """``a.get(key) or {}``; a malformed non mapping section reads as empty (hardening)."""
    value = attributes.get(key)
    if not value or not isinstance(value, Mapping):
        return {}
    return value


def _protection_section(attributes: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    """``a.get(key) or {}`` for a protection; ``None`` when malformed.

    The template raises on a truthy non mapping section. A protection that
    cannot be read is reported as active (conservative hardening).
    """
    value = attributes.get(key)
    if not value:
        return {}
    return value if isinstance(value, Mapping) else None


def _jinja_float(value: Any) -> float | None:
    """``value | float(none)``: Python ``float`` or ``None`` (booleans convert like Jinja)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _as_timestamp(value: Any, default: float, local_tz: tzinfo) -> float:
    """``as_timestamp(value, default)`` for ISO 8601 strings and datetimes.

    Aware values are converted with their offset. Naive values are read in
    ``local_tz`` (the template reads them in the process local time).
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return default
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_tz)
    try:
        timestamp = parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return default
    return timestamp if math.isfinite(timestamp) else default


def _unique_key(value: Any) -> Any:
    """Key of the Jinja ``unique`` filter: strings compare case-insensitively."""
    return value.lower() if isinstance(value, str) else value


def _unique(values: Sequence[Any]) -> list[Any]:
    """Jinja ``unique``: first occurrence wins; raises ``TypeError`` on unhashable items."""
    seen: set[Any] = set()
    result: list[Any] = []
    for value in values:
        key = _unique_key(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _is_jinja_sequence(value: Any) -> bool:
    """``value is sequence and value is not string and value is not mapping``."""
    if value is None or isinstance(value, (str, bytes, Mapping)):
        return False
    return hasattr(value, "__len__") and hasattr(value, "__getitem__")


# ---------------------------------------------------------------------------
# Collected result.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CollectedRoom:
    """An eligible room: at least one valid physical valve."""

    room_key: str
    climate_entity_id: str
    target: float
    gap: float
    pilot_ok: bool
    valves: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """Diagnostic form (CONTRATTI.md section 2, ``rooms``)."""
        return {
            "climate_entity_id": self.climate_entity_id,
            "room_key": self.room_key,
            "target": self.target,
            "gap": self.gap,
            "pilot_ok": self.pilot_ok,
            "valves": list(self.valves),
        }


@dataclass(frozen=True, slots=True)
class ExcludedRoom:
    """A configured room that is not eligible, with every observed code."""

    room_key: str
    climate_entity_id: str
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """Diagnostic form (CONTRATTI.md section 2, ``excluded_rooms``)."""
        return {"climate_entity_id": self.climate_entity_id, "room_key": self.room_key, "reasons": list(self.reasons)}


@dataclass(frozen=True, slots=True)
class BoilerCollection:
    """Result of one collection, bound to one ``now``."""

    now: float
    raw: dict[str, Any]
    count_raw: int | None
    count_raw_valid: bool
    temperature_unit_supported: bool
    inputs: dict[str, Any]
    rooms: tuple[CollectedRoom, ...]
    excluded_rooms: tuple[ExcludedRoom, ...]
    watched_entity_ids: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        """``ok`` passed to M1."""
        return bool(self.raw["ok"])

    @property
    def valid_valve_count(self) -> int:
        """Unique valid physical valves (``count`` passed to M1)."""
        return int(self.raw["count"])


def _count_validity(states: Mapping[str, Any], count_entity_id: str | None) -> tuple[bool, int | None, Any]:
    """Return ``(valid_count, count_raw, ids)`` like the template prologue."""
    view = _lookup(states, count_entity_id)
    ids = view.attributes.get("active_device_ids") if view is not None else None
    count = _jinja_float(_states(states, count_entity_id))
    count_raw: int | None = None
    if count is not None and math.isfinite(count) and count == int(count):
        count_raw = int(count)
    # Non finite counts are invalid (hardening: the template raises on ``inf|int``).
    if count is None or not math.isfinite(count) or count < 0 or count != int(count) or not _is_jinja_sequence(ids):
        return False, count_raw, ids
    try:
        unique_ids = _unique(list(ids))
    except TypeError:
        # Unhashable ids make the template raise; the count is not trusted.
        return False, count_raw, ids
    return count == len(unique_ids), count_raw, ids


def _window_closed(states: Mapping[str, Any], window: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Return ``(closed, exclusion_code)`` for the window gate."""
    sensor = window.get("window_sensor_entity_id")
    bypass = window.get("is_window_bypass") == True  # noqa: E712 - Jinja ``== true`` semantics
    closed = bypass or (window.get("window_state") == STATE_OFF and isinstance(sensor, str) and _is_state(states, sensor, STATE_OFF))
    if closed:
        return True, None
    if window.get("window_state") == STATE_ON or (isinstance(sensor, str) and _is_state(states, sensor, STATE_ON)):
        return False, EXCLUSION_WINDOW_OPEN
    return False, EXCLUSION_WINDOW_UNKNOWN


def _pdc_codes(states: Mapping[str, Any], room: RoomBinding) -> list[str]:
    """PDC gate: the radiator is allowed only with session and split confirmed ``off``."""
    if not room.has_pdc:
        return []
    codes: list[str] = []
    session = _lookup(states, room.session_entity_id)
    if session is None or session.state != STATE_OFF:
        codes.append(EXCLUSION_PDC_SESSION_ACTIVE if session is not None and session.state == STATE_ON else EXCLUSION_PDC_SESSION_UNKNOWN)
    split = _lookup(states, room.split_entity_id)
    if split is None or split.state != STATE_OFF:
        codes.append(EXCLUSION_SPLIT_NOT_OFF if _has_value(split) else EXCLUSION_SPLIT_UNKNOWN)
    return codes


def _underlying_ids(over: Mapping[str, Any]) -> list[Any]:
    """``over.get('underlying_entities', [])``; a non list value reads as empty (hardening)."""
    value = over.get("underlying_entities", [])
    if not _is_jinja_sequence(value):
        return []
    return list(value)


def collect_boiler_inputs(
    states: Mapping[str, Any],
    config: OrchestrationConfig,
    now: float,
    *,
    temperature_unit: str | None = SUPPORTED_TEMPERATURE_UNIT,
    local_tz: tzinfo = timezone.utc,
) -> BoilerCollection:
    """Collect one snapshot at ``now`` (absolute Unix seconds).

    ``states`` is read only. ``temperature_unit`` is the unit of the VTherm
    temperatures (the Home Assistant unit system): M1 thresholds are Celsius,
    so any other unit invalidates the inputs instead of comparing Fahrenheit
    values with Celsius thresholds.
    """
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(float(now)):
        raise ValueError("now must be a finite number")
    now_s = float(now)
    unit_ok = temperature_unit == SUPPORTED_TEMPERATURE_UNIT

    valid_count, count_raw, ids = _count_validity(states, config.active_count_entity_id)

    collected: list[CollectedRoom] = []
    excluded: list[ExcludedRoom] = []
    raw_rooms: list[dict[str, Any]] = []
    all_valves: list[Any] = []
    watched: list[str] = list(config.configured_entity_ids())

    for room in config.rooms:
        codes: list[str] = []
        view = _lookup(states, room.climate_entity_id)
        available = _has_value(view)
        attributes: Mapping[str, Any] = view.attributes if (available and view is not None) else {}
        specific = _section(attributes, "specific_states")
        configuration = _section(attributes, "configuration")
        over = _section(attributes, "vtherm_over_climate")
        window = _section(attributes, "window_manager")

        sensor = window.get("window_sensor_entity_id")
        if isinstance(sensor, str):
            watched.append(sensor)
        underlying = _underlying_ids(over)
        watched.extend(valve for valve in underlying if isinstance(valve, str))

        if not valid_count:
            codes.append(EXCLUSION_COUNT_INVALID)
        if not unit_ok:
            codes.append(EXCLUSION_UNIT_UNSUPPORTED)

        current = target = None
        if not available:
            codes.append(EXCLUSION_ENTITY_UNAVAILABLE)
        else:
            assert view is not None
            if view.state not in HEATING_HVAC_MODES:
                codes.append(EXCLUSION_HVAC_NOT_HEATING)
            if not attributes.get("is_ready") == True:  # noqa: E712
                codes.append(EXCLUSION_NOT_READY)
            if not specific.get("is_initialized") == True:  # noqa: E712
                codes.append(EXCLUSION_NOT_INITIALIZED)
            if not configuration.get("is_used_by_central_boiler") == True:  # noqa: E712
                codes.append(EXCLUSION_NOT_BOILER_PARTICIPANT)
            closed, window_code = _window_closed(states, window)
            if not closed and window_code is not None:
                codes.append(window_code)
        codes.extend(_pdc_codes(states, room))
        if available:
            inhibition = _protection_section(specific, "inhibition")
            if inhibition is None or not inhibition.get("inhibited", False) == False:  # noqa: E712
                codes.append(EXCLUSION_INHIBITED)
            safety = _protection_section(attributes, "safety_manager")
            if safety is None or safety.get("safety_state", STATE_OFF) != STATE_OFF:
                codes.append(EXCLUSION_SAFETY)
            power = _protection_section(attributes, "power_manager")
            if power is None or power.get("overpowering_state", STATE_OFF) != STATE_OFF:
                codes.append(EXCLUSION_OVERPOWERING)
            if not over.get("avatto_room_demand") != False:  # noqa: E712
                codes.append(EXCLUSION_AVATTO_NO_DEMAND)
            current = _jinja_float(attributes.get("current_temperature"))
            target = _jinja_float(attributes.get("temperature"))
            # Non finite temperatures are missing (hardening, see the module docstring).
            if current is None or target is None or not math.isfinite(current) or not math.isfinite(target):
                codes.append(EXCLUSION_TEMPERATURE_MISSING)
            age = now_s - _as_timestamp(specific.get("last_temperature_datetime"), 0.0, local_tz)
            if age < 0:
                codes.append(EXCLUSION_TEMPERATURE_FUTURE)
            elif age > MAX_TEMPERATURE_AGE_S:
                codes.append(EXCLUSION_TEMPERATURE_STALE)

        if codes:
            excluded.append(ExcludedRoom(room.room_key, room.climate_entity_id, tuple(_ordered(codes))))
            continue

        assert current is not None and target is not None and ids is not None
        active = [
            valve for valve in underlying if valve in ids and _is_state(states, valve, HEATING_HVAC_MODES) and _state_attr(states, valve, "hvac_action") == HVAC_ACTION_HEATING
        ]
        if not active:
            excluded.append(ExcludedRoom(room.room_key, room.climate_entity_id, (EXCLUSION_NO_ACTIVE_VALVE,)))
            continue
        gap = round(target - current, 3)
        pilot_ok = (not room.pilot_filter) or over.get("avatto_room_demand") == True  # noqa: E712
        raw_rooms.append({"slug": room.room_key, "target": target, "gap": gap, "pilot_ok": pilot_ok})
        all_valves.extend(active)
        collected.append(
            CollectedRoom(
                room_key=room.room_key,
                climate_entity_id=room.climate_entity_id,
                target=target,
                gap=gap,
                pilot_ok=pilot_ok,
                valves=tuple(str(valve) for valve in _unique(active)),
            )
        )

    manual = _states(states, config.manual_request_entity_id) if config.manual_request_entity_id else STATE_OFF
    pilot = _states(states, config.pilot_filter_entity_id) if config.pilot_filter_entity_id else STATE_OFF
    pilot_ready = _is_state(states, config.pilot_ready_entity_id, STATE_ON) if config.pilot_ready_entity_id else False
    consent = bool(config.consent_entity_ids) and all(_is_state(states, entity_id, STATE_ON) for entity_id in config.consent_entity_ids)
    central_view = _lookup(states, config.central_mode_entity_id)

    raw = {
        "ok": bool(valid_count and _is_state(states, config.central_mode_entity_id, CENTRAL_MODES_ALLOWING_HEAT) and unit_ok),
        "master": _states(states, config.master_entity_id),
        "count": len(_unique(all_valves)),
        "rooms": raw_rooms,
        "manual": manual,
        "pilot": pilot,
        "pilot_ready": pilot_ready,
        "consent": consent,
    }
    inputs = {
        "central_mode": None if central_view is None or central_view.state is None else str(central_view.state),
        "manual": manual,
        "pilot": pilot,
        "pilot_ready": pilot_ready,
        "consent": consent,
    }
    return BoilerCollection(
        now=now_s,
        raw=raw,
        count_raw=count_raw,
        count_raw_valid=valid_count,
        temperature_unit_supported=unit_ok,
        inputs=inputs,
        rooms=tuple(collected),
        excluded_rooms=tuple(excluded),
        watched_entity_ids=tuple(dict.fromkeys(watched)),
    )


def _state_attr(states: Mapping[str, Any], entity_id: Any, name: str) -> Any:
    """``state_attr(entity_id, name)``."""
    view = _lookup(states, entity_id) if isinstance(entity_id, str) else None
    return None if view is None else view.attributes.get(name)


def _ordered(codes: Sequence[str]) -> list[str]:
    """Deduplicate and sort codes in the stable reporting order."""
    unique = set(codes)
    return [code for code in EXCLUSION_CODES if code in unique]


# ---------------------------------------------------------------------------
# Diagnostic payload (schema v1, CONTRATTI.md section 2).
# ---------------------------------------------------------------------------

CONDITION_UNITS: Final = {
    "thermal_gap": "°C",
    "valve_count": "count",
    "stability": "s",
    "cooldown": "s",
}
MAX_ERROR_LENGTH: Final = 255
MAX_LIST_ITEMS: Final = 64

CYCLE_ORIGIN_LEGACY_SEED: Final = "legacy_seed"
CYCLE_ORIGIN_LOCAL_RESTORE: Final = "local_restore"
CYCLE_ORIGIN_NEW_OBSERVATION: Final = "new_observation"
CYCLE_ORIGINS: Final = (CYCLE_ORIGIN_LEGACY_SEED, CYCLE_ORIGIN_LOCAL_RESTORE, CYCLE_ORIGIN_NEW_OBSERVATION)

# Control fields of schema 1.1 (observe and disabled keep these values).
DEFAULT_CONTROL_FIELDS: Final = {
    "control_status": "inactive",
    "command_status": None,
    "commanded_mode": None,
    "last_command_at": None,
    "last_confirmation_at": None,
    "service_completed_at": None,
    "command_attempts": 0,
    "command_retry_at": None,
    "external_owners": [],
}

STATE_DISABLED: Final = "disabled"
STATE_STARTING: Final = "starting"
STATE_ERROR: Final = "error"
OWNER_OBSERVER: Final = "observer"


def json_safe(value: Any, *, depth: int = 0) -> Any:
    """Return a JSON value without NaN/Infinity, tuples or unbounded nesting."""
    if depth > 6:
        return None
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): json_safe(item, depth=depth + 1) for key, item in list(value.items())[:MAX_LIST_ITEMS]}
    if isinstance(value, (list, tuple)):
        return [json_safe(item, depth=depth + 1) for item in list(value)[:MAX_LIST_ITEMS]]
    return str(value)


def cycle_to_dict(cycle: Any) -> dict[str, Any]:
    """Serialize an M1 ``BoilerCycle`` with the baseline ``m/off/r/s`` keys."""
    if cycle is None:
        return {"m": None, "off": None, "r": None, "s": []}
    single = cycle.single
    return {
        "m": cycle.mode,
        "off": cycle.off_since,
        "r": cycle.normal_since,
        "s": [] if single is None else [single.slug, single.target, single.since],
    }


def condition_to_dict(condition: Any) -> dict[str, Any]:
    """Serialize an M1 ``BoilerCondition`` with its unit."""
    return {
        "id": condition.id,
        "status": condition.status,
        "reason_code": condition.reason_code,
        "actual": json_safe(condition.actual),
        "expected": json_safe(condition.expected),
        "deadline": json_safe(condition.deadline),
        "unit": CONDITION_UNITS.get(condition.id),
    }


def _truncate_error(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= MAX_ERROR_LENGTH else text[: MAX_ERROR_LENGTH - 1] + "…"


def empty_diagnostics(
    mode: str,
    *,
    owner: str | None = None,
    generation: int = 0,
    evaluated_at: float | None = None,
    last_error: str | None = None,
    cycle_origin: str | None = None,
    control: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Attributes for ``disabled``, ``starting`` and ``error`` states (no decision)."""
    payload = {
        **DEFAULT_CONTROL_FIELDS,
        **(control or {}),
    }
    return json_safe(
        {
            "schema_version": SCHEMA_VERSION,
            "schema_minor": SCHEMA_MINOR,
            "mode": mode,
            "owner": owner,
            "evaluated_at": evaluated_at,
            "generation": generation,
            "proposed_action": None,
            "reason": None,
            "deadline": None,
            "master_mode": None,
            "count_raw": None,
            "count_raw_valid": False,
            "valid_valve_count": 0,
            "inputs_ok": False,
            "inputs": {"central_mode": None, "manual": None, "pilot": None, "pilot_ready": None, "consent": None},
            "rooms": [],
            "excluded_rooms": [],
            "conditions": [],
            "cycle": {"m": None, "off": None, "r": None, "s": []},
            "cycle_origin": cycle_origin,
            **payload,
            "last_error": _truncate_error(last_error),
        }
    )


def build_boiler_diagnostics(
    collection: BoilerCollection,
    decision: Any,
    *,
    mode: str,
    owner: str | None,
    generation: int,
    cycle_origin: str,
    last_error: str | None = None,
    control: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Diagnostic attributes of one published evaluation (schema v1.1).

    ``decision`` is the M1 ``BoilerDecision`` computed on the same snapshot and
    with the same ``now`` as ``collection``. ``control`` carries the M2-B fields
    (``heating_boiler_command.command_diagnostics``); without it the command
    fields keep their idle values: an observer never sends a command.
    """
    if decision.evaluated_at != collection.now:
        raise ValueError("decision and collection must share the same clock")
    payload = {
        **DEFAULT_CONTROL_FIELDS,
        **(control or {}),
    }
    return json_safe(
        {
            "schema_version": SCHEMA_VERSION,
            "schema_minor": SCHEMA_MINOR,
            "mode": mode,
            "owner": owner,
            "evaluated_at": collection.now,
            "generation": generation,
            "proposed_action": decision.action,
            "reason": decision.reason,
            "deadline": decision.deadline,
            "master_mode": collection.raw["master"],
            "count_raw": collection.count_raw,
            "count_raw_valid": collection.count_raw_valid,
            "valid_valve_count": collection.valid_valve_count,
            "inputs_ok": collection.ok,
            "inputs": dict(collection.inputs),
            "rooms": [room.as_dict() for room in collection.rooms],
            "excluded_rooms": [room.as_dict() for room in collection.excluded_rooms],
            "conditions": [condition_to_dict(condition) for condition in decision.conditions],
            "cycle": cycle_to_dict(decision.cycle),
            "cycle_origin": cycle_origin,
            **payload,
            "last_error": _truncate_error(last_error),
        }
    )


# Attributes that carry the decision. The others (clock, generation, gaps, actual
# values) change at every evaluation and are refreshed by the heartbeat only.
SIGNIFICANT_SCALAR_KEYS: Final = (
    "schema_version",
    "schema_minor",
    "mode",
    "owner",
    "proposed_action",
    "reason",
    "deadline",
    "master_mode",
    "count_raw",
    "count_raw_valid",
    "valid_valve_count",
    "inputs_ok",
    "cycle_origin",
    "control_status",
    "command_status",
    "commanded_mode",
    "last_command_at",
    "last_confirmation_at",
    "service_completed_at",
    "command_attempts",
    "command_retry_at",
    "last_error",
)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def publication_signature(state: str, attributes: Mapping[str, Any]) -> tuple[Any, ...]:
    """Hashable summary of what a consumer must be told as soon as it changes.

    It covers the state (M1 reason), the decision, the control and command
    fields, the inputs, the set of eligible rooms with their valves, the
    exclusion codes, the status of every condition and the cycle anchors. It
    excludes ``evaluated_at``, ``generation``, the numeric gaps and targets and
    the ``actual`` values of the conditions, which change with every sample.
    """
    rooms = tuple((room.get("climate_entity_id"), room.get("pilot_ok"), _freeze(room.get("valves"))) for room in attributes.get("rooms") or ())
    excluded = tuple((room.get("climate_entity_id"), _freeze(room.get("reasons"))) for room in attributes.get("excluded_rooms") or ())
    conditions = tuple((item.get("id"), item.get("status"), item.get("reason_code")) for item in attributes.get("conditions") or ())
    return (
        state,
        tuple(_freeze(attributes.get(key)) for key in SIGNIFICANT_SCALAR_KEYS),
        _freeze(attributes.get("inputs")),
        _freeze(attributes.get("cycle")),
        _freeze(attributes.get("external_owners")),
        rooms,
        excluded,
        conditions,
    )
