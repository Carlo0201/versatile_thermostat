"""Per-room heating status and start conditions (pure, read-only projection).

``evaluate_room_status`` answers, for one configured room: is it heating, why,
which start conditions pass, in which stage the start chain is blocked. It is a
read-only *projection* of data the boiler collector and M1 already computed in
the same round; it sends nothing, owns nothing and never changes what the
collector or M1 publish. The contract is ``ROOM-CONDITIONS-CONTRATTO.md``.

Like M1 and the PDC engines this module imports no Home Assistant code, does no
I/O and takes the clock explicitly. Qualification rules (window, inhibition,
safety, power, temperature, PDC release, demand, valves) are **not**
reimplemented: they come from the exclusion codes of the collector. The only
rules written here are those the collector does not have (central mode row,
window tolerance, demand fallback, controller row) and the explanation of the
state (the port of the Oikos ``explainRoom``).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import tzinfo, timezone
from typing import Any, Final

from .heating_boiler_collector import (
    CENTRAL_MODES_ALLOWING_HEAT,
    EXCLUSION_AVATTO_NO_DEMAND,
    EXCLUSION_COUNT_INVALID,
    EXCLUSION_ENTITY_UNAVAILABLE,
    EXCLUSION_HVAC_NOT_HEATING,
    EXCLUSION_INHIBITED,
    EXCLUSION_NO_ACTIVE_VALVE,
    EXCLUSION_NOT_BOILER_PARTICIPANT,
    EXCLUSION_NOT_INITIALIZED,
    EXCLUSION_NOT_READY,
    EXCLUSION_OVERPOWERING,
    EXCLUSION_PDC_SESSION_ACTIVE,
    EXCLUSION_PDC_SESSION_UNKNOWN,
    EXCLUSION_SAFETY,
    EXCLUSION_SPLIT_NOT_OFF,
    EXCLUSION_SPLIT_UNKNOWN,
    EXCLUSION_TEMPERATURE_FUTURE,
    EXCLUSION_TEMPERATURE_MISSING,
    EXCLUSION_TEMPERATURE_STALE,
    EXCLUSION_UNIT_UNSUPPORTED,
    EXCLUSION_WINDOW_OPEN,
    EXCLUSION_WINDOW_UNKNOWN,
    MAX_TEMPERATURE_AGE_S,
    BoilerCollection,
    RoomBinding,
    _as_timestamp,
    _count_validity,
    _is_jinja_sequence,
    _underlying_ids,
    active_valves,
    _jinja_float,
    _lookup,
    _section,
    condition_to_dict,
    json_safe,
)
from .heating_boiler_policy import (
    BOILER_REASON_AWAITING_CONSENT,
    BOILER_REASON_NORMAL_WAIT,
    BOILER_REASON_SINGLE_WAIT,
    CONDITION_FAIL,
    CONDITION_NA,
    CONDITION_PASS,
    CONDITION_UNKNOWN,
    CONDITION_WAIT,
    DEFAULT_THERMAL_GAP_C,
    BoilerDecision,
)

SCHEMA_VERSION: Final = 1
MAX_ATTRIBUTE_BYTES: Final = 8192

# Statuses beyond the M1 ones.
CONDITION_OBSERVE: Final = "observe"

STAGE_READY: Final = "ready"
STAGE_DEMAND: Final = "demand"
STAGE_BOILER: Final = "boiler"
STAGE_PUMP: Final = "pump"
STAGE_ORDER: Final = (STAGE_READY, STAGE_DEMAND, STAGE_BOILER, STAGE_PUMP)
CHAIN_STAGES: Final = (STAGE_READY, STAGE_DEMAND, STAGE_BOILER)

STATE_DISABLED: Final = "disabled"
STATE_STARTING: Final = "starting"
STATE_ERROR: Final = "error"

SOURCE_RADIATOR: Final = "radiator"
SOURCE_PDC: Final = "pdc"
SOURCE_NONE: Final = "none"

HEAT_STATES: Final = (
    "pump_transition",
    "pump_heating",
    "pump_selected",
    "radiator_heating",
    "boiler_waiting",
    "heat_waiting",
    "continuing_heat",
    "at_target",
    "below_start_threshold",
    "regulating",
    "window",
    "central_off",
    "safety",
    "power",
    "auto_pause",
    "off",
    "other_mode",
    "unavailable",
    "initializing",
    "unknown",
)

_ON_VALUES: Final = ("on", "open", "opening", "true", "1")
_UNAVAILABLE: Final = ("unavailable", "unknown")
_ACTIONS_KNOWN: Final = ("heating", "idle", "off", "cooling", "drying", "fan")
_HEAT_MODES: Final = ("heat", "auto", "heat_cool")
_HVAC_ENABLED_MODES: Final = ("heat", "auto")
_ROOM_WIDE_STATES: Final = ("window", "central_off", "safety", "power", "auto_pause")
_NO_PUMP_EXPLANATION_STATES: Final = ("window", "central_off", "safety", "power", "auto_pause", "off", "unavailable", "initializing", "other_mode")
_ROUTINE_PUMP_REASONS: Final = ("ready", "managed", "room_off", "at_target", "small_gap")
_PUMP_PHASES: Final = {
    "waiting_radiator": "waiting_trv",
    "waiting_target": "waiting_target",
    "waiting_heat": "waiting_split",
    "returning": "returning_radiator",
    "returning_direct": "returning_radiator",
    "releasing": "returning_radiator",
    "return_unconfirmed": "return_unconfirmed",
}
_PUMP_FAILURES: Final = ("trv_unconfirmed", "target_unconfirmed", "split_unconfirmed", "conditions_changed", "return_unconfirmed")
_PUBLISH_FRESH_MIN_S: Final = -2.0
_PUBLISH_FRESH_MAX_S: Final = 45.0
_SNAKE_RE: Final = re.compile(r"(?<!^)(?=[A-Z])")


def _snake(value: str) -> str:
    return _SNAKE_RE.sub("_", value).lower()


# ---------------------------------------------------------------------------
# Models.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoomCondition:
    """One start condition (CONTRATTI section 2 shape plus ``stage``)."""

    id: str
    stage: str
    status: str
    reason_code: str | None = None
    actual: Any = None
    expected: Any = None
    deadline: float | None = None
    unit: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "stage": self.stage,
            "status": self.status,
            "reason_code": self.reason_code,
            "actual": json_safe(self.actual),
            "expected": json_safe(self.expected),
            "deadline": json_safe(self.deadline),
            "unit": self.unit,
        }


@dataclass(frozen=True, slots=True)
class RoomStage:
    key: str
    status: str
    passed: int
    total: int

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "status": self.status, "passed": self.passed, "total": self.total}


@dataclass(frozen=True, slots=True)
class RoomBlocker:
    id: str
    stage: str
    status: str
    reason_code: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "stage": self.stage, "status": self.status, "reason_code": self.reason_code}


@dataclass(frozen=True, slots=True)
class PdcView:
    """Last publication of the PDC manager for the room (state and attributes), read only."""

    state: str | None
    attributes: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ControllerView:
    """Orchestration state and ownership as published by the boiler manager."""

    mode: str
    owner: str | None = None
    state: str | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class RoomStatus:
    """Result of one room evaluation."""

    room_key: str
    climate_entity_id: str
    heat_state: str
    reason: str
    detail_reasons: tuple[str, ...]
    source: str
    current: float | None
    target: float | None
    gap: float | None
    start_gap: float
    deadline: float | None
    stages: tuple[RoomStage, ...]
    blocker: RoomBlocker | None
    conditions: tuple[RoomCondition, ...]
    pump: Mapping[str, Any] | None
    last_error: str | None = None


# ---------------------------------------------------------------------------
# Small readers (the VTherm attributes read here only explain the state).
# ---------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    """``toNumber``: a finite number or ``None`` (booleans are not numbers)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _is_on(value: Any) -> bool:
    return value is True or str(value).lower() in _ON_VALUES


