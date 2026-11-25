"""Pure collector of the PDC (heat pump) inputs and diagnostics (runtime W2).

It maps a read-only mapping of Home Assistant states to the pure models of the
other PDC modules:

* :func:`collect_pdc_snapshot`: one room's :class:`PdcSnapshot` (``heating_pdc_policy``),
  with the entity reads of the baseline automation ``*_scelta_radiatore_o_pompa_di_calore``
  (``states()``, ``state_attr()``, ``states[...].last_reported/last_changed``) and the
  semantics of the Jinja ``float(none)`` filter;
* :func:`build_observation`: the :class:`PdcObservation` the transaction machine
  (``heating_pdc_transaction``) needs after each wait, with the baseline re-checks of
  the phase computed by the policy predicates;
* :func:`unsafe_during_start` and :func:`radiator_released_while_running`: the
  protections that the runtime applies *during* a transaction (the baseline
  ``mode: single`` silently drops those events, anomaly 2 of ``M3-ESITO.md``);
* :func:`parse_pdc_config` and the configuration models (all entity ids come from the
  configuration, none is in the code);
* :func:`build_pdc_diagnostics`: the per-room diagnostic attributes (``CONTRATTI.md`` section 4).

No Home Assistant import, no I/O, no global clock, no asyncio. ``now`` is an
explicit absolute Unix time.

Conventions: a missing entity, ``unknown`` and ``unavailable`` are *never* turned
into ``off``, zero or a closed window. Raw state strings are kept as read;
numbers that are not finite are treated as missing (hardening already used by the
policy snapshot).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final

from .heating_pdc_policy import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    SPLIT_ACTIVE_MODES,
    PdcCycle,
    PdcDecision,
    PdcPolicyConfig,
    PdcSnapshot,
    automatic_ok,
    keep_running_ok,
    parse_pdc_cycle,
    radiator_stopped,
    recheck_after_radiator_stop,
    recheck_after_setpoint,
    split_setpoint,
    window_running_ok,
)
from .heating_pdc_provenance import DeviceState, OverrideState
from .heating_pdc_transaction import (
    PHASE_WAITING_HEAT,
    PHASE_WAITING_RADIATOR,
    PHASE_WAITING_TARGET,
    PdcObservation,
    PdcReservation,
    PdcTransaction,
    PdcTransactionConfig,
)

SCHEMA_VERSION: Final = 1

MODE_DISABLED: Final = "disabled"
MODE_OBSERVE: Final = "observe"
MODE_CONTROL: Final = "control"
KNOWN_MODES: Final = (MODE_DISABLED, MODE_OBSERVE, MODE_CONTROL)

OWNER_OBSERVER: Final = "observer"
OWNER_CONTROLLER: Final = "controller"
OWNER_NONE: Final = None

# Sensor states that are not a decision reason.
STATE_DISABLED: Final = "disabled"
STATE_STARTING: Final = "starting"
STATE_ERROR: Final = "error"
STATE_SUSPENDED: Final = "suspended"
STATE_BLOCKED: Final = "blocked"

# Configuration keys.
KEY_MODE: Final = "mode"
KEY_POWER: Final = "power_entity_id"
KEY_CENTRAL_MODE: Final = "central_mode_entity_id"
KEY_ROOMS: Final = "rooms"
KEY_TRANSACTION: Final = "transaction"
KEY_ROOM_KEY: Final = "room_key"
KEY_RADIATOR: Final = "radiator_entity_id"
KEY_VALVE: Final = "radiator_valve_entity_id"
KEY_TEMPERATURE: Final = "temperature_entity_id"
KEY_WINDOW: Final = "window_entity_id"
KEY_SPLIT: Final = "split_entity_id"
KEY_PDC: Final = "pdc_entity_id"
KEY_AUTOMATIC: Final = "automatic_entity_id"
KEY_SCHEDULE_STOP: Final = "schedule_stop_entity_id"
KEY_OVERRIDE_TIMER: Final = "override_timer_entity_id"
KEY_SCHEDULE_SWITCHES: Final = "schedule_switch_entity_ids"
KEY_MANAGED: Final = "managed_entity_id"
KEY_OVERRIDE: Final = "override_entity_id"
KEY_CYCLE: Final = "cycle_entity_id"
KEY_EXTERNAL_OWNERS: Final = "external_owner_entity_ids"
KEY_SETPOINT_MIN: Final = "setpoint_min_c"
KEY_MANUAL_BYPASS: Final = "manual_preset_bypass"

ENTITY_ID_PATTERN: Final = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_)\.(?!_)[\da-z_]+(?<!_)$")
ROOM_KEY_PATTERN: Final = re.compile(r"^[a-z0-9_]{1,64}$")
MAX_ROOMS: Final = 8
MAX_EXTERNAL_OWNERS: Final = 32
MAX_ERROR_LENGTH: Final = 255
MAX_LIST_ITEMS: Final = 64

CLIMATE_DOMAINS: Final = ("climate",)
SENSOR_DOMAINS: Final = ("sensor", "input_number", "number")
BINARY_DOMAINS: Final = ("binary_sensor", "input_boolean", "switch")
CENTRAL_MODE_DOMAINS: Final = ("select", "input_select")
TIMER_DOMAINS: Final = ("timer",)
TEXT_DOMAINS: Final = ("input_text",)
SCHEDULE_DOMAINS: Final = ("sensor",)
OWNER_DOMAINS: Final = ("automation", "script")

# Configuration error codes (stable, untranslated).
ERR_INVALID_SECTION: Final = "invalid_section"
ERR_UNSUPPORTED_MODE: Final = "unsupported_mode"
ERR_REQUIRED: Final = "required"
ERR_INVALID_ENTITY: Final = "invalid_entity"
ERR_INVALID_DOMAIN: Final = "invalid_domain"
ERR_INVALID_ROOM_KEY: Final = "invalid_room_key"
ERR_DUPLICATE_ROOM: Final = "duplicate_room"
ERR_DUPLICATE_BINDING: Final = "duplicate_binding"
ERR_TOO_MANY_ROOMS: Final = "too_many_rooms"
ERR_INVALID_VALUE: Final = "invalid_value"
ERR_EXTERNAL_OWNERS_REQUIRED: Final = "external_owners_required"
ERR_LEGACY_REQUIRED: Final = "legacy_helpers_required"
ERR_CONTROL_NOT_AVAILABLE: Final = "control_not_available"


class PdcConfigError(ValueError):
    """Invalid PDC configuration. ``code`` is stable, ``key`` names the offending key."""

    def __init__(self, code: str, key: str | None = None) -> None:
        super().__init__(code if key is None else f"{code}:{key}")
        self.code = code
        self.key = key


@dataclass(frozen=True, slots=True)
class PdcRoomConfig:
    """Entity bindings and policy parameters of one room served by a radiator and a PDC."""

    room_key: str
    radiator_entity_id: str
    radiator_valve_entity_id: str
    temperature_entity_id: str
    window_entity_id: str
    split_entity_id: str
    pdc_entity_id: str
    automatic_entity_id: str
    schedule_stop_entity_id: str
    override_timer_entity_id: str | None = None
    schedule_switch_entity_ids: tuple[str, ...] = ()
    managed_entity_id: str | None = None
    override_entity_id: str | None = None
    cycle_entity_id: str | None = None
    external_owner_entity_ids: tuple[str, ...] = ()
    external_owners_declared: bool = False
    policy: PdcPolicyConfig = field(default_factory=PdcPolicyConfig)

    def room_entity_ids(self) -> tuple[str, ...]:
        """Every entity the room's inputs depend on (without the shared ones)."""
        ids = [
            self.radiator_entity_id,
            self.radiator_valve_entity_id,
            self.temperature_entity_id,
            self.window_entity_id,
            self.split_entity_id,
            self.pdc_entity_id,
            self.automatic_entity_id,
            self.schedule_stop_entity_id,
            self.override_timer_entity_id,
            *self.schedule_switch_entity_ids,
            self.managed_entity_id,
            self.override_entity_id,
            self.cycle_entity_id,
        ]
        return tuple(entity_id for entity_id in ids if entity_id)


