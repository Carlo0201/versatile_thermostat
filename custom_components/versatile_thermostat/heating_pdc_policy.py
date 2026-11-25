"""Pure decision policy for a room served by a radiator and a heat pump (PDC).

This module is the M3 extraction of the decisions that currently live in the
Home Assistant automations ``*_scelta_radiatore_o_pompa_di_calore`` (choose
branches and their templates), in the template sensor ``*_pdc_diagnostica``
(legacy reason codes and the ``ready`` gate), in ``*_registro_ciclo_persistente_pdc``
(confirmed HEAT/OFF and initial target) and in the stop conditions of
``*_protezioni_override_condizionatore``.

It is deliberately pure: no Home Assistant import, no I/O, no global clock, no
asyncio and no service call. The same inputs always produce the same decision.
The command sequence (inhibition, waits, confirmations, fallbacks) is not here:
it belongs to the transaction model (M4). This module only exposes the
predicates that the sequence re-evaluates after each wait.

Inputs are normalized by the future collector. Entity states are kept as raw
strings (``None`` for a missing entity) so that ``unknown``/``unavailable`` are
never turned into ``off`` or zero. Numbers are already parsed like the Jinja
``float(none)`` filter. Times are absolute UTC/Unix seconds; a missing
``last_reported``/``last_changed`` is handled like ``as_timestamp(..., 0)``,
that is as a very old sample.

Baseline quirks are preserved on purpose and must not be silently corrected:

* the keep-running check accepts a temperature or power sample from the future
  (age < 0) and a negative power, while the start check rejects a negative
  power and the thermal stop requires ``0 <= age``;
* the start edge is one-shot: the baseline state trigger fires once when the
  diagnostic has been ``ready`` for 120 s. If a guard that is not part of the
  diagnostic (raw last_changed guards, override not ``off``) fails at that
  instant, no new start is attempted until the diagnostic leaves ``ready``
  (``start_edge_one_shot``);
* the window grace while running uses the Home Assistant state condition
  ``for`` semantics, which is strict (open for more than 120 s);
* the stop journal reason reports ``window`` whenever the window is not
  ``off``, even when the stop was caused by the temperature;
* with the override in any state other than ``off`` the chooser does nothing,
  not even a protective stop; the override protections only act when the
  override is ``on``.

Deliberate hardening, documented in ``M3-ESITO.md``: non-finite numbers are not
accepted in the snapshot, non-finite or boolean cycle values are treated as
missing, and a non-finite program horizon is treated as invalid.

The structured ``conditions`` tuple is informational (diagnostic contract
proposal). The policy never reads it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Mapping

PdcAction = Literal["none", "start", "stop", "set_setpoint"]
PDC_ACTION_NONE: Final = "none"
PDC_ACTION_START: Final = "start"
PDC_ACTION_STOP: Final = "stop"
PDC_ACTION_SET_SETPOINT: Final = "set_setpoint"

# Decision reasons (stable codes, not translated).
PDC_REASON_OVERRIDE: Final = "manual_override"
PDC_REASON_OVERRIDE_UNKNOWN: Final = "override_unknown"
PDC_REASON_STOP: Final = "stop"
PDC_REASON_SETPOINT: Final = "setpoint"
PDC_REASON_RUNNING: Final = "running"
PDC_REASON_MANAGED_UNKNOWN: Final = "managed_unknown"
PDC_REASON_NOT_READY: Final = "not_ready"
PDC_REASON_STABILITY_WAIT: Final = "stability_wait"
PDC_REASON_EDGE_CONSUMED: Final = "edge_consumed"
PDC_REASON_START_BLOCKED: Final = "start_blocked"
PDC_REASON_START: Final = "start"

# Legacy diagnostic codes of ``sensor.<room>_pdc_diagnostica`` (CONTRATTI section 4).
CODE_READY: Final = "ready"
CODE_SCHEDULE_CHANGE: Final = "schedule_change"
CODE_DATA_MISSING: Final = "data_missing"
CODE_MANUAL_OVERRIDE: Final = "manual_override"
CODE_WINDOW: Final = "window"
CODE_MANAGED: Final = "managed"
CODE_AUTOMATIC_OFF: Final = "automatic_off"
CODE_CENTRAL_MODE: Final = "central_mode"
CODE_ROOM_OFF: Final = "room_off"
CODE_PRESET: Final = "preset"
CODE_TEMPERATURE_MISSING: Final = "temperature_missing"
CODE_AT_TARGET: Final = "at_target"
CODE_SMALL_GAP: Final = "small_gap"
CODE_POWER_MISSING: Final = "power_missing"
CODE_POWER_HIGH: Final = "power_high"
CODE_OTHER_PDC: Final = "other_pdc"
CODE_SPLIT_BUSY: Final = "split_busy"
CODE_COOLDOWN: Final = "cooldown"
# New codes (start guards outside the legacy diagnostic, M3 proposal).
CODE_RAW_GUARD: Final = "raw_guard"
CODE_EDGE_CONSUMED: Final = "edge_consumed"

# Stop journal reasons (``journal_reason`` of the baseline), in baseline order.
STOP_POWER_LIMIT: Final = "power_limit"
STOP_WINDOW: Final = "window"
STOP_CENTRAL: Final = "central"
STOP_AUTOMATIC_DISABLED: Final = "automatic_disabled"
STOP_PROGRAM_OFF: Final = "program_off"
STOP_DATA_MISSING: Final = "data_missing"
STOP_SPLIT_MODE: Final = "split_mode"
STOP_TARGET_LOWERED: Final = "target_lowered"
STOP_TEMPERATURE_LIMIT: Final = "temperature_limit"
STOP_TARGET_REACHED: Final = "target_reached"
STOP_CONDITIONS_CHANGED: Final = "conditions_changed"

# Start sequence stages and failure classification of the baseline helper ``*_pdc_transizione``.
STAGE_WAITING_TRV: Final = "waiting_trv"
STAGE_WAITING_TARGET: Final = "waiting_target"
STAGE_WAITING_SPLIT: Final = "waiting_split"
FAILURE_TRV_UNCONFIRMED: Final = "trv_unconfirmed"
FAILURE_TARGET_UNCONFIRMED: Final = "target_unconfirmed"
FAILURE_SPLIT_UNCONFIRMED: Final = "split_unconfirmed"
FAILURE_CONDITIONS_CHANGED: Final = "conditions_changed"

STATE_ON: Final = "on"
STATE_OFF: Final = "off"
STATE_HEAT: Final = "heat"
STATE_UNKNOWN: Final = "unknown"
STATE_UNAVAILABLE: Final = "unavailable"
TIMER_ACTIVE: Final = "active"
TIMER_IDLE: Final = "idle"
SCHEDULE_NONE: Final = "none"

# Split modes accepted by the persistent cycle register and the override protections.
SPLIT_RECORDED_MODES: Final = ("off", "heat", "cool", "auto", "dry", "fan_only", "heat_cool")
SPLIT_ACTIVE_MODES: Final = ("heat", "cool", "auto", "dry", "fan_only", "heat_cool")

ConditionStatus = Literal["pass", "fail", "wait", "unknown", "na"]
CONDITION_PASS: Final = "pass"
CONDITION_FAIL: Final = "fail"
CONDITION_WAIT: Final = "wait"
CONDITION_UNKNOWN: Final = "unknown"
CONDITION_NA: Final = "na"


def _finite_number(name: str, value: Any, *, minimum: float | None = None) -> float:
    """Validate a finite number and return it as float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and number < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return number