def _available(state: Any) -> bool:
    return bool(state) and state not in _UNAVAILABLE


def _command_state(value: Any) -> bool | None:
    """``commandState``: ``True`` boiler on, ``False`` off, ``None`` unknown."""
    if not _available(value):
        return None
    text = str(value).lower()
    if text in ("on", "heat", "heating", "active", "true", "1"):
        return True
    if text in ("off", "idle", "false", "0", "standby"):
        return False
    return None


def _first(*values: Any) -> Any:
    """JS ``??`` chain."""
    for value in values:
        if value is not None:
            return value
    return None


def _vtherm_window(states: Mapping[str, Any], attributes: Mapping[str, Any]) -> dict[str, bool]:
    """Window as VTherm reports it (``vthermWindow``): raw sensor versus confirmed state."""
    manager = _section(attributes, "window_manager")
    sensor = _first(manager.get("window_sensor_entity_id") or None, attributes.get("window_sensor_entity_id") or None)
    raw_view = _lookup(states, sensor) if isinstance(sensor, str) else None
    raw = None if raw_view is None else raw_view.state
    reported = _first(manager.get("window_state"), attributes.get("window_state"), attributes.get("window_auto_state"))
    reported_known = reported in ("on", "off", True, False)
    raw_known = raw in ("on", "off", "open", "closed")
    known = raw_known if isinstance(sensor, str) and sensor else reported_known
    is_open = _is_on(raw) if raw_known else _is_on(reported)
    bypass = manager.get("is_window_bypass") is True or attributes.get("is_window_bypass") is True
    confirmed = _is_on(reported) if reported_known else is_open
    return {
        "open": is_open,
        "known": known,
        "pending": is_open and reported_known and not confirmed and not bypass,
        "bypass": bypass,
        "locked": confirmed and not bypass,
    }


@dataclass(frozen=True, slots=True)
class _Reading:
    """Port of ``roomHeatingStatus`` (what the thermostat reports about itself)."""

    state: Any
    available: bool
    action: Any
    current: float | None
    target: float | None
    below_target: bool | None
    initialized: bool
    heating: bool
    activity_known: bool
    participates: bool | None
    underlying: tuple[str, ...]
    window: Mapping[str, bool]
    window_locked_status: bool
    code: str