@dataclass(frozen=True, slots=True)
class PdcConfig:
    """Validated configuration of the PDC runtime."""

    mode: str
    power_entity_id: str
    central_mode_entity_id: str
    rooms: tuple[PdcRoomConfig, ...]
    transaction: PdcTransactionConfig = field(default_factory=PdcTransactionConfig)

    def room(self, room_key: str) -> PdcRoomConfig | None:
        """The room with this key."""
        return next((room for room in self.rooms if room.room_key == room_key), None)

    def others(self, room_key: str) -> tuple[PdcRoomConfig, ...]:
        """The other PDC rooms (the shared capacity and the mutual exclusion)."""
        return tuple(room for room in self.rooms if room.room_key != room_key)

    def configured_entity_ids(self) -> tuple[str, ...]:
        """Every entity the runtime reads (inputs, legacy helpers, interlock)."""
        ids: list[str] = [self.power_entity_id, self.central_mode_entity_id]
        for room in self.rooms:
            ids.extend(room.room_entity_ids())
            ids.extend(room.external_owner_entity_ids)
        return tuple(dict.fromkeys(ids))


def _entity(value: Any, key: str, domains: Sequence[str], *, required: bool) -> str | None:
    if value is None or value == "":
        if required:
            raise PdcConfigError(ERR_REQUIRED, key)
        return None
    if not isinstance(value, str) or not ENTITY_ID_PATTERN.match(value):
        raise PdcConfigError(ERR_INVALID_ENTITY, key)
    if value.split(".", 1)[0] not in domains:
        raise PdcConfigError(ERR_INVALID_DOMAIN, key)
    return value


