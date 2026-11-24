"""Pure model of the manual-override provenance of a PDC split (M5).

Extracted from the baseline automations ``*_registro_comandi_condizionatore``
(expected-effects register), ``*_override_manuale_condizionatore`` (detection)
and ``*_fine_override_condizionatore`` (expiry). No Home Assistant import, no
I/O, explicit clock.

Rules preserved:

* the register stores, **per field** (mode ``m``, temperature ``t``, fan ``f``,
  preset ``p``, swing ``s``), the exact expected value and an expiry 75 s after
  the HA command. Only an exact value before its expiry is an expected effect;
  there is no blind window for other changes;
* the register also records the effects that SmartThings produces on its own:
  ``set_preset_mode quiet`` brings the fan to ``auto``; OFF (``set_hvac_mode off``
  or ``turn_off``) brings the preset to ``none``;
* a climate service call with an authenticated Home Assistant user on the split
  or on the PDC VTherm is a manual command and always wins;
* a device change whose fields do not match an unexpired expected value is manual
  but its author is **not known** (the SmartThings remote controller is not
  identified): the provenance stays ``unknown`` and is never promoted to ``user``;
* changes from/to ``unknown``/``unavailable`` and plain confirmations are not manual;
* the override lasts 120 minutes and is renewed by each new manual event; it
  ends when the stored end instant is reached (``end <= now``), like the baseline.

Time comparisons mirror the templates: an expectation is valid while
``expires_at >= now``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Final, Mapping

EXPECTED_WINDOW_S: Final = 75.0
OVERRIDE_DURATION_S: Final = 7200.0

FIELD_MODE: Final = "m"
FIELD_TEMPERATURE: Final = "t"
FIELD_FAN: Final = "f"
FIELD_PRESET: Final = "p"
FIELD_SWING: Final = "s"
FIELDS: Final = (FIELD_MODE, FIELD_TEMPERATURE, FIELD_FAN, FIELD_PRESET, FIELD_SWING)
ATTRIBUTE_OF: Final = {FIELD_TEMPERATURE: "temperature", FIELD_FAN: "fan_mode", FIELD_PRESET: "preset_mode", FIELD_SWING: "swing_mode"}

REGISTERED_SERVICES: Final = ("set_hvac_mode", "set_temperature", "set_fan_mode", "set_preset_mode", "set_swing_mode", "turn_off", "turn_on")
RECORDED_MODES: Final = ("off", "heat", "cool", "auto", "dry", "fan_only", "heat_cool")

PROVENANCE_EXPECTED: Final = "expected_effect"
PROVENANCE_USER: Final = "user"
PROVENANCE_UNKNOWN: Final = "unknown"
PROVENANCE_NONE: Final = "none"

SOURCE_USER_COMMAND: Final = "user_command"
SOURCE_DEVICE_CHANGE: Final = "device_change"
SOURCE_DEVICE_FOUND_RUNNING: Final = "device_found_running"


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


@dataclass(frozen=True, slots=True)
class ExpectedEffect:
    """One expected value of one field with its expiry (absolute Unix seconds)."""

    value: Any
    expires_at: float

    def __post_init__(self) -> None:
        """Validate the expiry."""
        _finite("expires_at", self.expires_at)


@dataclass(frozen=True, slots=True)
class ExpectedRegister:
    """Per-field expected effects (``input_text.<room>_pdc_comandi_attesi``)."""

    effects: Mapping[str, ExpectedEffect] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Keep only known fields and freeze a copy of the mapping."""
        object.__setattr__(self, "effects", {k: v for k, v in dict(self.effects).items() if k in FIELDS and isinstance(v, ExpectedEffect)})


EMPTY_REGISTER: Final = ExpectedRegister()


def register_command(register: ExpectedRegister, service: str, data: Mapping[str, Any] | None, now: float, window_s: float = EXPECTED_WINDOW_S) -> ExpectedRegister:
    """Record the expected effects of a climate service call on the split (every HA command, user or automation)."""
    n = _finite("now", now)
    if service not in REGISTERED_SERVICES:
        return register
    d = data or {}
    expires = float(int(n + window_s))
    out = dict(register.effects)
    if "hvac_mode" in d or service in ("turn_off", "turn_on"):
        out[FIELD_MODE] = ExpectedEffect(d.get("hvac_mode", "off" if service == "turn_off" else "heat"), expires)
    if "temperature" in d:
        try:
            out[FIELD_TEMPERATURE] = ExpectedEffect(float(d["temperature"]), expires)
        except (TypeError, ValueError):
            out.pop(FIELD_TEMPERATURE, None)
    if "fan_mode" in d:
        out[FIELD_FAN] = ExpectedEffect(d["fan_mode"], expires)
    elif service == "set_preset_mode" and d.get("preset_mode") == "quiet":
        out[FIELD_FAN] = ExpectedEffect("auto", expires)
    if "preset_mode" in d:
        out[FIELD_PRESET] = ExpectedEffect(d["preset_mode"], expires)
    elif d.get("hvac_mode") == "off" or service == "turn_off":
        out[FIELD_PRESET] = ExpectedEffect("none", expires)
    if "swing_mode" in d:
        out[FIELD_SWING] = ExpectedEffect(d["swing_mode"], expires)
    return ExpectedRegister(out)