def _read_room(states: Mapping[str, Any], climate_entity_id: str, start_gap: float) -> _Reading:
    view = _lookup(states, climate_entity_id)
    state = None if view is None else view.state
    attributes: Mapping[str, Any] = {} if view is None else view.attributes
    available = _available(state)
    action = attributes.get("hvac_action")
    current = _number(attributes.get("current_temperature"))
    target = _number(_first(attributes.get("temperature"), attributes.get("target_temp")))
    below_target = None if current is None or target is None else (target - current) > start_gap + 1e-9
    specific = _section(attributes, "specific_states")
    configuration = _section(attributes, "configuration")
    window = _vtherm_window(states, attributes)
    manager = _section(attributes, "window_manager")
    off_reason = _first(specific.get("hvac_off_reason"), attributes.get("hvac_off_reason"))
    window_suspended = (off_reason == "hvac_off_window_detection" and state == "off") or (
        window["locked"] and (not manager.get("window_action") or manager.get("window_action") == "window_turn_off")
    )
    safety = _is_on(_section(attributes, "safety_manager").get("safety_state"))
    power = _is_on(_section(attributes, "power_manager").get("overpowering_state"))
    initialized = attributes.get("is_ready") is not False and specific.get("is_initialized") is not False
    enabled = available and state != "off"
    action_known = action in _ACTIONS_KNOWN
    heating = bool(enabled and initialized and not window_suspended and (action == "heating" or (not action_known and specific.get("is_device_active") is True)))
    activity_known = available and (action_known or isinstance(specific.get("is_device_active"), bool))
    eligibility = _first(configuration.get("is_used_by_central_boiler"), attributes.get("is_used_by_central_boiler"))
    participates = eligibility if isinstance(eligibility, bool) else None
    underlying_raw = _section(attributes, "vtherm_over_climate").get("underlying_entities")
    underlying = tuple(dict.fromkeys(item for item in underlying_raw if isinstance(item, str) and item)) if isinstance(underlying_raw, (list, tuple)) else ()
    if not available:
        code = "unavailable"
    elif not initialized:
        code = "initializing"
    elif window_suspended:
        code = "window"
    elif safety:
        code = "safety"
    elif power:
        code = "power"
    elif state == "off":
        code = "central_off" if off_reason == "hvac_off_central_mode" else "auto_pause" if off_reason == "hvac_off_auto_start_stop" else "off"
    elif heating:
        code = "heating"
    elif state not in _HEAT_MODES:
        code = "other_mode"
    elif not activity_known:
        code = "unknown"
    elif below_target is True:
        code = "below_target"
    elif below_target is False:
        code = "idle"
    else:
        code = "unknown"
    return _Reading(state, available, action, current, target, below_target, initialized, heating, activity_known, participates, underlying, window, window_suspended, code)


# ---------------------------------------------------------------------------
# PDC view (data forwarded from the PDC manager, never recomputed).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PumpReading:
    available: bool
    owned: bool
    managed: bool
    split_mode: Any
    split_action: Any
    phase: str | None
    reasons: tuple[str, ...]
    deadline: float | None
    conditions: tuple[Mapping[str, Any], ...]
    raw_phase: str | None
    failure: str | None
    started_at: float | None
    tx_deadline: float | None


def _read_pump(room: RoomBinding, pdc: PdcView | None, now: float) -> _PumpReading | None:
    if not room.has_pdc:
        return None
    attributes: Mapping[str, Any] = {} if pdc is None else pdc.attributes
    state = None if pdc is None else pdc.state
    mode = attributes.get("mode")
    evaluated = _number(attributes.get("evaluated_at"))
    age = None if evaluated is None else now - evaluated
    available = bool(
        pdc is not None
        and attributes.get("schema_version") == 1
        and mode in ("observe", "control")
        and state
        and state not in ("unknown", "unavailable", "starting", "error", "disabled")
        and age is not None
        and _PUBLISH_FRESH_MIN_S <= age <= _PUBLISH_FRESH_MAX_S
    )
    owned = available and mode == "control" and attributes.get("owner") == "controller" and state != "blocked"
    raw_conditions = attributes.get("conditions") if available else None
    conditions = tuple(item for item in raw_conditions if isinstance(item, Mapping) and isinstance(item.get("id"), str)) if isinstance(raw_conditions, list) else ()
    managed_raw = attributes.get("managed") if owned else None
    transaction = attributes.get("transaction") if owned and isinstance(attributes.get("transaction"), Mapping) else {}
    raw_phase = transaction.get("phase") if transaction else None
    reasons = list(dict.fromkeys(item["reason_code"] for item in conditions if isinstance(item.get("reason_code"), str)))
    diagnostic = attributes.get("diagnostic_state") if available else None
    if isinstance(diagnostic, str) and (diagnostic == "ready" or diagnostic.startswith("ready|")):
        reasons.append("ready")
    deadline = _number(attributes.get("deadline")) if available else None
    return _PumpReading(
        available=available,
        owned=owned,
        managed=managed_raw is True,
        split_mode=attributes.get("split_mode") if available else None,
        split_action=attributes.get("split_action") if available else None,
        phase=_PUMP_PHASES.get(raw_phase) if isinstance(raw_phase, str) else None,
        reasons=tuple(reasons),
        deadline=deadline,
        conditions=conditions,
        raw_phase=raw_phase if isinstance(raw_phase, str) else None,
        failure=transaction.get("failure") if isinstance(transaction.get("failure"), str) else None,
        started_at=_number(transaction.get("started_at")) if transaction else None,
        tx_deadline=_number(transaction.get("deadline")) if transaction else None,
    )


# ---------------------------------------------------------------------------
# Conditions.
# ---------------------------------------------------------------------------


