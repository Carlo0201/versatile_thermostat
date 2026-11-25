"""Pure policy of the timed room override (the weekly program is paused for a while).

It reproduces what the Oikos room cards and the automation
``oikos_riprendi_programma_riscaldamento_<room>`` do today, without any helper:

* the *program* of a room is a set of Scheduler switches (``switch.schedule_*``):
  all on = program, all off = manual;
* an *override* switches them off and remembers when to give the program back;
* at the end the switches are turned on again and the slot in progress is applied, because
  Scheduler applies a preset only when a time slot starts. The programs that apply today are the
  ones whose ``weekdays`` contain ``daily``, today's weekday, ``workday`` on a working day or
  ``weekend`` on a non working day (the rule of the blueprint). An unknown working-day sensor
  falls back to Monday-Friday.

No Home Assistant import, no I/O, no clock, no asyncio: the caller passes ``now`` (epoch seconds,
so a DST change cannot move an expiry) and the local weekday index.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Final

STORE_SCHEMA_VERSION: Final = 1

MIN_DURATION_MIN: Final = 1.0
MAX_DURATION_MIN: Final = 1440.0

SWITCH_ON: Final = "on"
SWITCH_OFF: Final = "off"
SCHEDULER_ENABLED: Final = (SWITCH_ON, "triggered")
SCHEDULER_KNOWN: Final = (*SCHEDULER_ENABLED, SWITCH_OFF)

MODE_NONE: Final = "none"  # the room has no program switch
MODE_PROGRAM: Final = "program"
MODE_OVERRIDE: Final = "override"
MODE_MANUAL: Final = "manual"
MODE_PARTIAL: Final = "partial"
MODE_UNKNOWN: Final = "unknown"  # a switch is unknown/unavailable: never read as on or off

WEEKDAY_NAMES: Final = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True, slots=True)
class OverrideState:
    """Persistent state of one room.

    ``ending`` is set before the end sequence starts and cleared only when every step is
    confirmed, so a restart in the middle resumes the sequence instead of forgetting it.
    """

    active: bool = False
    started_at: float | None = None
    ends_at: float | None = None
    duration_min: float | None = None
    ending: bool = False
    attempts: int = 0
    pausing: bool = False
    restored: bool = False
    end_reason: str | None = None
    paused_schedulers: tuple[str, ...] = ()
    resumed_actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("started_at", "ends_at", "duration_min"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise ValueError(f"{name} must be a finite number")
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int) or self.attempts < 0:
            raise ValueError("attempts must be a non-negative integer")
        if self.active and (self.started_at is None or self.ends_at is None or self.duration_min is None):
            raise ValueError("an active override needs started_at, ends_at and duration_min")
        if self.ending and not self.active:
            raise ValueError("only an active override can be ending")
        if self.pausing and (not self.active or self.ending):
            raise ValueError("only an active override that is not ending can be pausing")
        if self.restored and not self.ending:
            raise ValueError("only an ending override can have restored the program")
        if self.end_reason is not None and (not self.ending or self.end_reason not in ("cancelled", "expired", "externally_resumed")):
            raise ValueError("invalid end reason")


IDLE: Final = OverrideState()


def normalise_duration(minutes: object) -> float:
    """A usable duration in minutes, or ``ValueError`` (never a silent default)."""
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not math.isfinite(minutes):
        raise ValueError("duration must be a finite number of minutes")
    if minutes < MIN_DURATION_MIN or minutes > MAX_DURATION_MIN:
        raise ValueError(f"duration must be between {MIN_DURATION_MIN:g} and {MAX_DURATION_MIN:g} minutes")
    return float(minutes)


def start_override(state: OverrideState, now: float, minutes: object) -> OverrideState:
    """Start, or restart with the new duration (like ``timer.start`` on a running timer)."""
    if not math.isfinite(now):
        raise ValueError("now must be finite")
    duration = normalise_duration(minutes)
    return OverrideState(
        active=True, started_at=now if not state.active or state.started_at is None else state.started_at, ends_at=now + duration * 60.0, duration_min=duration, pausing=True
    )


def begin_ending(state: OverrideState, reason: str = "expired") -> OverrideState:
    """Mark the end sequence as started (persisted before the first command)."""
    return replace(state, ending=True, pausing=False, end_reason=reason) if state.active else state


def failed_attempt(state: OverrideState) -> OverrideState:
    """One more failed attempt of the end sequence."""
    return replace(state, attempts=state.attempts + 1) if state.active else state


def is_due(state: OverrideState, now: float) -> bool:
    """The override ended, or its end sequence is already in progress."""
    return state.active and (state.ending or (state.ends_at is not None and state.ends_at <= now))


def remaining_s(state: OverrideState, now: float) -> float | None:
    """Seconds left, never negative; ``None`` when no override is active."""
    if not state.active or state.ends_at is None:
        return None
    return max(0.0, state.ends_at - now)


def program_mode(switch_states: Sequence[str | None], override_active: bool) -> str:
    """The mode of a room from its program switches, with the override first.

    An unrecognized switch state (unknown, unavailable, missing) makes the mode
    ``unknown`` unless an override is running: the override is our own persistent fact.
    """
    if not switch_states:
        return MODE_NONE
    if override_active:
        return MODE_OVERRIDE
    if any(state not in SCHEDULER_KNOWN for state in switch_states):
        return MODE_UNKNOWN
    on = sum(1 for state in switch_states if state in SCHEDULER_ENABLED)
    if on == len(switch_states):
        return MODE_PROGRAM
    return MODE_PARTIAL if on else MODE_MANUAL


def program_externally_resumed(switch_states: Sequence[str | None]) -> bool:
    """Every switch is on again: somebody gave the program back during the override."""
    return bool(switch_states) and all(state in SCHEDULER_ENABLED for state in switch_states)


def program_applies_today(weekdays: Iterable[str] | None, weekday: int, workday_state: str | None) -> bool:
    """Whether a Scheduler program applies today (the rule of the blueprint).

    ``weekday`` is 0 for Monday in the **local** time zone. ``workday_state`` is the state of the
    working-day sensor; anything but ``on``/``off`` falls back to Monday-Friday.
    """
    if isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 6:
        raise ValueError("weekday must be 0..6")
    days = {str(day).lower() for day in (weekdays or ())}
    is_workday = (workday_state == "on") if workday_state in ("on", "off") else weekday < 5
    return "daily" in days or WEEKDAY_NAMES[weekday] in days or ("workday" in days and is_workday) or ("weekend" in days and not is_workday)


def programs_to_run(programs: Sequence[tuple[str, Iterable[str] | None]], weekday: int, workday_state: str | None) -> tuple[str, ...]:
    """Entity ids whose current slot must be applied now, in configuration order."""
    return tuple(entity_id for entity_id, weekdays in programs if program_applies_today(weekdays, weekday, workday_state))
