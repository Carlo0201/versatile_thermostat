"""Pure decision policy for the central boiler request.

This module is the M1 extraction of the decision block that currently lives in the
Home Assistant automation ``automation.caldaia_controllo_stabile_delle_richieste``
(template ``plan``). It is deliberately pure: no Home Assistant import, no I/O, no
global clock, no asyncio and no service call. The same inputs always produce the
same decision.

The input snapshot is already normalized by the future collector. In particular
``gap`` is expected to be rounded to three decimals like the baseline template,
and ``count`` is the number of distinct valid physical valves, not the number of
rooms. Every room listed in the snapshot is considered to have at least one
active physical valve; the thermal gap threshold is applied here.

Baseline quirks are preserved on purpose and must not be silently corrected
during the migration:

* the normal path (two or more valves) does not require ``pilot_ok`` on any
  room, while the single-valve path does;
* the single-valve path does not require the native consent;
* the manual request bypasses cooldown and native consent, but not input or
  central validity;
* stability and cooldown anchors are truncated to whole seconds in the stored
  cycle, like the Jinja ``|int`` filter;
* ``r`` is cleared whenever the normal path is not applicable, including while
  the master is already in ``heat``;
* a non-finite or unparsable stored anchor is treated as missing (a deliberate
  hardening over ``float(nan)``, which would leak NaN into the decision).

The structured ``conditions`` tuple is an interface proposal for the M2
diagnostic contract. It is informational only: the policy never reads it.
Conditions are assigned after selecting the route of the decision
(maintenance, manual, automatic, master unavailable): requirements of another
route are reported as ``na`` instead of as blockers. The room-level pilot veto
of the single path (``single_room_pilot``) is reported separately from the
global pilot readiness (``pilot``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Mapping, Sequence

BOILER_ACTION_NONE: Final = "none"
BOILER_ACTION_HEAT: Final = "heat"
BOILER_ACTION_OFF: Final = "off"
BoilerAction = Literal["none", "heat", "off"]

BOILER_REASON_NO_DEMAND: Final = "no_demand"
BOILER_REASON_MAINTAIN: Final = "maintain"
BOILER_REASON_NO_VALID_RADIATOR: Final = "no_valid_radiator"
BOILER_REASON_MANUAL: Final = "manual"
BOILER_REASON_NORMAL_WAIT: Final = "normal_wait"
BOILER_REASON_NORMAL: Final = "normal"
BOILER_REASON_AWAITING_CONSENT: Final = "awaiting_consent"
BOILER_REASON_SINGLE_WAIT: Final = "single_wait"
BOILER_REASON_SINGLE: Final = "single"
BOILER_REASON_THERMAL_WAIT: Final = "thermal_wait"
BOILER_REASON_DATA_OR_CENTRAL: Final = "data_or_central"
BOILER_REASON_DATA_MISSING: Final = "data_missing"
BOILER_REASON_MASTER_UNAVAILABLE: Final = "master_unavailable"

BOILER_MASTER_OFF: Final = "off"
BOILER_MASTER_HEAT: Final = "heat"
BOILER_MANUAL_ON: Final = "on"
BOILER_MANUAL_OFF: Final = "off"
BOILER_PILOT_ON: Final = "on"
BOILER_PILOT_OFF: Final = "off"

DEFAULT_THERMAL_GAP_C: Final = 0.3
DEFAULT_NORMAL_VALVE_COUNT: Final = 2
DEFAULT_NORMAL_STABILITY_S: Final = 120.0
DEFAULT_SINGLE_STABILITY_S: Final = 900.0
DEFAULT_OFF_COOLDOWN_S: Final = 600.0

ConditionStatus = Literal["pass", "fail", "wait", "unknown", "na"]
CONDITION_PASS: Final = "pass"
CONDITION_FAIL: Final = "fail"
CONDITION_WAIT: Final = "wait"
CONDITION_UNKNOWN: Final = "unknown"
CONDITION_NA: Final = "na"

# Diagnostic-only reason code: the single qualified room is vetoed by the pilot
# (``pilot_ok`` false) while the global pilot readiness may be satisfied.
CONDITION_REASON_SINGLE_ROOM_PILOT_VETO: Final = "single_room_pilot_veto"

# Diagnostic routes, mirroring the branches of the decision. They select which
# conditions apply; they are never read by the policy.
_ROUTE_MAINTAIN: Final = "maintain"
_ROUTE_MANUAL: Final = "manual"
_ROUTE_AUTOMATIC: Final = "automatic"
_ROUTE_MASTER_UNAVAILABLE: Final = "master_unavailable"


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


def _coerce_timestamp(value: Any) -> float | None:
    """Coerce a stored cycle timestamp like the Jinja ``float(none)`` filter.

    Booleans, non-finite values and unparsable strings return ``None`` so that a
    corrupt anchor is handled as missing instead of propagating NaN/Infinity.
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