def _condition(id_: str, stage: str, status: str, reason_code: str | None = None, **values: Any) -> RoomCondition:
    return RoomCondition(id_, stage, status, reason_code, **values)


def _from_codes(id_: str, codes: Sequence[str], fail: Sequence[str], unknown: Sequence[str] = ()) -> RoomCondition:
    """A condition derived only from the collector exclusion codes of the room."""
    for code in codes:
        if code in fail:
            return _condition(id_, STAGE_READY, CONDITION_FAIL, code)
    for code in codes:
        if code in unknown:
            return _condition(id_, STAGE_READY, CONDITION_UNKNOWN, code)
    return _condition(id_, STAGE_READY, CONDITION_PASS)


def _room_codes(collection: BoilerCollection, room: RoomBinding) -> tuple[bool, tuple[str, ...]]:
    """``(eligible, codes)`` of the room, taken from the collector output."""
    if any(item.room_key == room.room_key for item in collection.rooms):
        return True, ()
    for item in collection.excluded_rooms:
        if item.room_key == room.room_key:
            return False, tuple(item.reasons)
    return False, ()


def _temperature_age(states: Mapping[str, Any], climate_entity_id: str, now: float, local_tz: tzinfo) -> float | None:
    view = _lookup(states, climate_entity_id)
    if view is None:
        return None
    raw = _section(view.attributes, "specific_states").get("last_temperature_datetime")
    if raw is None:
        return None
    stamp = _as_timestamp(raw, math.nan, local_tz)
    return None if not math.isfinite(stamp) else round(now - stamp, 3)