def _entity_list(value: Any, key: str, domains: Sequence[str], limit: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise PdcConfigError(ERR_INVALID_SECTION, key)
    if len(value) > limit:
        raise PdcConfigError(ERR_INVALID_VALUE, key)
    items: list[str] = []
    for item in value:
        entity = _entity(item, key, domains, required=True)
        assert entity is not None
        if entity in items:
            raise PdcConfigError(ERR_DUPLICATE_BINDING, key)
        items.append(entity)
    return tuple(items)


def _number(value: Any, key: str, default: float, low: float, high: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not low <= float(value) <= high:
        raise PdcConfigError(ERR_INVALID_VALUE, key)
    return float(value)


def _parse_room(item: Any, mode: str) -> PdcRoomConfig:
    if not isinstance(item, Mapping):
        raise PdcConfigError(ERR_INVALID_SECTION, KEY_ROOMS)
    room_key = item.get(KEY_ROOM_KEY)
    if not isinstance(room_key, str) or not ROOM_KEY_PATTERN.match(room_key):
        raise PdcConfigError(ERR_INVALID_ROOM_KEY, KEY_ROOM_KEY)
    legacy = _entity(item.get(KEY_MANAGED), KEY_MANAGED, BINARY_DOMAINS, required=False)
    override = _entity(item.get(KEY_OVERRIDE), KEY_OVERRIDE, BINARY_DOMAINS, required=False)
    if mode == MODE_OBSERVE and (legacy is None or override is None):
        # observe compares with the running legacy automations: it reads their helpers
        raise PdcConfigError(ERR_LEGACY_REQUIRED, KEY_MANAGED if legacy is None else KEY_OVERRIDE)
    owners_declared = KEY_EXTERNAL_OWNERS in item and item.get(KEY_EXTERNAL_OWNERS) is not None
    owners = _entity_list(item.get(KEY_EXTERNAL_OWNERS), KEY_EXTERNAL_OWNERS, OWNER_DOMAINS, MAX_EXTERNAL_OWNERS)
    if mode == MODE_CONTROL and not owners_declared:
        # control is explicit: the list may be empty only if the operator declared it empty
        raise PdcConfigError(ERR_EXTERNAL_OWNERS_REQUIRED, KEY_EXTERNAL_OWNERS)
    policy = PdcPolicyConfig(
        setpoint_min_c=_number(item.get(KEY_SETPOINT_MIN), KEY_SETPOINT_MIN, 16.0, 5.0, 30.0),
        manual_preset_bypass=_bool(item.get(KEY_MANUAL_BYPASS), KEY_MANUAL_BYPASS),
    )
    return PdcRoomConfig(
        room_key=room_key,
        radiator_entity_id=_entity(item.get(KEY_RADIATOR), KEY_RADIATOR, CLIMATE_DOMAINS, required=True),  # type: ignore[arg-type]
        radiator_valve_entity_id=_entity(item.get(KEY_VALVE), KEY_VALVE, CLIMATE_DOMAINS, required=True),  # type: ignore[arg-type]
        temperature_entity_id=_entity(item.get(KEY_TEMPERATURE), KEY_TEMPERATURE, SENSOR_DOMAINS, required=True),  # type: ignore[arg-type]
        window_entity_id=_entity(item.get(KEY_WINDOW), KEY_WINDOW, BINARY_DOMAINS, required=True),  # type: ignore[arg-type]
        split_entity_id=_entity(item.get(KEY_SPLIT), KEY_SPLIT, CLIMATE_DOMAINS, required=True),  # type: ignore[arg-type]
        pdc_entity_id=_entity(item.get(KEY_PDC), KEY_PDC, CLIMATE_DOMAINS, required=True),  # type: ignore[arg-type]
        automatic_entity_id=_entity(item.get(KEY_AUTOMATIC), KEY_AUTOMATIC, BINARY_DOMAINS, required=True),  # type: ignore[arg-type]
        schedule_stop_entity_id=_entity(item.get(KEY_SCHEDULE_STOP), KEY_SCHEDULE_STOP, SCHEDULE_DOMAINS, required=True),  # type: ignore[arg-type]
        override_timer_entity_id=_entity(item.get(KEY_OVERRIDE_TIMER), KEY_OVERRIDE_TIMER, TIMER_DOMAINS, required=False),
        schedule_switch_entity_ids=_entity_list(item.get(KEY_SCHEDULE_SWITCHES), KEY_SCHEDULE_SWITCHES, ("switch",), 8),
        managed_entity_id=legacy,
        override_entity_id=override,
        cycle_entity_id=_entity(item.get(KEY_CYCLE), KEY_CYCLE, TEXT_DOMAINS, required=False),
        external_owner_entity_ids=owners,
        external_owners_declared=owners_declared,
        policy=policy,
    )


def _bool(value: Any, key: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise PdcConfigError(ERR_INVALID_VALUE, key)
    return value


def parse_pdc_config(section: Any, *, allow_control: bool = False) -> PdcConfig:
    """Validate the PDC section. Raises :class:`PdcConfigError`; never guesses a missing binding.

    ``control`` is refused (``control_not_available``) unless ``allow_control`` is set: a caller that is
    not ready to command the plant never gets a control implicitly. A ``disabled`` section is accepted
    whatever else it holds (kept for a later activation, never read).
    """
    if not isinstance(section, Mapping):
        raise PdcConfigError(ERR_INVALID_SECTION)
    mode = section.get(KEY_MODE, MODE_DISABLED)
    if not isinstance(mode, str) or mode not in KNOWN_MODES:
        raise PdcConfigError(ERR_UNSUPPORTED_MODE, KEY_MODE)
    if mode == MODE_CONTROL and not allow_control:
        raise PdcConfigError(ERR_CONTROL_NOT_AVAILABLE, KEY_MODE)
    raw_rooms = section.get(KEY_ROOMS)
    if mode == MODE_DISABLED:
        return PdcConfig(MODE_DISABLED, "sensor.unconfigured", "select.unconfigured", ())
    if isinstance(raw_rooms, (str, bytes, Mapping)) or not isinstance(raw_rooms, (list, tuple)) or not raw_rooms:
        raise PdcConfigError(ERR_REQUIRED, KEY_ROOMS)
    if len(raw_rooms) > MAX_ROOMS:
        raise PdcConfigError(ERR_TOO_MANY_ROOMS, KEY_ROOMS)
    rooms = tuple(_parse_room(item, mode) for item in raw_rooms)
    if len({room.room_key for room in rooms}) != len(rooms):
        raise PdcConfigError(ERR_DUPLICATE_ROOM, KEY_ROOM_KEY)
    exclusive = [room.split_entity_id for room in rooms] + [room.pdc_entity_id for room in rooms] + [room.radiator_entity_id for room in rooms]
    if len(set(exclusive)) != len(exclusive):
        raise PdcConfigError(ERR_DUPLICATE_BINDING, KEY_SPLIT)
    raw_tx = section.get(KEY_TRANSACTION) or {}
    if not isinstance(raw_tx, Mapping):
        raise PdcConfigError(ERR_INVALID_SECTION, KEY_TRANSACTION)
    tx_defaults = PdcTransactionConfig()
    try:
        transaction = PdcTransactionConfig(
            **{
                name: _number(raw_tx.get(name), f"{KEY_TRANSACTION}.{name}", getattr(tx_defaults, name), 0.0, 3600.0)
                for name in (
                    "radiator_timeout_s",
                    "target_timeout_s",
                    "heat_timeout_s",
                    "off_timeout_s",
                    "direct_off_timeout_s",
                    "heat_settle_s",
                    "release_retry_s",
                    "return_retry_s",
                )
            }
        )
    except (TypeError, ValueError) as err:
        raise PdcConfigError(ERR_INVALID_VALUE, KEY_TRANSACTION) from err
    return PdcConfig(
        mode=mode,
        power_entity_id=_entity(section.get(KEY_POWER), KEY_POWER, SENSOR_DOMAINS, required=True),  # type: ignore[arg-type]
        central_mode_entity_id=_entity(section.get(KEY_CENTRAL_MODE), KEY_CENTRAL_MODE, CENTRAL_MODE_DOMAINS, required=True),  # type: ignore[arg-type]
        rooms=rooms,
        transaction=transaction,
    )


# ---------------------------------------------------------------------------
# State access with the semantics of the Home Assistant template helpers.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StateView:
    """One entity as read: raw state string (``None`` when the entity does not exist), attributes and instants."""

    state: str | None
    attributes: Mapping[str, Any]
    changed_at: float | None
    reported_at: float | None


_MISSING: Final = StateView(None, {}, None, None)


def to_timestamp(value: Any) -> float | None:
    """A datetime, a number or an ISO string as Unix seconds; anything else is missing."""
    if isinstance(value, datetime):
        stamp = (value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)).timestamp()
    elif isinstance(value, bool):
        return None
    elif isinstance(value, (int, float)):
        stamp = float(value)
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        stamp = (parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)).timestamp()
    else:
        return None
    return stamp if math.isfinite(stamp) else None


def lookup(states: Mapping[str, Any], entity_id: str | None) -> StateView:
    """Read ``entity_id``; state objects or ``{"state", "attributes", "last_changed", "last_reported"}`` dictionaries."""
    if not entity_id:
        return _MISSING
    try:
        value = states.get(entity_id)
    except (TypeError, KeyError):
        return _MISSING
    if value is None:
        return _MISSING
    if isinstance(value, Mapping):
        state, attributes = value.get("state"), value.get("attributes")
        changed, reported = value.get("last_changed"), value.get("last_reported")
    else:
        state, attributes = getattr(value, "state", None), getattr(value, "attributes", None)
        changed, reported = getattr(value, "last_changed", None), getattr(value, "last_reported", None)
    if not isinstance(attributes, Mapping):
        attributes = {}
    return StateView(None if state is None else str(state), attributes, to_timestamp(changed), to_timestamp(reported))


def as_number(value: Any) -> float | None:
    """``value | float(none)`` with the hardening of the policy: booleans and non finite numbers are missing."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _state_number(view: StateView) -> float | None:
    return None if view.state in (None, STATE_UNKNOWN, STATE_UNAVAILABLE) else as_number(view.state)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def read_inhibition(attributes: Mapping[str, Any]) -> tuple[bool | None, str | None]:
    """``(inhibited, last_transition_at)`` of a VTherm.

    The inhibition section is published only once the inhibition was used: a VTherm
    that publishes ``specific_states`` without it was never inhibited (``False``).
    A VTherm that publishes no ``specific_states`` at all, or a malformed section,
    is unreadable (``None``): that never counts as "not inhibited".
    """
    specific = attributes.get("specific_states")
    if not isinstance(specific, Mapping):
        return None, None
    section = specific.get("inhibition")
    if section is None:
        return False, None
    if not isinstance(section, Mapping):
        return None, None
    inhibited = section.get("inhibited")
    return (inhibited if isinstance(inhibited, bool) else None), _text(section.get("last_transition_at"))


def read_requested_hvac_mode(attributes: Mapping[str, Any]) -> str | None:
    """``(state_attr(vtherm, 'requested_state') or {}).get('hvac_mode')``."""
    requested = attributes.get("requested_state")
    if not isinstance(requested, Mapping):
        return None
    mode = requested.get("hvac_mode")
    return None if mode is None else str(mode)


@dataclass(frozen=True, slots=True)
class PdcLocalInputs:
    """Values the runtime owns in ``control``: ``None`` means "read the legacy helper instead" (``observe``)."""

    override: str | None = None
    managed: str | None = None
    managed_changed_at: float | None = None
    cycle: PdcCycle | None = None


@dataclass(frozen=True, slots=True)
class PdcOtherView:
    """What the mutual exclusion needs to know about the other PDC rooms (aggregated)."""

    managed: str | None
    split_mode: str | None


def aggregate_others(views: Sequence[PdcOtherView]) -> PdcOtherView:
    """Fold the other rooms into the two snapshot fields.

    ``managed`` is ``on`` if any is on, ``off`` only if all are confirmed off, else
    the first value that is not ``off`` (unknown stays unknown). ``split_mode`` is
    the first active mode, ``off`` only if all are confirmed off, else the first
    non-off value. With no other room the PDC is free.
    """
    if not views:
        return PdcOtherView(STATE_OFF, STATE_OFF)

    def fold(values: Sequence[str | None], active: Sequence[str]) -> str | None:
        for value in values:
            if value in active:
                return value
        if all(value == STATE_OFF for value in values):
            return STATE_OFF
        return next((value for value in values if value != STATE_OFF), None)

    return PdcOtherView(fold([v.managed for v in views], (STATE_ON,)), fold([v.split_mode for v in views], SPLIT_ACTIVE_MODES))


def read_other_view(states: Mapping[str, Any], room: PdcRoomConfig, local_managed: str | None = None) -> PdcOtherView:
    """The managed flag (``local_managed`` when the runtime owns it, else the legacy helper) and the split of one room."""
    managed = local_managed if local_managed is not None else lookup(states, room.managed_entity_id).state
    return PdcOtherView(managed, lookup(states, room.split_entity_id).state)


@dataclass(frozen=True, slots=True)
class PdcCollection:
    """One coherent read of a room."""

    snapshot: PdcSnapshot
    split_action: str | None
    inhibition_marker: str | None
    missing: tuple[str, ...]
    split_device: DeviceState | None
    split_changed_at: float | None


def collect_pdc_snapshot(
    states: Mapping[str, Any],
    room: PdcRoomConfig,
    config: PdcConfig,
    *,
    local: PdcLocalInputs | None = None,
    other: PdcOtherView | None = None,
) -> PdcCollection:
    """Build the snapshot of one room from a single read of ``states``.

    Entities read here: the baseline variables of the chooser. ``local`` replaces
    the legacy helpers (override, in_gestione, ciclo) when the runtime owns them.
    ``other`` is the already aggregated state of the other PDC rooms.
    """
    local = local or PdcLocalInputs()
    radiator = lookup(states, room.radiator_entity_id)
    valve = lookup(states, room.radiator_valve_entity_id)
    temperature = lookup(states, room.temperature_entity_id)
    window = lookup(states, room.window_entity_id)
    power = lookup(states, config.power_entity_id)
    split = lookup(states, room.split_entity_id)
    pdc = lookup(states, room.pdc_entity_id)
    central = lookup(states, config.central_mode_entity_id)
    automatic = lookup(states, room.automatic_entity_id)
    schedule = lookup(states, room.schedule_stop_entity_id)
    timer = lookup(states, room.override_timer_entity_id)
    override = lookup(states, room.override_entity_id)
    managed = lookup(states, room.managed_entity_id)
    cycle_view = lookup(states, room.cycle_entity_id)
    if other is None:
        other = aggregate_others([read_other_view(states, o) for o in config.others(room.room_key)])

    if local.cycle is not None:
        cycle, cycle_available = local.cycle, True
    else:
        cycle_available = cycle_view.state not in (None, STATE_UNKNOWN, STATE_UNAVAILABLE)
        cycle = _decode_cycle(cycle_view.state) if cycle_available else PdcCycle()

    inhibited, marker = read_inhibition(radiator.attributes)
    snapshot = PdcSnapshot(
        override=local.override if local.override is not None else override.state,
        managed=local.managed if local.managed is not None else managed.state,
        managed_changed_at=local.managed_changed_at if local.managed is not None else managed.changed_at,
        automatic=automatic.state,
        central_mode=central.state,
        radiator_mode=radiator.state,
        requested_hvac_mode=read_requested_hvac_mode(radiator.attributes),
        preset_mode=_text(radiator.attributes.get("preset_mode")),
        temperature=_state_number(temperature),
        temperature_reported_at=temperature.reported_at,
        target=as_number(radiator.attributes.get("temperature")),
        window=window.state,
        window_changed_at=window.changed_at,
        power=_state_number(power),
        power_reported_at=power.reported_at,
        split_mode=split.state,
        split_changed_at=split.changed_at,
        pdc_mode=pdc.state,
        pdc_setpoint=as_number(pdc.attributes.get("temperature")),
        other_managed=other.managed,
        other_split_mode=other.split_mode,
        schedule_stop=schedule.state,
        cycle=cycle,
        cycle_available=cycle_available,
        heating_override_timer=timer.state,
        schedule_switches=tuple(lookup(states, switch).state for switch in room.schedule_switch_entity_ids),
        radiator_valve_mode=valve.state,
        radiator_inhibited=inhibited,
    )
    missing = tuple(entity_id for entity_id in room.room_entity_ids() + (config.power_entity_id, config.central_mode_entity_id) if lookup(states, entity_id).state is None)
    return PdcCollection(
        snapshot=snapshot,
        split_action=_text(split.attributes.get("hvac_action")),
        inhibition_marker=marker,
        missing=missing,
        split_device=device_state(split),
        split_changed_at=split.changed_at,
    )


def _decode_cycle(text: str | None) -> PdcCycle:
    """``states(helper) | from_json({})`` followed by ``parse_pdc_cycle`` (a non mapping is an empty cycle)."""

    def reject(constant: str) -> Any:
        raise ValueError(constant)

    try:
        return parse_pdc_cycle(json.loads(text or "", parse_constant=reject))
    except ValueError:
        return PdcCycle()


def device_state(view: StateView) -> DeviceState | None:
    """The split as the provenance model sees it (``None`` when the entity is missing)."""
    if view.state is None:
        return None
    attributes = view.attributes
    return DeviceState(
        state=view.state,
        temperature=as_number(attributes.get("temperature")),
        fan_mode=_text(attributes.get("fan_mode")),
        preset_mode=_text(attributes.get("preset_mode")),
        swing_mode=_text(attributes.get("swing_mode")),
    )


# ---------------------------------------------------------------------------
# Observation of a transaction and protections during a transaction.
# ---------------------------------------------------------------------------


def radiator_stop_reading(snapshot: PdcSnapshot) -> bool | None:
    """``True`` stopped, ``False`` explicitly not stopped, ``None`` when a reading is missing."""
    if radiator_stopped(snapshot):
        return True
    readings = (snapshot.radiator_valve_mode, snapshot.radiator_mode)
    if snapshot.radiator_inhibited is None or any(reading in (None, STATE_UNKNOWN, STATE_UNAVAILABLE) for reading in readings):
        return None
    return False


def build_observation(
    token: int,
    snapshot: PdcSnapshot,
    now: float,
    config: PdcPolicyConfig,
    *,
    phase: str,
    override_off: bool | None,
) -> PdcObservation:
    """The facts the machine needs, with the baseline re-check of ``phase`` computed by the policy predicates."""
    if phase == PHASE_WAITING_RADIATOR:
        conditions: bool | None = recheck_after_radiator_stop(snapshot, now, config)
    elif phase == PHASE_WAITING_TARGET:
        conditions = recheck_after_setpoint(snapshot, now, config)
    elif phase == PHASE_WAITING_HEAT:
        conditions = keep_running_ok(snapshot, now, config)
    else:
        conditions = None
    setpoint = split_setpoint(snapshot.target, config)
    target_confirmed = None if setpoint is None or snapshot.pdc_setpoint is None else snapshot.pdc_setpoint == setpoint
    return PdcObservation(
        token=token,
        override_off=override_off,
        split_mode=snapshot.split_mode,
        pdc_mode=snapshot.pdc_mode,
        radiator_stopped=radiator_stop_reading(snapshot),
        target_confirmed=target_confirmed,
        conditions_ok=conditions,
        radiator_inhibited=snapshot.radiator_inhibited,
    )


def unsafe_during_start(snapshot: PdcSnapshot, now: float, config: PdcPolicyConfig, *, phase: str) -> bool:
    """A protection that must interrupt a start sequence without waiting for its next checkpoint.

    The baseline drops events that arrive during the sequence (``mode: single``,
    ``max_exceeded: silent``). Here the sequence is event driven, so the hard
    protections are evaluated on every event: missing, stale or excessive power
    (stop threshold), central mode, automatic mode and program no longer
    compatible, and the window. Only conditions that the baseline itself treats as
    a reason to return at its checkpoints are used (never the start-only gap or
    capacity reserve, which the PDC's own consumption would trip).
    """
    power = snapshot.power
    if power is None or power >= config.stop_power_w or (now - (snapshot.power_reported_at or 0.0)) > config.power_max_age_s:
        return True
    if not automatic_ok(snapshot, config):
        return True
    if phase in (PHASE_WAITING_RADIATOR, PHASE_WAITING_TARGET):
        return snapshot.window != STATE_OFF
    return not window_running_ok(snapshot, now, config)


def radiator_released_while_running(snapshot: PdcSnapshot, tx: PdcTransaction) -> bool:
    """The VTherm explicitly reports the radiator not inhibited while this transaction owns the inhibition (a second heat source)."""
    return tx.inhibition_owned and snapshot.radiator_inhibited is False


# ---------------------------------------------------------------------------
# Diagnostics.
# ---------------------------------------------------------------------------


def json_safe(value: Any, *, depth: int = 0) -> Any:
    """A JSON value without NaN/Infinity, tuples or unbounded nesting."""
    if depth > 6:
        return None
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): json_safe(item, depth=depth + 1) for key, item in list(value.items())[:MAX_LIST_ITEMS]}
    if isinstance(value, (list, tuple)):
        return [json_safe(item, depth=depth + 1) for item in list(value)[:MAX_LIST_ITEMS]]
    return str(value)


def truncate_error(value: str | None) -> str | None:
    """Keep ``last_error`` short and free of secrets-sized payloads."""
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= MAX_ERROR_LENGTH else text[: MAX_ERROR_LENGTH - 3] + "..."


def condition_to_dict(condition: Any) -> dict[str, Any]:
    """Serialize a policy condition (shape of ``CONTRATTI.md`` section 2)."""
    return {
        "id": condition.id,
        "status": condition.status,
        "reason_code": condition.reason_code,
        "actual": json_safe(condition.actual),
        "expected": json_safe(condition.expected),
        "deadline": json_safe(condition.deadline),
        "unit": condition.unit,
    }


def proposed_action(decision: PdcDecision | None) -> str:
    """Policy action mapped to the contract vocabulary ``none``/``heat``/``off`` (a setpoint change is not a mode change)."""
    if decision is None:
        return "none"
    return {"start": "heat", "stop": "off"}.get(decision.action, "none")


def empty_pdc_diagnostics(
    room_key: str,
    mode: str,
    *,
    owner: str | None = None,
    evaluated_at: float | None = None,
    last_error: str | None = None,
    generation: int = 0,
    suspension: str | None = None,
) -> dict[str, Any]:
    """Attributes for ``disabled``, ``starting``, ``blocked`` and ``error`` states (no decision)."""
    return json_safe(
        {
            "schema_version": SCHEMA_VERSION,
            "room_key": room_key,
            "mode": mode,
            "owner": owner,
            "evaluated_at": evaluated_at,
            "generation": generation,
            "proposed_action": "none",
            "reason": None,
            "conditions": [],
            "power_limit_w": None,
            "stability_s": None,
            "transaction": {"id": None, "phase": "idle", "started_at": None, "deadline": None},
            "reservation": {"holder": None},
            "override": {"active": False, "until": None, "source": None, "provenance": "none"},
            "suspension": suspension,
            "last_error": truncate_error(last_error),
        }
    )


def build_pdc_diagnostics(
    *,
    room_key: str,
    mode: str,
    owner: str | None,
    decision: PdcDecision | None,
    snapshot: PdcSnapshot | None,
    tx: PdcTransaction | None,
    reservation: PdcReservation | None,
    override: OverrideState | None,
    policy: PdcPolicyConfig,
    evaluated_at: float,
    generation: int,
    last_error: str | None = None,
    suspension: str | None = None,
    blocked_by: Sequence[str] = (),
    split_action: str | None = None,
    reason: str | None = None,
    release_held: bool = False,
) -> dict[str, Any]:
    """The per-room sensor attributes (``CONTRATTI.md`` section 4, schema v1).

    ``transaction.id`` is the token (generation) of the hand-over; ``reservation`` is the shared holder
    (room key or ``None``). ``split_action`` is the ``hvac_action`` the split reports: it is shown, never
    used to decide (HEAT of the split is not the activity of the compressor).
    """
    attributes: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "room_key": room_key,
        "mode": mode,
        "owner": owner,
        "evaluated_at": evaluated_at,
        "generation": generation,
        "proposed_action": proposed_action(decision),
        "reason": reason if reason is not None else (decision.reason if decision is not None else None),
        "conditions": [condition_to_dict(c) for c in decision.conditions] if decision is not None else [],
        "diagnostic_state": decision.diagnostic_state if decision is not None else None,
        "power_limit_w": policy.diagnostic_power_limit_w,
        "stability_s": policy.stability_s,
        "setpoint": decision.setpoint if decision is not None else None,
        "stop_reason": decision.stop_reason if decision is not None else None,
        "deadline": decision.deadline if decision is not None else None,
        "transaction": {
            "id": tx.token if tx is not None and tx.token else None,
            "phase": tx.phase if tx is not None else "idle",
            "started_at": tx.started_at if tx is not None else None,
            "deadline": tx.deadline if tx is not None else None,
            "failure": tx.failure if tx is not None else None,
            "manual": tx.manual if tx is not None else False,
            "inhibition_owned": tx.inhibition_owned if tx is not None else False,
            "inhibited_before": tx.inhibited_before if tx is not None else None,
            "release_attempts": tx.release_attempts if tx is not None else 0,
            "release_held": release_held,
        },
        "managed": tx is not None and tx.phase != "idle",
        "reservation": {"holder": reservation.holder if reservation is not None else None},
        "override": {
            "active": override.active if override is not None else False,
            "until": override.until if override is not None else None,
            "source": override.source if override is not None else None,
            "provenance": override.provenance if override is not None else "none",
        },
        "suspension": suspension,
        "blocked_by": list(blocked_by),
        "split_mode": snapshot.split_mode if snapshot is not None else None,
        "pdc_mode": snapshot.pdc_mode if snapshot is not None else None,
        "split_action": split_action,
        "last_error": truncate_error(last_error),
    }
    return json_safe(attributes)