@dataclass(frozen=True, slots=True)
class BoilerPolicyConfig:
    """Validated parameters of the boiler policy.

    The defaults reproduce the observed baseline. Room/valve associations and
    entity ids must stay outside this model.
    """

    thermal_gap_c: float = DEFAULT_THERMAL_GAP_C
    normal_valve_count: int = DEFAULT_NORMAL_VALVE_COUNT
    normal_stability_s: float = DEFAULT_NORMAL_STABILITY_S
    single_stability_s: float = DEFAULT_SINGLE_STABILITY_S
    off_cooldown_s: float = DEFAULT_OFF_COOLDOWN_S

    def __post_init__(self) -> None:
        """Reject parameters that would make the decision non-deterministic."""
        _finite_number("thermal_gap_c", self.thermal_gap_c, minimum=0.0)
        if isinstance(self.normal_valve_count, bool) or not isinstance(self.normal_valve_count, int):
            raise TypeError("normal_valve_count must be an integer")
        if self.normal_valve_count < 2:
            raise ValueError("normal_valve_count must be >= 2")
        _finite_number("normal_stability_s", self.normal_stability_s, minimum=0.0)
        _finite_number("single_stability_s", self.single_stability_s, minimum=0.0)
        _finite_number("off_cooldown_s", self.off_cooldown_s, minimum=0.0)


DEFAULT_CONFIG: Final = BoilerPolicyConfig()


@dataclass(frozen=True, slots=True)
class QualifiedBoilerRoom:
    """A room that already has at least one active physical valve.

    ``gap`` is the target-to-current difference rounded to three decimals by the
    collector. ``target`` is used only to decide whether the single-valve wait
    still refers to the same request.
    """

    slug: str
    target: float
    gap: float
    pilot_ok: bool

    def __post_init__(self) -> None:
        """Validate the room entry."""
        if not isinstance(self.slug, str) or not self.slug:
            raise ValueError("slug must be a non-empty string")
        _finite_number("target", self.target)
        _finite_number("gap", self.gap)
        if not isinstance(self.pilot_ok, bool):
            raise TypeError("pilot_ok must be a boolean")