def _ready_and_demand(
    room: RoomBinding,
    reading: _Reading,
    codes: tuple[str, ...],
    eligible: bool,
    collection: BoilerCollection,
    states: Mapping[str, Any],
    now: float,
    local_tz: tzinfo,
    start_gap: float,
    active_count_entity_id: str | None,
) -> list[RoomCondition]:
    unavailable = EXCLUSION_ENTITY_UNAVAILABLE in codes
    result: list[RoomCondition] = []
    if unavailable:
        result.append(_condition("room_available", STAGE_READY, CONDITION_FAIL, EXCLUSION_ENTITY_UNAVAILABLE))
    else:
        result.append(_condition("room_available", STAGE_READY, CONDITION_PASS))

    def derived(id_: str, fail: Sequence[str], unknown: Sequence[str] = (), **values: Any) -> RoomCondition:
        if unavailable:
            return _condition(id_, STAGE_READY, CONDITION_UNKNOWN, "not_evaluated")
        base = _from_codes(id_, codes, fail, unknown)
        return (
            RoomCondition(base.id, base.stage, base.status, base.reason_code, **values)
            if base.status != CONDITION_PASS
            else RoomCondition(base.id, base.stage, base.status, None, **values)
        )

    result.append(derived("hvac_enabled", (EXCLUSION_HVAC_NOT_HEATING,), actual=reading.state if _available(reading.state) else None, expected="heat|auto"))
    result.append(derived("room_ready", (EXCLUSION_NOT_READY,)))
    result.append(derived("initialized", (EXCLUSION_NOT_INITIALIZED,)))

    central = collection.inputs.get("central_mode")
    if central is None or central in _UNAVAILABLE:
        central_cond = _condition("central_mode", STAGE_READY, CONDITION_UNKNOWN, "central_mode_unknown", actual=None, expected="|".join(CENTRAL_MODES_ALLOWING_HEAT))
    elif central in CENTRAL_MODES_ALLOWING_HEAT:
        central_cond = _condition("central_mode", STAGE_READY, CONDITION_PASS, actual=central, expected="|".join(CENTRAL_MODES_ALLOWING_HEAT))
    else:
        central_cond = _condition("central_mode", STAGE_READY, CONDITION_FAIL, "central_off", actual=central, expected="|".join(CENTRAL_MODES_ALLOWING_HEAT))
    result.append(central_cond)

    if unavailable:
        window_cond = _condition("window", STAGE_READY, CONDITION_UNKNOWN, "not_evaluated")
    elif EXCLUSION_WINDOW_OPEN in codes:
        # VTherm waits before suspending: raw sensor open, confirmed state still closed.
        if reading.window["pending"]:
            window_cond = _condition("window", STAGE_READY, CONDITION_WAIT, "window_pending", actual="on", expected="off|bypass")
        else:
            window_cond = _condition("window", STAGE_READY, CONDITION_FAIL, EXCLUSION_WINDOW_OPEN, actual="on", expected="off|bypass")
    elif EXCLUSION_WINDOW_UNKNOWN in codes:
        window_cond = _condition("window", STAGE_READY, CONDITION_UNKNOWN, EXCLUSION_WINDOW_UNKNOWN, actual=None, expected="off|bypass")
    else:
        window_cond = _condition("window", STAGE_READY, CONDITION_PASS, actual="bypass" if reading.window["bypass"] else "off", expected="off|bypass")
    result.append(window_cond)

    result.append(derived("inhibition", (EXCLUSION_INHIBITED,)))
    result.append(derived("safety", (EXCLUSION_SAFETY,)))
    result.append(derived("overpowering", (EXCLUSION_OVERPOWERING,)))

    age = _temperature_age(states, room.climate_entity_id, now, local_tz)
    if unavailable:
        temp_cond = _condition("temperature", STAGE_READY, CONDITION_UNKNOWN, "not_evaluated", unit="s")
    elif EXCLUSION_UNIT_UNSUPPORTED in codes:
        temp_cond = _condition("temperature", STAGE_READY, CONDITION_FAIL, EXCLUSION_UNIT_UNSUPPORTED, actual=age, expected=MAX_TEMPERATURE_AGE_S, unit="s")
    elif EXCLUSION_TEMPERATURE_MISSING in codes:
        temp_cond = _condition("temperature", STAGE_READY, CONDITION_UNKNOWN, EXCLUSION_TEMPERATURE_MISSING, actual=age, expected=MAX_TEMPERATURE_AGE_S, unit="s")
    elif EXCLUSION_TEMPERATURE_STALE in codes:
        temp_cond = _condition("temperature", STAGE_READY, CONDITION_FAIL, EXCLUSION_TEMPERATURE_STALE, actual=age, expected=MAX_TEMPERATURE_AGE_S, unit="s")
    elif EXCLUSION_TEMPERATURE_FUTURE in codes:
        temp_cond = _condition("temperature", STAGE_READY, CONDITION_FAIL, EXCLUSION_TEMPERATURE_FUTURE, actual=age, expected=MAX_TEMPERATURE_AGE_S, unit="s")
    else:
        temp_cond = _condition("temperature", STAGE_READY, CONDITION_PASS, actual=age, expected=MAX_TEMPERATURE_AGE_S, unit="s")
    result.append(temp_cond)

    if room.has_pdc:
        if unavailable:
            result.append(_condition("pdc_released", STAGE_READY, CONDITION_UNKNOWN, "not_evaluated"))
        else:
            result.append(
                _from_codes(
                    "pdc_released",
                    codes,
                    (EXCLUSION_PDC_SESSION_ACTIVE, EXCLUSION_SPLIT_NOT_OFF),
                    (EXCLUSION_PDC_SESSION_UNKNOWN, EXCLUSION_SPLIT_UNKNOWN),
                )
            )

    # Demand stage: ``avatto_no_demand`` from the collector; the fallback to ``hvac_action`` is the only new rule.
    gap = None if reading.current is None or reading.target is None else round(reading.target - reading.current, 3)
    view = _lookup(states, room.climate_entity_id)
    over = _section({} if view is None else view.attributes, "vtherm_over_climate")
    avatto = over.get("avatto_room_demand")
    if unavailable:
        demand_cond = _condition("room_demand", STAGE_DEMAND, CONDITION_UNKNOWN, "not_evaluated", unit="°C")
    elif EXCLUSION_AVATTO_NO_DEMAND in codes:
        demand_cond = _condition("room_demand", STAGE_DEMAND, CONDITION_FAIL, EXCLUSION_AVATTO_NO_DEMAND, actual=gap, expected=start_gap, unit="°C")
    elif isinstance(avatto, bool):
        continuing = gap is not None and gap < start_gap
        demand_cond = _condition("room_demand", STAGE_DEMAND, CONDITION_PASS, "continuing_demand" if continuing else None, actual=gap, expected=start_gap, unit="°C")
    elif reading.action == "heating":
        demand_cond = _condition("room_demand", STAGE_DEMAND, CONDITION_PASS, "trv_request", actual=gap, expected=start_gap, unit="°C")
    else:
        demand_cond = _condition("room_demand", STAGE_DEMAND, CONDITION_UNKNOWN, "demand_unknown", actual=gap, expected=start_gap, unit="°C")
    result.append(demand_cond)

    if eligible:
        valve_cond = _condition("valve_active", STAGE_DEMAND, CONDITION_PASS, actual=None, expected=">=1", unit="count")
    elif unavailable:
        valve_cond = _condition("valve_active", STAGE_DEMAND, CONDITION_UNKNOWN, "not_evaluated", expected=">=1", unit="count")
    else:
        # The collector skips the valves of a room already excluded for another reason: same expression, read directly.
        _, _, ids = _count_validity(states, active_count_entity_id)
        underlying = _underlying_ids(over)
        if not _is_jinja_sequence(ids):
            valve_cond = _condition("valve_active", STAGE_DEMAND, CONDITION_UNKNOWN, EXCLUSION_COUNT_INVALID, expected=">=1", unit="count")
        else:
            try:
                count = len(active_valves(states, underlying, ids))
            except TypeError:
                count = None
            if count is None:
                valve_cond = _condition("valve_active", STAGE_DEMAND, CONDITION_UNKNOWN, EXCLUSION_COUNT_INVALID, expected=">=1", unit="count")
            elif count > 0:
                valve_cond = _condition("valve_active", STAGE_DEMAND, CONDITION_PASS, actual=count, expected=">=1", unit="count")
            else:
                valve_cond = _condition("valve_active", STAGE_DEMAND, CONDITION_FAIL, EXCLUSION_NO_ACTIVE_VALVE, actual=0, expected=">=1", unit="count")
    result.append(valve_cond)
    return result