def _optional_number(name: str, value: Any) -> None:
    if value is not None:
        _finite_number(name, value)


def _optional_state(name: str, value: Any) -> None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{name} must be a string or None")


def _string_tuple(name: str, value: Any, *, allow_none: bool = False) -> tuple:
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)):
        raise TypeError(f"{name} must be a tuple of strings")
    items = tuple(value)
    for item in items:
        if item is None and allow_none:
            continue
        if not isinstance(item, str):
            raise TypeError(f"{name} must contain strings")
    return items


def _coerce_float(value: Any) -> float | None:
    """Coerce a stored value like the Jinja ``float(none)`` filter.

    Booleans, non-finite values and unparsable strings return ``None`` (hardening:
    Jinja would turn ``true`` into 1.0 and ``"nan"`` into NaN).
    """
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


def has_value(state: str | None) -> bool:
    """Reproduce the Home Assistant ``has_value`` template function."""
    return state is not None and state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)


def _age(now: float, at: float | None) -> float:
    """Age of a sample like ``now - as_timestamp(last_reported, 0)``."""
    return now - (0.0 if at is None else at)


@dataclass(frozen=True, slots=True)
class PdcPolicyConfig:
    """Validated parameters of the PDC policy.

    The defaults are the values shared by the two captured rooms. The room
    differences are explicit parameters: ``setpoint_min_c`` (Camera Carlo 16,
    Camera Diego Luisa 18) and ``manual_preset_bypass`` (Camera Carlo only).
    Entity ids stay outside this model.
    """

    start_gap_c: float = 0.4
    comparison_epsilon_c: float = 0.000001
    stability_s: float = 120.0
    temperature_max_age_s: float = 3600.0
    power_max_age_s: float = 45.0
    power_reserve_w: float = 1500.0
    power_budget_w: float = 2600.0
    diagnostic_power_limit_w: float = 1100.0
    stop_power_w: float = 2700.0
    schedule_horizon_s: float = 600.0
    off_cooldown_s: float = 600.0
    raw_guard_s: float = 600.0
    min_on_s: float = 600.0
    stop_margin_c: float = 0.2
    early_stop_margin_c: float = 0.5
    window_running_grace_s: float = 120.0
    override_window_open_s: float = 120.0
    override_window_unknown_s: float = 30.0
    setpoint_offset_c: float = 1.0
    setpoint_min_c: float = 16.0
    setpoint_max_c: float = 30.0
    allowed_presets: tuple[str, ...] = ("none", "eco", "comfort", "boost")
    allowed_central_modes: tuple[str, ...] = ("Auto", "Heat only")
    central_stopped_mode: str = "Stopped"
    manual_preset_bypass: bool = False
    start_edge_one_shot: bool = True
    start_settle_delay_s: float = 2.0

    def __post_init__(self) -> None:
        """Reject parameters that would make the decision non-deterministic."""
        for name in (
            "start_gap_c",
            "comparison_epsilon_c",
            "stability_s",
            "temperature_max_age_s",
            "power_max_age_s",
            "power_reserve_w",
            "power_budget_w",
            "diagnostic_power_limit_w",
            "stop_power_w",
            "schedule_horizon_s",
            "off_cooldown_s",
            "raw_guard_s",
            "min_on_s",
            "stop_margin_c",
            "early_stop_margin_c",
            "window_running_grace_s",
            "override_window_open_s",
            "override_window_unknown_s",
            "start_settle_delay_s",
        ):
            _finite_number(name, getattr(self, name), minimum=0.0)
        _finite_number("setpoint_offset_c", self.setpoint_offset_c)
        low = _finite_number("setpoint_min_c", self.setpoint_min_c)
        high = _finite_number("setpoint_max_c", self.setpoint_max_c)
        if low > high:
            raise ValueError("setpoint_min_c must be <= setpoint_max_c")
        object.__setattr__(self, "allowed_presets", _string_tuple("allowed_presets", self.allowed_presets))
        object.__setattr__(self, "allowed_central_modes", _string_tuple("allowed_central_modes", self.allowed_central_modes))
        if not self.allowed_central_modes:
            raise ValueError("allowed_central_modes must not be empty")
        if not isinstance(self.central_stopped_mode, str):
            raise TypeError("central_stopped_mode must be a string")
        for name in ("manual_preset_bypass", "start_edge_one_shot"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a boolean")


DEFAULT_CONFIG: Final = PdcPolicyConfig()


@dataclass(frozen=True, slots=True)
class PdcCycle:
    """Confirmed split cycle (``input_text.<room>_pdc_ciclo``).

    ``mode`` is the last confirmed split mode, ``on``/``off`` the whole-second
    instants of the last confirmed HEAT/OFF and ``target`` the radiator target
    sampled when HEAT was confirmed (initial target).
    """

    mode: str | None = None
    on: float | None = None
    off: float | None = None
    target: float | None = None

    def __post_init__(self) -> None:
        """Validate the cycle."""
        _optional_state("mode", self.mode)
        for name in ("on", "off", "target"):
            _optional_number(name, getattr(self, name))


EMPTY_CYCLE: Final = PdcCycle()


def parse_pdc_cycle(value: Any) -> PdcCycle:
    """Build a cycle from the decoded JSON value (``from_json({})``)."""
    if not isinstance(value, Mapping):
        return EMPTY_CYCLE
    mode = value.get("m")
    return PdcCycle(
        mode=mode if isinstance(mode, str) else None,
        on=_coerce_float(value.get("on")),
        off=_coerce_float(value.get("off")),
        target=_coerce_float(value.get("target")),
    )


def observe_split_mode(previous: Any, mode: str | None, radiator_target: float | None, now: float) -> dict[str, Any] | None:
    """Reproduce ``*_registro_ciclo_persistente_pdc``.

    ``previous`` is the decoded JSON of the stored cycle (``{}`` when the
    stored text is not valid JSON, as ``from_json({})`` does). Returns the new
    JSON object to persist, or ``None`` when the baseline does not write.
    Values not rewritten are copied as stored (raw), like the baseline.
    """
    n = _finite_number("now", now)
    _optional_state("mode", mode)
    _optional_number("radiator_target", radiator_target)
    if not isinstance(previous, Mapping) or mode not in SPLIT_RECORDED_MODES:
        return None
    changed = previous.get("m") != mode
    missing_on = mode == STATE_HEAT and _coerce_float(previous.get("on")) is None
    missing_off = mode == STATE_OFF and _coerce_float(previous.get("off")) is None
    if not (changed or missing_on or missing_off):
        return None
    ts = int(n)
    return {
        "m": mode,
        "on": ts if mode == STATE_HEAT else previous.get("on"),
        "off": ts if mode == STATE_OFF else previous.get("off"),
        "target": (None if radiator_target is None else float(radiator_target)) if mode == STATE_HEAT else previous.get("target"),
    }


@dataclass(frozen=True, slots=True)
class PdcSnapshot:
    """Normalized inputs of one evaluation for one room.

    State fields keep the raw Home Assistant state; ``None`` is a missing entity.
    ``*_at`` fields are ``last_changed``/``last_reported`` instants.
    """

    override: str | None
    managed: str | None
    managed_changed_at: float | None
    automatic: str | None
    central_mode: str | None
    radiator_mode: str | None
    requested_hvac_mode: str | None
    preset_mode: str | None
    temperature: float | None
    temperature_reported_at: float | None
    target: float | None
    window: str | None
    window_changed_at: float | None
    power: float | None
    power_reported_at: float | None
    split_mode: str | None
    split_changed_at: float | None
    pdc_mode: str | None
    pdc_setpoint: float | None
    other_managed: str | None
    other_split_mode: str | None
    schedule_stop: str | None
    cycle: PdcCycle = EMPTY_CYCLE
    cycle_available: bool = True
    heating_override_timer: str | None = None
    schedule_switches: tuple[str | None, ...] = ()
    radiator_valve_mode: str | None = None
    radiator_inhibited: bool | None = None
    # None means the radiator does not publish native room-override attributes.
    room_manual_regulation: bool | None = None

    def __post_init__(self) -> None:
        """Validate the snapshot without touching caller data."""
        for name in (
            "override",
            "managed",
            "automatic",
            "central_mode",
            "radiator_mode",
            "requested_hvac_mode",
            "preset_mode",
            "window",
            "split_mode",
            "pdc_mode",
            "other_managed",
            "other_split_mode",
            "schedule_stop",
            "heating_override_timer",
            "radiator_valve_mode",
        ):
            _optional_state(name, getattr(self, name))
        for name in (
            "managed_changed_at",
            "temperature",
            "temperature_reported_at",
            "target",
            "window_changed_at",
            "power",
            "power_reported_at",
            "split_changed_at",
            "pdc_setpoint",
        ):
            _optional_number(name, getattr(self, name))
        if not isinstance(self.cycle, PdcCycle):
            raise TypeError("cycle must be a PdcCycle")
        if not isinstance(self.cycle_available, bool):
            raise TypeError("cycle_available must be a boolean")
        object.__setattr__(self, "schedule_switches", _string_tuple("schedule_switches", self.schedule_switches, allow_none=True))
        if self.radiator_inhibited is not None and not isinstance(self.radiator_inhibited, bool):
            raise TypeError("radiator_inhibited must be a boolean or None")
        if self.room_manual_regulation is not None and not isinstance(self.room_manual_regulation, bool):
            raise TypeError("room_manual_regulation must be a boolean or None")


@dataclass(frozen=True, slots=True)
class PdcStartContext:
    """Ephemeral stability state of the start edge (not persisted by the baseline).

    ``ready_since`` is the instant from which the diagnostic is continuously
    ``ready``; ``edge_consumed`` is true once the one-shot start edge of that
    ready period has been evaluated.
    """

    ready_since: float | None = None
    edge_consumed: bool = False

    def __post_init__(self) -> None:
        """Validate the context."""
        _optional_number("ready_since", self.ready_since)
        if not isinstance(self.edge_consumed, bool):
            raise TypeError("edge_consumed must be a boolean")


EMPTY_CONTEXT: Final = PdcStartContext()


@dataclass(frozen=True, slots=True)
class PdcCondition:
    """One structured diagnostic condition (CONTRATTI section 2/4 shape)."""

    id: str
    status: ConditionStatus
    reason_code: str | None = None
    actual: Any = None
    expected: Any = None
    deadline: float | None = None
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class PdcDecision:
    """Result of one evaluation. ``action`` is a proposal, never a confirmation."""

    action: PdcAction
    reason: str
    evaluated_at: float
    context: PdcStartContext
    deadline: float | None
    diagnostic_codes: tuple[str, ...]
    diagnostic_state: str
    setpoint: float | None = None
    stop_reason: str | None = None
    conditions: tuple[PdcCondition, ...] = field(default_factory=tuple)


# --- Predicates shared with the transaction sequence (M4) -------------------------------------


def preset_ok(snapshot: PdcSnapshot, config: PdcPolicyConfig = DEFAULT_CONFIG) -> bool:
    """Preset compatibility; Camera Carlo also accepts the manual Oikos regulation."""
    if snapshot.preset_mode in config.allowed_presets:
        return True
    if not config.manual_preset_bypass:
        return False
    if snapshot.room_manual_regulation is not None:
        return snapshot.room_manual_regulation
    timer = snapshot.heating_override_timer
    return timer == TIMER_ACTIVE or (timer == TIMER_IDLE and all(switch == STATE_OFF for switch in snapshot.schedule_switches))


def automatic_ok(snapshot: PdcSnapshot, config: PdcPolicyConfig = DEFAULT_CONFIG) -> bool:
    """Automatic enabled, room program in heat, preset and central mode compatible."""
    return snapshot.automatic == STATE_ON and snapshot.requested_hvac_mode == STATE_HEAT and preset_ok(snapshot, config) and snapshot.central_mode in config.allowed_central_modes


def _temperature_ok(snapshot: PdcSnapshot, now: float, config: PdcPolicyConfig) -> bool:
    # No lower bound on the age: a sample from the future passes (baseline).
    return snapshot.temperature is not None and snapshot.target is not None and _age(now, snapshot.temperature_reported_at) <= config.temperature_max_age_s


def _power_fresh(snapshot: PdcSnapshot, now: float, config: PdcPolicyConfig) -> bool:
    return snapshot.power is not None and _age(now, snapshot.power_reported_at) <= config.power_max_age_s


def _gap_ok(snapshot: PdcSnapshot, config: PdcPolicyConfig) -> bool:
    assert snapshot.temperature is not None and snapshot.target is not None
    return (snapshot.target - snapshot.temperature) >= config.start_gap_c - config.comparison_epsilon_c


def _capacity_ok(snapshot: PdcSnapshot, now: float, config: PdcPolicyConfig) -> bool:
    if not _power_fresh(snapshot, now, config):
        return False
    assert snapshot.power is not None
    return 0 <= snapshot.power and snapshot.power + config.power_reserve_w <= config.power_budget_w


def other_pdc_free(snapshot: PdcSnapshot) -> bool:
    """Mutual exclusion: the other PDC is neither managed nor running."""
    return snapshot.other_managed == STATE_OFF and snapshot.other_split_mode == STATE_OFF


def start_core_ok(snapshot: PdcSnapshot, now: float, config: PdcPolicyConfig = DEFAULT_CONFIG) -> bool:
    """Common part of the start templates (also re-checked after each wait)."""
    return (
        automatic_ok(snapshot, config)
        and snapshot.window == STATE_OFF
        and _temperature_ok(snapshot, now, config)
        and _gap_ok(snapshot, config)
        and _capacity_ok(snapshot, now, config)
        and other_pdc_free(snapshot)
    )


def raw_guards_ok(snapshot: PdcSnapshot, now: float, config: PdcPolicyConfig = DEFAULT_CONFIG) -> bool:
    """Local availability and the conservative raw last_changed guards of the start branch."""
    return (
        snapshot.managed == STATE_OFF
        and snapshot.radiator_mode == STATE_HEAT
        and snapshot.split_mode == STATE_OFF
        and _age(now, snapshot.split_changed_at) >= config.raw_guard_s
        and _age(now, snapshot.managed_changed_at) >= config.raw_guard_s
        and snapshot.pdc_mode == STATE_OFF
    )


def schedule_value(snapshot: PdcSnapshot) -> float | None:
    """Program horizon like ``float(none)``; non-finite is treated as invalid (hardening)."""
    return _coerce_float(snapshot.schedule_stop)


def schedule_ok(snapshot: PdcSnapshot, now: float, config: PdcPolicyConfig = DEFAULT_CONFIG) -> bool:
    """No program change that removes the request within the horizon (``float(0)`` semantics)."""
    if snapshot.schedule_stop == SCHEDULE_NONE:
        return True
    value = schedule_value(snapshot)
    return (0.0 if value is None else value) > now + config.schedule_horizon_s


def cooldown_ok(snapshot: PdcSnapshot, now: float, config: PdcPolicyConfig = DEFAULT_CONFIG) -> bool:
    """At least ``off_cooldown_s`` since the persisted confirmed OFF."""
    stopped = snapshot.cycle.off
    return snapshot.cycle.mode == STATE_OFF and stopped is not None and 0 <= stopped <= now - config.off_cooldown_s


def start_conditions_ok(snapshot: PdcSnapshot, now: float, config: PdcPolicyConfig = DEFAULT_CONFIG) -> bool:
    """All conditions of the start branch except the trigger edge and the diagnostic gate."""
    return start_core_ok(snapshot, now, config) and raw_guards_ok(snapshot, now, config) and schedule_ok(snapshot, now, config) and cooldown_ok(snapshot, now, config)


def radiator_stopped(snapshot: PdcSnapshot) -> bool:
    """Radiator stop confirmed: physical valve OFF, VTherm OFF and inhibition reported."""
    return snapshot.radiator_valve_mode == STATE_OFF and snapshot.radiator_mode == STATE_OFF and snapshot.radiator_inhibited is True


def recheck_after_radiator_stop(snapshot: PdcSnapshot, now: float, config: PdcPolicyConfig = DEFAULT_CONFIG) -> bool:
    """Condition of the baseline ``if`` after the TRV wait (start sequence step 3)."""
    return radiator_stopped(snapshot) and start_core_ok(snapshot, now, config) and schedule_ok(snapshot, now, config)


def recheck_after_setpoint(snapshot: PdcSnapshot, now: float, config: PdcPolicyConfig = DEFAULT_CONFIG) -> bool:
    """Condition of the baseline ``if`` after the setpoint command (start sequence step 3.3)."""
    return start_core_ok(snapshot, now, config) and snapshot.pdc_setpoint == split_setpoint(snapshot.target, config) and schedule_ok(snapshot, now, config)


def window_running_ok(snapshot: PdcSnapshot, now: float, config: PdcPolicyConfig = DEFAULT_CONFIG) -> bool:
    """Window closed, or open for not more than the grace (HA ``for`` is strict)."""
    if snapshot.window == STATE_OFF:
        return True
    if snapshot.window != STATE_ON:
        return False
    changed = 0.0 if snapshot.window_changed_at is None else snapshot.window_changed_at
    return not (now - config.window_running_grace_s > changed)


def keep_running_ok(snapshot: PdcSnapshot, now: float, config: PdcPolicyConfig = DEFAULT_CONFIG) -> bool:
    """Running protections (stop branch, negated) and the confirmation after HEAT."""
    return (
        automatic_ok(snapshot, config)
        and _temperature_ok(snapshot, now, config)
        and _power_fresh(snapshot, now, config)
        and window_running_ok(snapshot, now, config)
        and snapshot.power is not None
        and snapshot.power < config.stop_power_w
        and snapshot.pdc_mode == STATE_HEAT
        and snapshot.split_mode == STATE_HEAT
    )


def minimum_on_done(snapshot: PdcSnapshot, now: float, config: PdcPolicyConfig = DEFAULT_CONFIG) -> bool:
    """At least ``min_on_s`` since the persisted confirmed HEAT."""
    started = snapshot.cycle.on
    return snapshot.cycle.mode == STATE_HEAT and started is not None and now - started >= config.min_on_s


def target_lowered(snapshot: PdcSnapshot) -> bool:
    """Target lowered below the initial target and already reached."""
    t, target, start = snapshot.temperature, snapshot.target, snapshot.cycle.target
    return target is not None and start is not None and target < start and t is not None and t >= target


def thermal_stop(snapshot: PdcSnapshot, now: float, config: PdcPolicyConfig = DEFAULT_CONFIG) -> bool:
    """Ordinary, early and lowered-target stop (requires a non-future, fresh sample)."""
    t, target = snapshot.temperature, snapshot.target
    if t is None or target is None or not 0 <= _age(now, snapshot.temperature_reported_at) <= config.temperature_max_age_s:
        return False
    eps = config.comparison_epsilon_c
    return t >= target + config.early_stop_margin_c - eps or (minimum_on_done(snapshot, now, config) and t >= target + config.stop_margin_c - eps) or target_lowered(snapshot)


def split_setpoint(target: float | None, config: PdcPolicyConfig = DEFAULT_CONFIG) -> float | None:
    """``[[ceil(target + 1), min] | max, max] | min``; ``None`` when the target is missing."""
    if target is None:
        return None
    raised = int(math.ceil(float(target + config.setpoint_offset_c) * 1.0) / 1.0)
    return float(min(max(raised, config.setpoint_min_c), config.setpoint_max_c))


def stop_reason(snapshot: PdcSnapshot, now: float, config: PdcPolicyConfig = DEFAULT_CONFIG) -> str:
    """Journal reason of a stop, in the exact baseline order."""
    t, target, p = snapshot.temperature, snapshot.target, snapshot.power
    eps = config.comparison_epsilon_c
    if p is not None and p >= config.stop_power_w:
        return STOP_POWER_LIMIT
    if snapshot.window != STATE_OFF:
        return STOP_WINDOW
    if snapshot.central_mode not in config.allowed_central_modes:
        return STOP_CENTRAL
    if snapshot.automatic != STATE_ON:
        return STOP_AUTOMATIC_DISABLED
    if snapshot.requested_hvac_mode != STATE_HEAT:
        return STOP_PROGRAM_OFF
    if (
        t is None
        or target is None
        or p is None
        or _age(now, snapshot.power_reported_at) > config.power_max_age_s
        or _age(now, snapshot.temperature_reported_at) > config.temperature_max_age_s
    ):
        return STOP_DATA_MISSING
    if snapshot.split_mode != STATE_HEAT or snapshot.pdc_mode != STATE_HEAT:
        return STOP_SPLIT_MODE
    start = snapshot.cycle.target
    if start is not None and target < start and t >= target:
        return STOP_TARGET_LOWERED
    if t >= target + config.early_stop_margin_c - eps:
        return STOP_TEMPERATURE_LIMIT
    if t >= target + config.stop_margin_c - eps:
        return STOP_TARGET_REACHED
    return STOP_CONDITIONS_CHANGED


def classify_start_failure(stage: str | None, snapshot: PdcSnapshot, config: PdcPolicyConfig = DEFAULT_CONFIG) -> str | None:
    """Failure code written when the start sequence falls back to the radiator.

    Returns ``None`` where the baseline template cannot render (stage
    ``waiting_target`` with a missing target): the helper keeps its value.
    """
    if stage == STAGE_WAITING_TRV and not radiator_stopped(snapshot):
        return FAILURE_TRV_UNCONFIRMED
    if stage == STAGE_WAITING_TARGET:
        setpoint = split_setpoint(snapshot.target, config)
        if setpoint is None:
            return None
        if snapshot.pdc_setpoint != setpoint:
            return FAILURE_TARGET_UNCONFIRMED
    if stage == STAGE_WAITING_SPLIT and snapshot.split_mode != STATE_HEAT:
        return FAILURE_SPLIT_UNCONFIRMED
    return FAILURE_CONDITIONS_CHANGED


def override_protection_stop(snapshot: PdcSnapshot, now: float, config: PdcPolicyConfig = DEFAULT_CONFIG) -> bool:
    """Conditions of ``*_protezioni_override_condizionatore`` (manual mode protections).

    ``True`` means the protective stop sequence must run. The override is kept
    after the stop (no automatic restart).
    """
    if snapshot.override != STATE_ON or snapshot.split_mode not in SPLIT_ACTIVE_MODES:
        return False
    p = snapshot.power
    if p is None or p < 0 or p >= config.stop_power_w or _age(now, snapshot.power_reported_at) > config.power_max_age_s or snapshot.central_mode == config.central_stopped_mode:
        return True
    changed = 0.0 if snapshot.window_changed_at is None else snapshot.window_changed_at
    if snapshot.window == STATE_ON and now - config.override_window_open_s > changed:
        return True
    return snapshot.window not in (STATE_ON, STATE_OFF) and now - changed >= config.override_window_unknown_s


def diagnostic_codes(snapshot: PdcSnapshot, now: float, config: PdcPolicyConfig = DEFAULT_CONFIG) -> tuple[str, ...]:
    """Legacy reason codes of ``sensor.<room>_pdc_diagnostica`` (empty means ``ready``)."""
    t, target, p = snapshot.temperature, snapshot.target, snapshot.power
    schedule = schedule_value(snapshot)
    stopped = snapshot.cycle.off
    eps = config.comparison_epsilon_c
    checks = (
        (CODE_SCHEDULE_CHANGE, schedule is not None and schedule <= now + config.schedule_horizon_s),
        (CODE_DATA_MISSING, snapshot.schedule_stop != SCHEDULE_NONE and schedule is None),
        (CODE_MANUAL_OVERRIDE, snapshot.override == STATE_ON),
        (CODE_WINDOW, snapshot.window == STATE_ON),
        (
            CODE_DATA_MISSING,
            snapshot.window != STATE_OFF
            or not has_value(snapshot.radiator_mode)
            or not has_value(snapshot.split_mode)
            or not has_value(snapshot.pdc_mode)
            or not has_value(snapshot.managed),
        ),
        (CODE_MANAGED, snapshot.managed == STATE_ON),
        (CODE_AUTOMATIC_OFF, snapshot.automatic == STATE_OFF),
        (CODE_DATA_MISSING, snapshot.automatic != STATE_ON or snapshot.managed != STATE_OFF or not has_value(snapshot.central_mode)),
        (CODE_CENTRAL_MODE, snapshot.central_mode not in config.allowed_central_modes),
        (CODE_ROOM_OFF, snapshot.requested_hvac_mode != STATE_HEAT or snapshot.radiator_mode != STATE_HEAT),
        (CODE_PRESET, not preset_ok(snapshot, config)),
        (CODE_TEMPERATURE_MISSING, t is None or target is None or _age(now, snapshot.temperature_reported_at) > config.temperature_max_age_s),
        (CODE_AT_TARGET, t is not None and target is not None and t >= target),
        (CODE_SMALL_GAP, t is not None and target is not None and target - t < config.start_gap_c - eps),
        (CODE_POWER_MISSING, p is None or _age(now, snapshot.power_reported_at) > config.power_max_age_s or (p is not None and p < 0)),
        (CODE_POWER_HIGH, p is not None and p > config.diagnostic_power_limit_w),
        (CODE_OTHER_PDC, snapshot.other_managed == STATE_ON or (has_value(snapshot.other_split_mode) and snapshot.other_split_mode != STATE_OFF)),
        (CODE_DATA_MISSING, not has_value(snapshot.other_managed) or not has_value(snapshot.other_split_mode)),
        (CODE_SPLIT_BUSY, snapshot.split_mode != STATE_OFF or snapshot.pdc_mode != STATE_OFF),
        (CODE_DATA_MISSING, not snapshot.cycle_available or stopped is None),
        (CODE_COOLDOWN, snapshot.cycle.mode != STATE_OFF or stopped is None or stopped > now - config.off_cooldown_s or stopped < 0),
    )
    codes: list[str] = []
    for code, active in checks:
        if active and code not in codes:
            codes.append(code)
    return tuple(codes)


def _format_number(value: float) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else repr(number)


def diagnostic_state(codes: tuple[str, ...], config: PdcPolicyConfig = DEFAULT_CONFIG) -> str:
    """Legacy diagnostic string ``codes|limit_W|stability_s``."""
    head = ",".join(codes) if codes else CODE_READY
    return f"{head}|{_format_number(config.diagnostic_power_limit_w)}|{_format_number(config.stability_s)}"


# --- Main evaluation ---------------------------------------------------------------------------


def evaluate_pdc(
    snapshot: PdcSnapshot,
    context: PdcStartContext | None,
    now: float,
    config: PdcPolicyConfig | None = None,
) -> PdcDecision:
    """Evaluate the chooser at ``now`` (absolute Unix seconds).

    The branch order is the baseline ``choose``: return to the radiator, update
    the split setpoint, start. The start branch requires the one-shot edge of a
    diagnostic continuously ``ready`` for ``stability_s``. The edge is
    consumed by the evaluation that sees it, whatever the outcome.
    """
    if not isinstance(snapshot, PdcSnapshot):
        raise TypeError("snapshot must be a PdcSnapshot")
    if context is not None and not isinstance(context, PdcStartContext):
        raise TypeError("context must be a PdcStartContext or None")
    if config is None:
        config = DEFAULT_CONFIG
    elif not isinstance(config, PdcPolicyConfig):
        raise TypeError("config must be a PdcPolicyConfig or None")
    n = _finite_number("now", now)
    context = context if context is not None else EMPTY_CONTEXT

    codes = diagnostic_codes(snapshot, n, config)
    ready = not codes
    ready_since = context.ready_since if ready else None
    consumed = context.edge_consumed if ready else False
    if ready and (ready_since is None or ready_since < 0 or ready_since > n):
        ready_since = n
        consumed = False
    edge = ready and ready_since is not None and n >= ready_since + config.stability_s and not consumed
    next_consumed = consumed or (edge and config.start_edge_one_shot)
    next_context = PdcStartContext(ready_since=ready_since, edge_consumed=next_consumed)

    action: PdcAction = PDC_ACTION_NONE
    setpoint = split_setpoint(snapshot.target, config)
    reason_stop: str | None = None

    if snapshot.override != STATE_OFF:
        reason = PDC_REASON_OVERRIDE if snapshot.override == STATE_ON else PDC_REASON_OVERRIDE_UNKNOWN
    elif snapshot.managed == STATE_ON:
        if not keep_running_ok(snapshot, n, config) or thermal_stop(snapshot, n, config):
            action = PDC_ACTION_STOP
            reason = PDC_REASON_STOP
            reason_stop = stop_reason(snapshot, n, config)
        elif snapshot.pdc_setpoint != setpoint:
            action = PDC_ACTION_SET_SETPOINT
            reason = PDC_REASON_SETPOINT
        else:
            reason = PDC_REASON_RUNNING
    elif not ready:
        reason = PDC_REASON_MANAGED_UNKNOWN if snapshot.managed != STATE_OFF else PDC_REASON_NOT_READY
    elif not edge:
        reason = PDC_REASON_EDGE_CONSUMED if consumed else PDC_REASON_STABILITY_WAIT
    elif start_conditions_ok(snapshot, n, config):
        action = PDC_ACTION_START
        reason = PDC_REASON_START
    else:
        reason = PDC_REASON_START_BLOCKED

    deadline = _next_deadline(snapshot, n, config, ready_since if ready and not next_consumed else None)
    conditions = _build_conditions(snapshot, n, config, codes, ready_since, next_consumed, setpoint)
    return PdcDecision(
        action=action,
        reason=reason,
        evaluated_at=n,
        context=next_context,
        deadline=deadline,
        diagnostic_codes=codes,
        diagnostic_state=diagnostic_state(codes, config),
        setpoint=setpoint if action == PDC_ACTION_SET_SETPOINT or snapshot.managed == STATE_ON else None,
        stop_reason=reason_stop,
        conditions=conditions,
    )


def _next_deadline(snapshot: PdcSnapshot, n: float, config: PdcPolicyConfig, ready_since: float | None) -> float | None:
    """Earliest future instant at which a time comparison may change.

    For strict comparisons (window grace) the change happens just after the
    returned instant; the runtime re-evaluates at or after it.
    """
    candidates: list[float] = []

    def add(value: float | None) -> None:
        if value is not None and value > n:
            candidates.append(value)

    if snapshot.temperature_reported_at is not None:
        add(snapshot.temperature_reported_at + config.temperature_max_age_s)
    if snapshot.power_reported_at is not None:
        add(snapshot.power_reported_at + config.power_max_age_s)
    if snapshot.managed == STATE_ON:
        if snapshot.window == STATE_ON and snapshot.window_changed_at is not None:
            add(snapshot.window_changed_at + config.window_running_grace_s)
        if snapshot.cycle.mode == STATE_HEAT and snapshot.cycle.on is not None:
            add(snapshot.cycle.on + config.min_on_s)
    else:
        if ready_since is not None:
            add(ready_since + config.stability_s)
        if snapshot.cycle.off is not None:
            add(snapshot.cycle.off + config.off_cooldown_s)
        for changed in (snapshot.split_changed_at, snapshot.managed_changed_at):
            if changed is not None:
                add(changed + config.raw_guard_s)
        schedule = schedule_value(snapshot)
        if schedule is not None:
            add(schedule - config.schedule_horizon_s)
    return min(candidates) if candidates else None


def _build_conditions(
    snapshot: PdcSnapshot,
    n: float,
    config: PdcPolicyConfig,
    codes: tuple[str, ...],
    ready_since: float | None,
    consumed: bool,
    setpoint: float | None,
) -> tuple[PdcCondition, ...]:
    """Informational conditions; start requirements are ``na`` while running and vice versa."""
    conditions: list[PdcCondition] = []
    running = snapshot.managed == STATE_ON

    def add(
        cid: str, status: ConditionStatus, reason_code: str | None = None, actual: Any = None, expected: Any = None, deadline: float | None = None, unit: str | None = None
    ) -> None:
        conditions.append(PdcCondition(cid, status, reason_code, actual, expected, deadline, unit))

    def boolean(cid: str, ok: bool, code: str, actual: Any, expected: Any, *, unknown: bool = False, unit: str | None = None, deadline: float | None = None) -> None:
        if ok:
            add(cid, CONDITION_PASS, None, actual, expected, deadline, unit)
        else:
            add(cid, CONDITION_UNKNOWN if unknown else CONDITION_FAIL, code, actual, expected, deadline, unit)

    override = snapshot.override
    boolean("override", override == STATE_OFF, CODE_MANUAL_OVERRIDE if override == STATE_ON else CODE_DATA_MISSING, override, STATE_OFF, unknown=override != STATE_ON)
    boolean("managed", snapshot.managed in (STATE_ON, STATE_OFF), CODE_DATA_MISSING, snapshot.managed, "on|off", unknown=True)
    boolean(
        "automatic",
        snapshot.automatic == STATE_ON,
        CODE_AUTOMATIC_OFF if snapshot.automatic == STATE_OFF else CODE_DATA_MISSING,
        snapshot.automatic,
        STATE_ON,
        unknown=not has_value(snapshot.automatic),
    )
    boolean("program", snapshot.requested_hvac_mode == STATE_HEAT, CODE_ROOM_OFF, snapshot.requested_hvac_mode, STATE_HEAT)
    boolean("preset", preset_ok(snapshot, config), CODE_PRESET, snapshot.preset_mode, "|".join(config.allowed_presets))
    boolean(
        "central_mode",
        snapshot.central_mode in config.allowed_central_modes,
        CODE_CENTRAL_MODE,
        snapshot.central_mode,
        "|".join(config.allowed_central_modes),
        unknown=not has_value(snapshot.central_mode),
    )
    temp_age = _age(n, snapshot.temperature_reported_at)
    temp_fresh = snapshot.temperature is not None and snapshot.target is not None and temp_age <= config.temperature_max_age_s
    boolean("temperature", temp_fresh, CODE_TEMPERATURE_MISSING, temp_age, f"<={_format_number(config.temperature_max_age_s)}", unknown=True, unit="s")
    power_age = _age(n, snapshot.power_reported_at)
    boolean("power_data", _power_fresh(snapshot, n, config), CODE_POWER_MISSING, power_age, f"<={_format_number(config.power_max_age_s)}", unknown=True, unit="s")

    gap = None if snapshot.temperature is None or snapshot.target is None else snapshot.target - snapshot.temperature
    if running:
        for cid, unit in (
            ("thermal_gap", "°C"),
            ("power_capacity", "W"),
            ("window_closed", None),
            ("other_pdc", None),
            ("split_free", None),
            ("raw_guards", "s"),
            ("schedule", "s"),
            ("cooldown", "s"),
            ("stability", "s"),
        ):
            add(cid, CONDITION_NA, unit=unit)
        boolean("window_running", window_running_ok(snapshot, n, config), CODE_WINDOW, snapshot.window, "off|on<=grace", unknown=not has_value(snapshot.window))
        boolean(
            "power_stop",
            snapshot.power is not None and snapshot.power < config.stop_power_w,
            CODE_POWER_HIGH,
            snapshot.power,
            f"<{_format_number(config.stop_power_w)}",
            unknown=snapshot.power is None,
            unit="W",
        )
        boolean(
            "split_heat",
            snapshot.pdc_mode == STATE_HEAT and snapshot.split_mode == STATE_HEAT,
            CODE_SPLIT_BUSY,
            snapshot.split_mode,
            STATE_HEAT,
            unknown=not has_value(snapshot.split_mode),
        )
        stop_now = thermal_stop(snapshot, n, config)
        boolean("thermal_stop", not stop_now, CODE_AT_TARGET, snapshot.temperature, None, unit="°C")
        done = minimum_on_done(snapshot, n, config)
        min_on_deadline = None if snapshot.cycle.on is None else snapshot.cycle.on + config.min_on_s
        add(
            "min_on",
            CONDITION_PASS if done else CONDITION_WAIT,
            None,
            None if snapshot.cycle.on is None else n - snapshot.cycle.on,
            f">={_format_number(config.min_on_s)}",
            min_on_deadline,
            "s",
        )
        boolean("setpoint", snapshot.pdc_setpoint == setpoint, CODE_DATA_MISSING, snapshot.pdc_setpoint, setpoint, unit="°C")
        return tuple(conditions)

    for cid, unit in (("window_running", None), ("power_stop", "W"), ("split_heat", None), ("thermal_stop", "°C"), ("min_on", "s"), ("setpoint", "°C")):
        add(cid, CONDITION_NA, unit=unit)
    if gap is None:
        add("thermal_gap", CONDITION_UNKNOWN, CODE_TEMPERATURE_MISSING, None, f">={config.start_gap_c}", unit="°C")
    else:
        boolean("thermal_gap", _gap_ok(snapshot, config), CODE_SMALL_GAP, gap, f">={config.start_gap_c}", unit="°C")
    capacity_expected = f"<={_format_number(config.power_budget_w - config.power_reserve_w)}"
    if snapshot.power is None:
        add("power_capacity", CONDITION_UNKNOWN, CODE_POWER_MISSING, None, capacity_expected, unit="W")
    else:
        boolean("power_capacity", _capacity_ok(snapshot, n, config), CODE_POWER_HIGH if snapshot.power > 0 else CODE_POWER_MISSING, snapshot.power, capacity_expected, unit="W")
    boolean(
        "window_closed",
        snapshot.window == STATE_OFF,
        CODE_WINDOW if snapshot.window == STATE_ON else CODE_DATA_MISSING,
        snapshot.window,
        STATE_OFF,
        unknown=snapshot.window != STATE_ON,
    )
    other_known = has_value(snapshot.other_managed) and has_value(snapshot.other_split_mode)
    boolean(
        "other_pdc",
        other_pdc_free(snapshot),
        CODE_OTHER_PDC if other_known else CODE_DATA_MISSING,
        [snapshot.other_managed, snapshot.other_split_mode],
        "off,off",
        unknown=not other_known,
    )
    boolean(
        "split_free",
        snapshot.split_mode == STATE_OFF and snapshot.pdc_mode == STATE_OFF and snapshot.radiator_mode == STATE_HEAT,
        CODE_SPLIT_BUSY,
        [snapshot.split_mode, snapshot.pdc_mode, snapshot.radiator_mode],
        "off,off,heat",
        unknown=not (has_value(snapshot.split_mode) and has_value(snapshot.pdc_mode)),
    )
    raw_age = min(_age(n, snapshot.split_changed_at), _age(n, snapshot.managed_changed_at))
    raw_ok = raw_age >= config.raw_guard_s
    raw_deadline = (
        max(0.0 if snapshot.split_changed_at is None else snapshot.split_changed_at, 0.0 if snapshot.managed_changed_at is None else snapshot.managed_changed_at)
        + config.raw_guard_s
    )
    add(
        "raw_guards",
        CONDITION_PASS if raw_ok else CONDITION_WAIT,
        None if raw_ok else CODE_RAW_GUARD,
        raw_age,
        f">={_format_number(config.raw_guard_s)}",
        None if raw_ok else raw_deadline,
        "s",
    )
    schedule = schedule_value(snapshot)
    if snapshot.schedule_stop == SCHEDULE_NONE:
        add("schedule", CONDITION_PASS, None, SCHEDULE_NONE, f">{_format_number(config.schedule_horizon_s)}", None, "s")
    elif schedule is None:
        add("schedule", CONDITION_UNKNOWN, CODE_DATA_MISSING, snapshot.schedule_stop, f">{_format_number(config.schedule_horizon_s)}", None, "s")
    else:
        boolean("schedule", schedule_ok(snapshot, n, config), CODE_SCHEDULE_CHANGE, schedule - n, f">{_format_number(config.schedule_horizon_s)}", unit="s")
    stopped = snapshot.cycle.off
    if not snapshot.cycle_available or stopped is None or snapshot.cycle.mode != STATE_OFF:
        add(
            "cooldown",
            CONDITION_UNKNOWN if stopped is None else CONDITION_FAIL,
            CODE_COOLDOWN,
            None if stopped is None else n - stopped,
            f">={_format_number(config.off_cooldown_s)}",
            None,
            "s",
        )
    else:
        ok = cooldown_ok(snapshot, n, config)
        add(
            "cooldown",
            CONDITION_PASS if ok else CONDITION_WAIT,
            None if ok else CODE_COOLDOWN,
            n - stopped,
            f">={_format_number(config.off_cooldown_s)}",
            None if ok else stopped + config.off_cooldown_s,
            "s",
        )
    if codes:
        add("stability", CONDITION_NA, None, None, f">={_format_number(config.stability_s)}", None, "s")
    elif ready_since is not None and n >= ready_since + config.stability_s:
        add(
            "stability",
            CONDITION_PASS if not consumed or not config.start_edge_one_shot else CONDITION_FAIL,
            CODE_EDGE_CONSUMED if consumed else None,
            n - ready_since,
            f">={_format_number(config.stability_s)}",
            None,
            "s",
        )
    else:
        stability_deadline = None if ready_since is None else ready_since + config.stability_s
        add("stability", CONDITION_WAIT, None, None if ready_since is None else n - ready_since, f">={_format_number(config.stability_s)}", stability_deadline, "s")
    return tuple(conditions)