@dataclass(frozen=True, slots=True)
class BoilerSnapshot:
    """Normalized inputs of one evaluation.

    ``master``, ``manual`` and ``pilot`` keep ``None`` for an absent value. They
    are never coerced to ``off``: a missing value is not a negative request.
    """

    ok: bool
    master: str | None
    count: int
    rooms: tuple[QualifiedBoilerRoom, ...] = ()
    manual: str | None = None
    pilot: str | None = None
    pilot_ready: bool | None = None
    consent: bool | None = None

    def __post_init__(self) -> None:
        """Validate and freeze the snapshot without touching the caller data."""
        if not isinstance(self.ok, bool):
            raise TypeError("ok must be a boolean")
        if self.master is not None and not isinstance(self.master, str):
            raise TypeError("master must be a string or None")
        if isinstance(self.count, bool) or not isinstance(self.count, int):
            raise TypeError("count must be an integer")
        if self.count < 0:
            raise ValueError("count must be >= 0")
        if isinstance(self.rooms, (str, bytes)) or not isinstance(self.rooms, Sequence):
            raise TypeError("rooms must be a sequence of QualifiedBoilerRoom")
        rooms = tuple(self.rooms)
        if any(not isinstance(room, QualifiedBoilerRoom) for room in rooms):
            raise TypeError("rooms must contain QualifiedBoilerRoom instances")
        object.__setattr__(self, "rooms", rooms)
        for name in ("manual", "pilot"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{name} must be a string or None")
        for name in ("pilot_ready", "consent"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise TypeError(f"{name} must be a boolean or None")


@dataclass(frozen=True, slots=True)
class BoilerSingleWait:
    """Anchor of the single-valve stability wait (``s`` in the stored cycle)."""

    slug: str
    target: float
    since: float | None

    def __post_init__(self) -> None:
        """Validate the anchor."""
        if not isinstance(self.slug, str) or not self.slug:
            raise ValueError("slug must be a non-empty string")
        _finite_number("target", self.target)
        if self.since is not None:
            _finite_number("since", self.since)


@dataclass(frozen=True, slots=True)
class BoilerCycle:
    """Logical cycle state persisted between evaluations.

    Times are absolute UTC/Unix seconds. Negative and future anchors are kept:
    the policy treats them as invalid and restarts them, exactly like the
    baseline template.
    """

    mode: str | None = None
    off_since: float | None = None
    normal_since: float | None = None
    single: BoilerSingleWait | None = None

    def __post_init__(self) -> None:
        """Validate the cycle."""
        if self.mode is not None and not isinstance(self.mode, str):
            raise TypeError("mode must be a string or None")
        for name in ("off_since", "normal_since"):
            value = getattr(self, name)
            if value is not None:
                _finite_number(name, value)
        if self.single is not None and not isinstance(self.single, BoilerSingleWait):
            raise TypeError("single must be a BoilerSingleWait or None")


EMPTY_CYCLE: Final = BoilerCycle()


@dataclass(frozen=True, slots=True)
class BoilerCondition:
    """One structured diagnostic condition (M2 contract proposal)."""

    id: str
    status: ConditionStatus
    reason_code: str | None = None
    actual: Any = None
    expected: Any = None
    deadline: float | None = None


@dataclass(frozen=True, slots=True)
class BoilerDecision:
    """Result of one evaluation.

    ``action`` and ``deadline`` are proposals. ``cycle`` is the state to persist
    for the next evaluation. A proposal does not change the observed master
    mode: ``cycle.mode`` still reflects what was sampled in the snapshot.
    """

    action: BoilerAction
    reason: str
    deadline: float | None
    cycle: BoilerCycle
    evaluated_at: float
    conditions: tuple[BoilerCondition, ...] = field(default_factory=tuple)


def parse_cycle_context(value: Any) -> BoilerCycle:
    """Build a cycle from a decoded JSON value.

    Missing, non-mapping, truncated or corrupt contexts produce an empty or
    partially empty cycle. This mirrors the baseline ``cycle if cycle is mapping
    else {}`` and its ``float(none)`` coercions, with the extra rule that
    non-finite numbers are treated as absent.
    """
    if not isinstance(value, Mapping):
        return EMPTY_CYCLE

    mode = value.get("m")
    if not isinstance(mode, str):
        mode = None

    single: BoilerSingleWait | None = None
    raw_single = value.get("s")
    if isinstance(raw_single, Sequence) and not isinstance(raw_single, (str, bytes)) and len(raw_single) == 3:
        slug, target, since = raw_single
        if isinstance(slug, str) and slug and isinstance(target, (int, float)) and not isinstance(target, bool):
            target_value = float(target)
            if math.isfinite(target_value):
                single = BoilerSingleWait(slug=slug, target=target_value, since=_coerce_timestamp(since))

    return BoilerCycle(
        mode=mode,
        off_since=_coerce_timestamp(value.get("off")),
        normal_since=_coerce_timestamp(value.get("r")),
        single=single,
    )


def evaluate_boiler(
    snapshot: BoilerSnapshot,
    cycle: BoilerCycle | None,
    now: float,
    config: BoilerPolicyConfig | None = None,
) -> BoilerDecision:
    """Evaluate the boiler policy at ``now`` (absolute Unix seconds).

    The function does not mutate ``snapshot``, ``cycle`` or ``config``. Passing
    ``None`` as cycle is equivalent to an empty context. The returned cycle
    contains whole-second anchors, as the baseline stores them.
    """
    if not isinstance(snapshot, BoilerSnapshot):
        raise TypeError("snapshot must be a BoilerSnapshot")
    if cycle is not None and not isinstance(cycle, BoilerCycle):
        raise TypeError("cycle must be a BoilerCycle or None")
    if config is None:
        config = DEFAULT_CONFIG
    elif not isinstance(config, BoilerPolicyConfig):
        raise TypeError("config must be a BoilerPolicyConfig or None")

    n = _finite_number("now", now)
    cycle = cycle if cycle is not None else EMPTY_CYCLE
    mode = snapshot.master
    saved_mode = cycle.mode
    stopped = cycle.off_since

    # An OFF observation without a valid OFF anchor starts a new cooldown now.
    if mode == BOILER_MASTER_OFF and (saved_mode != BOILER_MASTER_OFF or stopped is None or stopped < 0 or stopped > n):
        stopped = n

    usable = snapshot.ok and snapshot.count > 0 and mode in (BOILER_MASTER_OFF, BOILER_MASTER_HEAT)
    qualified = [room for room in snapshot.rooms if room.gap >= config.thermal_gap_c]
    automatic_ok = snapshot.pilot in (BOILER_PILOT_ON, BOILER_PILOT_OFF) and (snapshot.pilot == BOILER_PILOT_OFF or bool(snapshot.pilot_ready))

    normal = usable and snapshot.count >= config.normal_valve_count and len(qualified) > 0 and automatic_ok
    ready = cycle.normal_since if normal else None
    if normal and (ready is None or ready < 0 or ready > n):
        ready = n

    one = None
    if usable and snapshot.count == 1 and len(qualified) == 1 and automatic_ok and (snapshot.pilot == BOILER_PILOT_OFF or qualified[0].pilot_ok):
        one = qualified[0]

    prior = cycle.single
    same = one is not None and prior is not None and prior.slug == one.slug and prior.target == one.target
    single_since = prior.since if same else None
    if one is not None and (single_since is None or single_since < 0 or single_since > n):
        single_since = n

    action: BoilerAction = BOILER_ACTION_NONE
    reason = BOILER_REASON_NO_DEMAND
    deadline: float | None = None

    if mode == BOILER_MASTER_HEAT:
        reason = BOILER_REASON_MAINTAIN
        if not usable:
            action = BOILER_ACTION_OFF
            reason = BOILER_REASON_NO_VALID_RADIATOR
    elif mode == BOILER_MASTER_OFF:
        if usable and snapshot.manual == BOILER_MANUAL_ON:
            action = BOILER_ACTION_HEAT
            reason = BOILER_REASON_MANUAL
        elif usable and snapshot.manual == BOILER_MANUAL_OFF and automatic_ok and stopped is not None:
            cooldown = stopped + config.off_cooldown_s
            if normal:
                assert ready is not None  # normal always sets the stability anchor
                deadline = max(ready + config.normal_stability_s, cooldown)
                reason = BOILER_REASON_NORMAL_WAIT
                if n >= deadline and bool(snapshot.consent):
                    action = BOILER_ACTION_HEAT
                    reason = BOILER_REASON_NORMAL
                elif n >= deadline:
                    deadline = None
                    reason = BOILER_REASON_AWAITING_CONSENT
            elif one is not None:
                assert single_since is not None  # one always sets the single anchor
                deadline = max(single_since + config.single_stability_s, cooldown)
                reason = BOILER_REASON_SINGLE_WAIT
                if n >= deadline:
                    action = BOILER_ACTION_HEAT
                    reason = BOILER_REASON_SINGLE
            else:
                reason = BOILER_REASON_THERMAL_WAIT
        elif not snapshot.ok:
            reason = BOILER_REASON_DATA_OR_CENTRAL
        elif usable:
            reason = BOILER_REASON_DATA_MISSING
    else:
        reason = BOILER_REASON_MASTER_UNAVAILABLE

    next_single = None
    if one is not None:
        next_single = BoilerSingleWait(
            slug=one.slug,
            target=one.target,
            since=None if single_since is None else int(single_since),
        )

    next_cycle = BoilerCycle(
        mode=mode if mode in (BOILER_MASTER_OFF, BOILER_MASTER_HEAT) else saved_mode,
        off_since=None if stopped is None else int(stopped),
        normal_since=None if ready is None else int(ready),
        single=next_single,
    )

    conditions = _build_conditions(
        snapshot=snapshot,
        config=config,
        n=n,
        mode=mode,
        qualified=qualified,
        automatic_ok=automatic_ok,
        normal=normal,
        ready=ready,
        one=one,
        single_since=single_since,
        stopped=stopped,
        consent=snapshot.consent,
        reason=reason,
    )

    return BoilerDecision(
        action=action,
        reason=reason,
        deadline=deadline,
        cycle=next_cycle,
        evaluated_at=n,
        conditions=conditions,
    )


def _build_conditions(
    *,
    snapshot: BoilerSnapshot,
    config: BoilerPolicyConfig,
    n: float,
    mode: str | None,
    qualified: Sequence[QualifiedBoilerRoom],
    automatic_ok: bool,
    normal: bool,
    ready: float | None,
    one: QualifiedBoilerRoom | None,
    single_since: float | None,
    stopped: float | None,
    consent: bool | None,
    reason: str,
) -> tuple[BoilerCondition, ...]:
    """Build informational conditions for the future diagnostic contract.

    The route is determined first, with the same branch order as the decision:
    master HEAT is maintenance, a master other than OFF/HEAT is unavailable,
    master OFF with manual ``on`` is the manual start, anything else with
    master OFF is the automatic start. Only the requirements of that route are
    evaluated; the others are ``na``, so a diagnostic never shows a blocker
    that does not constrain the decision. The inputs and the minimum valve
    count stay applicable on every route. ``reason`` is used only to label an
    automatic route without a candidate path; the policy never reads the
    conditions.
    """
    conditions: list[BoilerCondition] = []

    if mode == BOILER_MASTER_HEAT:
        route = _ROUTE_MAINTAIN
    elif mode != BOILER_MASTER_OFF:
        route = _ROUTE_MASTER_UNAVAILABLE
    elif snapshot.manual == BOILER_MANUAL_ON:
        route = _ROUTE_MANUAL
    else:
        route = _ROUTE_AUTOMATIC
    automatic = route == _ROUTE_AUTOMATIC

    candidate: str | None = None
    if automatic and normal:
        candidate = BOILER_REASON_NORMAL_WAIT
    elif automatic and one is not None:
        candidate = BOILER_REASON_SINGLE_WAIT

    def add(
        condition_id: str,
        status: ConditionStatus,
        reason_code: str | None = None,
        actual: Any = None,
        expected: Any = None,
        deadline: float | None = None,
    ) -> None:
        conditions.append(
            BoilerCondition(
                id=condition_id,
                status=status,
                reason_code=reason_code,
                actual=actual,
                expected=expected,
                deadline=deadline,
            )
        )

    add(
        "master",
        CONDITION_PASS if mode in (BOILER_MASTER_OFF, BOILER_MASTER_HEAT) else CONDITION_UNKNOWN,
        BOILER_REASON_MASTER_UNAVAILABLE if mode not in (BOILER_MASTER_OFF, BOILER_MASTER_HEAT) else None,
        actual=mode,
        expected="off|heat",
    )
    add(
        "inputs",
        CONDITION_PASS if snapshot.ok else CONDITION_FAIL,
        None if snapshot.ok else BOILER_REASON_DATA_OR_CENTRAL,
        actual=snapshot.ok,
        expected=True,
    )
    add(
        "valve_count",
        CONDITION_PASS if snapshot.count > 0 else CONDITION_FAIL,
        None if snapshot.count > 0 else BOILER_REASON_NO_VALID_RADIATOR,
        actual=snapshot.count,
        expected=">=1",
    )

    # Start requirements of the automatic route only: thermal gap and pilot.
    max_gap = max((room.gap for room in snapshot.rooms), default=None)
    if automatic:
        add(
            "thermal_gap",
            CONDITION_PASS if qualified else CONDITION_WAIT,
            None if qualified else BOILER_REASON_THERMAL_WAIT,
            actual=max_gap,
            expected=f">={config.thermal_gap_c}",
        )
    else:
        add("thermal_gap", CONDITION_NA, actual=max_gap, expected=f">={config.thermal_gap_c}")

    if not automatic:
        add("pilot", CONDITION_NA, actual=snapshot.pilot, expected="off|on")
    elif snapshot.pilot == BOILER_PILOT_OFF:
        add("pilot", CONDITION_NA, actual=snapshot.pilot, expected="off")
    elif snapshot.pilot == BOILER_PILOT_ON:
        add(
            "pilot",
            CONDITION_PASS if automatic_ok else CONDITION_FAIL,
            None if automatic_ok else BOILER_REASON_DATA_MISSING,
            actual=snapshot.pilot_ready,
            expected=True,
        )
    else:
        add("pilot", CONDITION_UNKNOWN, BOILER_REASON_DATA_MISSING, actual=snapshot.pilot, expected="off|on")

    # Room-level pilot qualification (``pilot_ok``), distinct from the global
    # readiness above. The baseline applies it only to the single-valve
    # candidate with the pilot ON; the normal path never requires it.
    single_candidate = qualified[0] if automatic and snapshot.count == 1 and len(qualified) == 1 else None
    if single_candidate is not None and snapshot.pilot == BOILER_PILOT_ON:
        add(
            "single_room_pilot",
            CONDITION_PASS if single_candidate.pilot_ok else CONDITION_FAIL,
            None if single_candidate.pilot_ok else CONDITION_REASON_SINGLE_ROOM_PILOT_VETO,
            actual=single_candidate.pilot_ok,
            expected=True,
        )
    else:
        add(
            "single_room_pilot",
            CONDITION_NA,
            actual=None if single_candidate is None else single_candidate.pilot_ok,
            expected=True,
        )

    if mode != BOILER_MASTER_OFF:
        add("manual_request", CONDITION_NA, actual=snapshot.manual, expected="on")
    elif snapshot.manual == BOILER_MANUAL_ON:
        add("manual_request", CONDITION_PASS, BOILER_REASON_MANUAL, actual=snapshot.manual, expected="on")
    elif snapshot.manual == BOILER_MANUAL_OFF:
        add("manual_request", CONDITION_NA, actual=snapshot.manual, expected="on")
    else:
        add("manual_request", CONDITION_UNKNOWN, BOILER_REASON_DATA_MISSING, actual=snapshot.manual, expected="on")

    if route == _ROUTE_MAINTAIN:
        add("path", CONDITION_NA, actual=None, expected="manual|normal|single")
    elif route == _ROUTE_MASTER_UNAVAILABLE:
        add("path", CONDITION_UNKNOWN, BOILER_REASON_MASTER_UNAVAILABLE, actual=None, expected="manual|normal|single")
    elif route == _ROUTE_MANUAL:
        add("path", CONDITION_PASS, BOILER_REASON_MANUAL, actual=BOILER_REASON_MANUAL, expected="manual|normal|single")
    else:
        add(
            "path",
            CONDITION_PASS if candidate is not None else CONDITION_WAIT,
            candidate or reason,
            actual=candidate,
            expected="manual|normal|single",
        )

    if candidate is not None:
        start = ready if candidate == BOILER_REASON_NORMAL_WAIT else single_since
        duration = config.normal_stability_s if candidate == BOILER_REASON_NORMAL_WAIT else config.single_stability_s
        if start is not None:
            stability_deadline = start + duration
            add(
                "stability",
                CONDITION_PASS if n >= stability_deadline else CONDITION_WAIT,
                None if n >= stability_deadline else candidate,
                actual=n - start,
                expected=f">={duration}",
                deadline=stability_deadline,
            )
        else:
            add("stability", CONDITION_UNKNOWN, actual=None, expected=f">={duration}")
    else:
        add("stability", CONDITION_NA, actual=None, expected=None)

    if automatic and stopped is not None:
        cooldown_deadline = stopped + config.off_cooldown_s
        cooldown_passed = n >= cooldown_deadline
        add(
            "cooldown",
            CONDITION_PASS if cooldown_passed else CONDITION_WAIT,
            None if cooldown_passed else (candidate or reason),
            actual=n - stopped,
            expected=f">={config.off_cooldown_s}",
            deadline=cooldown_deadline,
        )
    else:
        add("cooldown", CONDITION_NA, actual=None, expected=f">={config.off_cooldown_s}")

    if candidate == BOILER_REASON_NORMAL_WAIT:
        consent_status: ConditionStatus
        if consent is None:
            consent_status = CONDITION_UNKNOWN
            consent_reason = None
        elif consent:
            consent_status = CONDITION_PASS
            consent_reason = None
        else:
            consent_status = CONDITION_FAIL
            consent_reason = BOILER_REASON_AWAITING_CONSENT
        add("consent", consent_status, consent_reason, actual=consent, expected=True)
    else:
        add("consent", CONDITION_NA, actual=consent, expected=True)

    return tuple(conditions)