def _controller_condition(controller: ControllerView | None) -> RoomCondition:
    """Row equivalent to the card ``nativeControllerRow`` (observe never counts as pass)."""
    if controller is None or controller.state is None:
        return _condition("controller", STAGE_BOILER, CONDITION_UNKNOWN, "controller_unknown")
    if controller.state == STATE_STARTING:
        return _condition("controller", STAGE_BOILER, CONDITION_WAIT, "starting", actual=controller.mode, expected="control")
    if controller.state == STATE_ERROR:
        return _condition("controller", STAGE_BOILER, CONDITION_FAIL, "error", actual=controller.mode, expected="control")
    if controller.state == STATE_DISABLED or controller.mode == STATE_DISABLED:
        return _condition("controller", STAGE_BOILER, CONDITION_FAIL, "disabled", actual=controller.mode, expected="control")
    if controller.mode == "observe":
        return _condition("controller", STAGE_BOILER, CONDITION_OBSERVE, "observe_only", actual=controller.owner, expected="controller")
    if controller.owner == "controller":
        return _condition("controller", STAGE_BOILER, CONDITION_PASS, actual=controller.owner, expected="controller")
    return _condition("controller", STAGE_BOILER, CONDITION_FAIL, "not_owner", actual=controller.owner, expected="controller")


def _boiler_conditions(eligible: bool, codes: tuple[str, ...], decision: BoilerDecision | None, controller: ControllerView | None, unavailable: bool) -> list[RoomCondition]:
    if EXCLUSION_NOT_BOILER_PARTICIPANT in codes:
        # The room is not part of the boiler by configuration: the whole stage does not apply.
        names = ("boiler_participation", "eligible_radiator", "controller")
        conds = [_condition(name, STAGE_BOILER, CONDITION_NA, EXCLUSION_NOT_BOILER_PARTICIPANT if name == "boiler_participation" else None) for name in names]
        conds.extend(_condition(item.id, STAGE_BOILER, CONDITION_NA) for item in (decision.conditions if decision is not None else ()))
        return conds
    conds = [
        _condition("boiler_participation", STAGE_BOILER, CONDITION_UNKNOWN, "not_evaluated") if unavailable else _condition("boiler_participation", STAGE_BOILER, CONDITION_PASS),
        _condition("eligible_radiator", STAGE_BOILER, CONDITION_PASS) if eligible else _condition("eligible_radiator", STAGE_BOILER, CONDITION_FAIL, codes[0] if codes else None),
        _controller_condition(controller),
    ]
    if decision is not None:
        for item in decision.conditions:
            data = condition_to_dict(item)
            conds.append(RoomCondition(data["id"], STAGE_BOILER, data["status"], data["reason_code"], data["actual"], data["expected"], data["deadline"], data["unit"]))
    return conds


def _pump_conditions(pump: _PumpReading | None) -> list[RoomCondition]:
    if pump is None:
        return []
    if not pump.available:
        return [_condition("pump_controller", STAGE_PUMP, CONDITION_UNKNOWN, "pdc_unavailable")]
    statuses = (CONDITION_PASS, CONDITION_FAIL, CONDITION_WAIT, CONDITION_UNKNOWN, CONDITION_NA, CONDITION_OBSERVE)
    result: list[RoomCondition] = []
    for item in pump.conditions:
        status = item.get("status")
        result.append(
            RoomCondition(
                item["id"],
                STAGE_PUMP,
                status if status in statuses else CONDITION_UNKNOWN,
                item.get("reason_code") if isinstance(item.get("reason_code"), str) else None,
                item.get("actual"),
                item.get("expected"),
                _number(item.get("deadline")),
                item.get("unit") if isinstance(item.get("unit"), str) else None,
            )
        )
    return result


def _stage_status(items: Sequence[RoomCondition]) -> str:
    real = [item.status for item in items if item.status != CONDITION_NA]
    if not real:
        return CONDITION_NA
    for status in (CONDITION_FAIL, CONDITION_WAIT, CONDITION_UNKNOWN):
        if status in real:
            return status
    return CONDITION_PASS


def build_stages(conditions: Sequence[RoomCondition], has_pdc: bool) -> tuple[RoomStage, ...]:
    stages: list[RoomStage] = []
    for key in STAGE_ORDER:
        if key == STAGE_PUMP and not has_pdc:
            continue
        items = [item for item in conditions if item.stage == key]
        real = [item for item in items if item.status != CONDITION_NA]
        passed = sum(1 for item in real if item.status in (CONDITION_PASS, CONDITION_OBSERVE))
        stages.append(RoomStage(key, _stage_status(items), passed, len(real)))
    return tuple(stages)


def find_blocker(conditions: Sequence[RoomCondition]) -> RoomBlocker | None:
    """First fail/wait along ``ready -> demand -> boiler``; else first unknown; the pump never names it."""
    chain = [item for key in CHAIN_STAGES for item in conditions if item.stage == key]
    for wanted in ((CONDITION_FAIL, CONDITION_WAIT), (CONDITION_UNKNOWN,)):
        for item in chain:
            if item.status in wanted:
                return RoomBlocker(item.id, item.stage, item.status, item.reason_code)
    return None


# ---------------------------------------------------------------------------
# Explanation of the state (port of ``explainRoom``).
# ---------------------------------------------------------------------------