@dataclass(frozen=True, slots=True)
class DeviceState:
    """State of the physical split as seen by one state-changed event."""

    state: str | None
    temperature: float | None = None
    fan_mode: str | None = None
    preset_mode: str | None = None
    swing_mode: str | None = None

    def value(self, key: str) -> Any:
        """Value of a register field."""
        return self.state if key == FIELD_MODE else getattr(self, ATTRIBUTE_OF[key])


@dataclass(frozen=True, slots=True)
class ChangeVerdict:
    """Classification of one device change."""

    changed: tuple[str, ...]
    manual_fields: tuple[str, ...]
    expected_fields: tuple[str, ...]
    provenance: str
    manual: bool


def classify_device_change(register: ExpectedRegister, old: DeviceState | None, new: DeviceState | None, now: float) -> ChangeVerdict:
    """Compare a state change with the expected effects.

    Not evaluated (empty ``changed``) when either side is missing or not a
    recorded HVAC mode: reconnection and restore are handled by
    :func:`device_found_running`. A field is expected only if its exact new value
    matches an effect with ``expires_at >= now``.
    """
    n = _finite("now", now)
    none = ChangeVerdict((), (), (), PROVENANCE_NONE, False)
    if old is None or new is None or old.state not in RECORDED_MODES or new.state not in RECORDED_MODES:
        return none
    changed = tuple(key for key in FIELDS if old.value(key) != new.value(key))
    if not changed:
        return none
    manual: list[str] = []
    expected: list[str] = []
    for key in changed:
        effect = register.effects.get(key)
        if effect is not None and effect.value == new.value(key) and not effect.expires_at < n:
            expected.append(key)
        else:
            manual.append(key)
    if manual:
        # Author not identifiable from the device event: never promote to ``user``.
        return ChangeVerdict(changed, tuple(manual), tuple(expected), PROVENANCE_UNKNOWN, True)
    return ChangeVerdict(changed, (), tuple(expected), PROVENANCE_EXPECTED, False)


def device_found_running(old_state: str | None, new_state: str | None, *, override_off: bool | None, managed_off: bool | None) -> bool:
    """Reconnection/restore: the split reappears active while neither override nor automatic management exist.

    The baseline starts an override only if the previous state was missing or
    unknown/unavailable, the override and the managed flag are ``off`` and the new
    state is an active mode. Otherwise the existing override is preserved without
    renewing it.
    """
    came_back = old_state is None or old_state in ("unknown", "unavailable")
    return came_back and new_state in RECORDED_MODES and new_state != "off" and override_off is True and managed_off is True


def is_user_command(user_id: str | None, service: str, targets: Any, watched: tuple[str, ...]) -> bool:
    """Authenticated HA user calling a climate service on one of the watched entities (split or PDC VTherm)."""
    ids = [targets] if isinstance(targets, str) else (list(targets) if isinstance(targets, (list, tuple)) else [])
    return user_id is not None and service in REGISTERED_SERVICES and any(entity in ids for entity in watched)


@dataclass(frozen=True, slots=True)
class OverrideState:
    """Manual override of one split: active flag, last start and end instants, and the source of the last event."""

    active: bool = False
    started_at: float | None = None
    until: float | None = None
    source: str | None = None
    provenance: str = PROVENANCE_NONE

    def __post_init__(self) -> None:
        """Validate the state."""
        for name in ("started_at", "until"):
            value = getattr(self, name)
            if value is not None:
                _finite(name, value)


def start_override(state: OverrideState, now: float, source: str, provenance: str, duration_s: float = OVERRIDE_DURATION_S) -> OverrideState:
    """Start or renew (120 minutes from ``now``). A known provenance is not downgraded to unknown, nor unknown promoted."""
    n = _finite("now", now)
    kept = state.provenance if state.active and provenance == PROVENANCE_UNKNOWN and state.provenance == PROVENANCE_USER else provenance
    return OverrideState(True, state.started_at if state.active and state.started_at is not None else n, n + duration_s, source, kept)


def override_expired(state: OverrideState, now: float) -> bool:
    """End instant reached (``until <= now``). A missing end of an active override counts as reached, like ``as_timestamp(.., 0)``."""
    n = _finite("now", now)
    return state.active and (0.0 if state.until is None else state.until) <= n


def end_override(state: OverrideState, now: float) -> OverrideState:
    """Return to automatic: clears the override (manual resume or expiry)."""
    _finite("now", now)
    return OverrideState()


def restore_override(state: OverrideState, now: float) -> OverrideState:
    """After a restart: keep the stored end instant (no renewal); expire it if already past."""
    return end_override(state, now) if override_expired(state, now) else state