def _explain(
    room: RoomBinding,
    reading: _Reading,
    pump: _PumpReading | None,
    collection: BoilerCollection,
    decision: BoilerDecision | None,
    eligible_collected: bool,
    start_gap: float,
    states: Mapping[str, Any],
) -> tuple[str, str, list[str], float | None]:
    view = _lookup(states, room.climate_entity_id)
    over = _section({} if view is None else view.attributes, "vtherm_over_climate")
    room_demand = over.get("avatto_room_demand")
    demand_known = isinstance(room_demand, bool)
    gap = None if reading.current is None or reading.target is None else round(reading.target - reading.current, 3)
    below_start_threshold = gap is not None and gap > 0 and ((gap < start_gap) if demand_known else reading.below_target is False)
    requested = room_demand is True or reading.heating or (reading.below_target is True and room_demand is not False)
    boiler = _command_state(collection.raw.get("master"))
    eligible = reading.participates is True or (reading.heating and reading.participates is not False and len(reading.underlying) > 0)

    managed = bool(pump and pump.managed)
    split_action = pump.split_action if pump else None
    split_mode = pump.split_mode if pump else None
    measured_heating = split_action == "heating" and split_mode == "heat"
    pump_selected_known = managed

    detail: list[str] = []
    state = reading.code
    reason = reading.code
    deadline: float | None = None

    if managed and pump is not None and pump.phase:
        state = "pump_transition"
        reason = pump.phase
        if reading.code in ("window", "central_off", "safety", "power"):
            detail.append(reading.code)
    elif not reading.available or not reading.initialized or reading.code in _ROOM_WIDE_STATES:
        pass
    elif measured_heating:
        state = "pump_heating"
        reason = "pump_serving" if managed else "pump_measured"
    elif pump_selected_known:
        state = "pump_selected"
        reason = "pump_selected" if split_mode == "heat" else "unknown_reason"
    elif reading.heating and eligible and boiler is True:
        state = "radiator_heating"
        reason = "radiator_heating"
    elif reading.heating and not eligible and reading.participates is False:
        state = "radiator_heating"
        reason = "radiator_reported"
    elif (
        requested
        and eligible
        and boiler is False
        and (reading.heating or (decision is not None and (decision.deadline is not None or decision.reason == BOILER_REASON_AWAITING_CONSENT)))
    ):
        state = "boiler_waiting"
        available = decision is not None
        cooldown = next((item for item in (decision.conditions if decision else ()) if item.id == "cooldown"), None)
        if not available:
            reason = "boiler_diagnostic"
        elif decision.reason == BOILER_REASON_SINGLE_WAIT:
            reason = "boiler_single"
        elif cooldown is not None and cooldown.status == CONDITION_WAIT:
            reason = "boiler_cooldown"
        elif decision.reason == BOILER_REASON_NORMAL_WAIT:
            reason = "boiler_stability"
        elif decision.reason == BOILER_REASON_AWAITING_CONSENT:
            reason = "boiler_consent"
        else:
            reason = "boiler_off"
        deadline = decision.deadline if available else None
    elif requested:
        continuing = not reading.heating and room_demand is True and below_start_threshold
        state = "continuing_heat" if continuing else "heat_waiting"
        reason = "trv_request" if reading.heating else "continuing_demand" if continuing else "room_demand"
    elif reading.code not in ("off", "other_mode", "unknown") and gap is not None:
        state = "at_target" if gap <= 0 else "below_start_threshold" if below_start_threshold else "regulating"
        reason = "at_target" if gap <= 0 else "small_gap" if below_start_threshold else "demand_standby"

    if state == "window" and reading.window["open"] is False:
        reason = "window_closing"
    if reading.window["pending"]:
        detail.append("window_pending")

    can_explain_pump = requested and not managed and not measured_heating and state not in _NO_PUMP_EXPLANATION_STATES
    if pump is not None and can_explain_pump:
        if pump.available:
            blocked = ["pump_window" if code == "window" else code for code in pump.reasons if code not in _ROUTINE_PUMP_REASONS]
        else:
            blocked = ["diagnostics_missing"]
        pump_reason = blocked[0] if blocked else ("pump_hold" if "ready" in pump.reasons else None)
        if pump_reason:
            if state == "radiator_heating":
                detail.append(reason)
            if state in ("radiator_heating", "boiler_waiting"):
                detail.append(pump_reason)
            else:
                reason = pump_reason
            detail.extend(blocked[1:])
            if pump_reason == "pump_hold" and deadline is None:
                deadline = pump.deadline
    elif pump is not None and pump.available and state == "window":
        detail.extend(code for code in pump.reasons if code != "window" and code not in _ROUTINE_PUMP_REASONS)

    reason = _snake(reason)
    state = _snake(state)
    if state not in HEAT_STATES:
        state = "unknown"
    ordered = tuple(dict.fromkeys(_snake(code) for code in detail if code != reason or state == "radiator_heating"))
    return state, reason, list(ordered), deadline


def _source(heat_state: str) -> str:
    if heat_state.startswith("pump_"):
        return SOURCE_PDC
    if heat_state in ("radiator_heating", "boiler_waiting", "heat_waiting", "continuing_heat"):
        return SOURCE_RADIATOR
    return SOURCE_NONE


def _pump_object(pump: _PumpReading | None) -> dict[str, Any] | None:
    if pump is None or not pump.available:
        return None
    return {
        "phase": pump.raw_phase or "idle",
        "failure": pump.failure if pump.failure in _PUMP_FAILURES else pump.failure,
        "started_at": pump.started_at,
        "deadline": pump.tx_deadline,
    }


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def evaluate_room_status(
    room: RoomBinding,
    states: Mapping[str, Any],
    collection: BoilerCollection,
    decision: BoilerDecision | None,
    pdc: PdcView | None,
    controller: ControllerView | None,
    now: float,
    *,
    local_tz: tzinfo = timezone.utc,
    start_gap: float = DEFAULT_THERMAL_GAP_C,
    active_count_entity_id: str | None = None,
) -> RoomStatus:
    """Evaluate one room on the snapshot already collected for the boiler round.

    ``collection`` and ``decision`` must come from the same collector pass and the
    same ``now``: this function reads the exclusion codes of the room from them.
    """
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(float(now)):
        raise ValueError("now must be a finite number")
    if decision is not None and decision.evaluated_at != collection.now:
        raise ValueError("decision and collection must share the same clock")
    now_s = float(now)
    reading = _read_room(states, room.climate_entity_id, start_gap)
    pump = _read_pump(room, pdc, now_s)
    eligible, codes = _room_codes(collection, room)
    unavailable = EXCLUSION_ENTITY_UNAVAILABLE in codes

    conditions = _ready_and_demand(room, reading, codes, eligible, collection, states, now_s, local_tz, start_gap, active_count_entity_id)
    conditions.extend(_boiler_conditions(eligible, codes, decision, controller, unavailable))
    conditions.extend(_pump_conditions(pump))

    heat_state, reason, detail, deadline = _explain(room, reading, pump, collection, decision, eligible, start_gap, states)
    gap = None if reading.current is None or reading.target is None else round(reading.target - reading.current, 3)
    if heat_state == "unknown" and reason in HEAT_STATES:
        reason = "unknown_reason" if reason == "unknown" else reason
    return RoomStatus(
        room_key=room.room_key,
        climate_entity_id=room.climate_entity_id,
        heat_state=heat_state,
        reason=reason,
        detail_reasons=tuple(detail),
        source=_source(heat_state),
        current=reading.current,
        target=reading.target,
        gap=gap,
        start_gap=start_gap,
        deadline=deadline,
        stages=build_stages(conditions, room.has_pdc),
        blocker=find_blocker(conditions),
        conditions=tuple(conditions),
        pump=_pump_object(pump),
        last_error=None,
    )


def room_attributes(status: RoomStatus, *, mode: str, evaluated_at: float, generation: int) -> dict[str, Any]:
    """Published attributes (schema v1). Raises ``ValueError`` over the size limit."""
    attributes = json_safe(
        {
            "schema_version": SCHEMA_VERSION,
            "mode": mode,
            "evaluated_at": evaluated_at,
            "generation": generation,
            "room_key": status.room_key,
            "climate_entity_id": status.climate_entity_id,
            "heat_state": status.heat_state,
            "reason": status.reason,
            "detail_reasons": list(status.detail_reasons),
            "source": status.source,
            "current": status.current,
            "target": status.target,
            "gap": status.gap,
            "start_gap": status.start_gap,
            "deadline": status.deadline,
            "stages": [stage.as_dict() for stage in status.stages],
            "blocker": None if status.blocker is None else status.blocker.as_dict(),
            "conditions": [condition.as_dict() for condition in status.conditions],
            "pump": None if status.pump is None else dict(status.pump),
            "last_error": status.last_error,
        }
    )
    import json  # local: keeps the module header free of unused imports on the hot path

    if len(json.dumps(attributes, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_ATTRIBUTE_BYTES:
        raise ValueError("room attributes too large")
    return attributes


def empty_room_attributes(room_key: str, climate_entity_id: str | None, *, mode: str, evaluated_at: float | None, generation: int, last_error: str | None = None) -> dict[str, Any]:
    """Attributes of the ``disabled``, ``starting`` and ``error`` states (no conditions)."""
    return json_safe(
        {
            "schema_version": SCHEMA_VERSION,
            "mode": mode,
            "evaluated_at": evaluated_at,
            "generation": generation,
            "room_key": room_key,
            "climate_entity_id": climate_entity_id,
            "heat_state": None,
            "reason": None,
            "detail_reasons": [],
            "source": SOURCE_NONE,
            "current": None,
            "target": None,
            "gap": None,
            "start_gap": DEFAULT_THERMAL_GAP_C,
            "deadline": None,
            "stages": [],
            "blocker": None,
            "conditions": [],
            "pump": None,
            "last_error": last_error,
        }
    )


# Attributes that carry the decision; the others change at every tick (heartbeat only).
SIGNIFICANT_KEYS: Final = ("schema_version", "mode", "heat_state", "reason", "source", "start_gap", "last_error")


def room_publication_signature(state: str, attributes: Mapping[str, Any]) -> tuple[Any, ...]:
    """Dedup key: state, reason, gap rounded to 0.1, blocker, stage statuses and condition statuses."""
    gap = attributes.get("gap")
    return (
        state,
        tuple(attributes.get(key) for key in SIGNIFICANT_KEYS),
        None if not isinstance(gap, (int, float)) else round(float(gap), 1),
        tuple(attributes.get("detail_reasons") or ()),
        None if attributes.get("blocker") is None else tuple(sorted((str(k), str(v)) for k, v in attributes["blocker"].items())),
        tuple((item.get("key"), item.get("status"), item.get("passed"), item.get("total")) for item in attributes.get("stages") or ()),
        tuple((item.get("id"), item.get("status"), item.get("reason_code")) for item in attributes.get("conditions") or ()),
        None if not attributes.get("pump") else tuple(sorted((str(k), str(v)) for k, v in attributes["pump"].items() if k not in ("started_at", "deadline"))),
    )
