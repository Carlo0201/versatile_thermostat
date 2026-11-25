"""Pure state machine of the radiator <-> heat pump (PDC) hand-over (M4).

It models the command sequence of the baseline automations
``*_scelta_radiatore_o_pompa_di_calore`` (start branch ``choose[2]`` and return
branch ``choose[0]``): reservation of the shared capacity, inhibition of the
radiator, wait for the radiator to stop, setpoint, HEAT, then, on the way back,
OFF to the PDC VTherm, wait for the physical split, direct OFF fallback.

Like ``heating_pdc_policy.py`` it is pure: no Home Assistant import, no I/O, no
global clock, no asyncio. The caller (the PDC runtime) feeds
:class:`PdcObservation` values read after each wait and executes the returned
:class:`PdcCommand` values. The machine never waits: it only exposes deadlines
(:func:`next_wakeup`).

Safety rules enforced here (``SPECIFICA.md``, section PDC):

* **ownership of the inhibition** (review of M4, ``M4-M5-RUNTIME-ESITO.md``): the
  VTherm inhibition is a single flag and ``InhibitionManager.async_set`` is
  idempotent, so a request cannot tell who placed it. The transaction therefore
  claims ownership (``inhibition_owned``) only if the radiator was **confirmed not
  inhibited** at the request (``radiator_inhibited_before is False``). A request
  with an inhibition that is already present (``True``) or of unknown state
  (``None``) is refused: the radiator is already stopped by somebody else, who may
  release it at any moment, so starting the PDC would create two sources. The
  claim is written to the store before the command runs; the runtime downgrades it
  with :func:`record_inhibition_result` when the call turns out not to have
  created the inhibition. The release happens **only** if owned;
* the inhibition is released only after the physical split **and** the PDC VTherm
  are observed ``off`` (and any HEAT command in flight had time to settle), by the
  ``releasing`` phase, which keeps retrying until the release is confirmed or the
  radiator is seen not inhibited. A failed release never loses the ownership;
* the PDC is never commanded to HEAT unless the radiator stop is confirmed *at the
  same observation* (radiator stopped, target confirmed and the policy re-check),
  and a running transaction whose radiator stop is no longer confirmed returns;
* a request is accepted only if the shared reservation is free and the other PDC
  is neither managed nor running; the reservation is held until the radiator is
  back (release confirmed);
* every transaction has a token (generation). Events, commands and timers carrying
  an old token are ignored, so a late answer cannot complete a newer transaction;
* an override that is not confirmed ``off`` (``on``, unknown, unavailable) freezes
  the machine without releasing anything, like the baseline script that stops at
  its ``override == off`` conditions and leaves the managed flag on. The return
  paths that the baseline itself runs *under an active override* (override
  protections, resume of the automatic mode, split switched off by the user) use
  ``override_exempt=True``;
* ``None``/unknown observations are never equated to ``off`` or to success.

The baseline waits are the default timeouts: radiator 25 s, HEAT 20 s, OFF 20 s,
direct OFF fallback 15 s. The setpoint has no wait in the baseline (checked once
right after the service call returns): ``target_timeout_s`` is 0 by default.

``running`` means *HEAT of the split confirmed*. It is not evidence that the
compressor is working (the SmartThings split reports HEAT while idle or in
defrost): nothing here reads ``hvac_action``.

The conditions that the baseline re-checks with a template (``recheck_*`` and
``keep_running_ok`` of ``heating_pdc_policy``) are computed by the caller and
passed as booleans, so this module does not depend on the policy module.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from typing import Any, Final

# Phases.
PHASE_IDLE: Final = "idle"
PHASE_WAITING_RADIATOR: Final = "waiting_radiator"
PHASE_WAITING_TARGET: Final = "waiting_target"
PHASE_WAITING_HEAT: Final = "waiting_heat"
PHASE_RUNNING: Final = "running"
PHASE_RETURNING: Final = "returning"
PHASE_RETURNING_DIRECT: Final = "returning_direct"
PHASE_RETURN_UNCONFIRMED: Final = "return_unconfirmed"
PHASE_RELEASING: Final = "releasing"
PHASE_OVERRIDE_HOLD: Final = "override_hold"

ALL_PHASES: Final = (
    PHASE_IDLE,
    PHASE_WAITING_RADIATOR,
    PHASE_WAITING_TARGET,
    PHASE_WAITING_HEAT,
    PHASE_RUNNING,
    PHASE_RETURNING,
    PHASE_RETURNING_DIRECT,
    PHASE_RETURN_UNCONFIRMED,
    PHASE_RELEASING,
    PHASE_OVERRIDE_HOLD,
)
START_PHASES: Final = (PHASE_WAITING_RADIATOR, PHASE_WAITING_TARGET, PHASE_WAITING_HEAT)
RETURN_PHASES: Final = (PHASE_RETURNING, PHASE_RETURNING_DIRECT)
#: Phases in which the radiator is on its way back (a new return request has nothing to add).
RETURN_LIKE_PHASES: Final = (PHASE_RETURNING, PHASE_RETURNING_DIRECT, PHASE_RELEASING)

# Commands (executed by the runtime; every command carries the transaction token).
CMD_INHIBIT_RADIATOR: Final = "inhibit_radiator"
CMD_SET_TARGET: Final = "set_pdc_target"
CMD_PDC_HEAT: Final = "set_pdc_heat"
CMD_PDC_OFF: Final = "set_pdc_off"
CMD_SPLIT_OFF_DIRECT: Final = "set_split_off_direct"
CMD_RELEASE_INHIBITION: Final = "release_inhibition"
CMD_RELEASE_RESERVATION: Final = "release_reservation"

# Failure codes (the first group is the vocabulary of the baseline ``*_pdc_transizione`` helper).
FAILURE_TRV_UNCONFIRMED: Final = "trv_unconfirmed"
FAILURE_TARGET_UNCONFIRMED: Final = "target_unconfirmed"
FAILURE_SPLIT_UNCONFIRMED: Final = "split_unconfirmed"
FAILURE_CONDITIONS_CHANGED: Final = "conditions_changed"
FAILURE_RETURN_UNCONFIRMED: Final = "return_unconfirmed"
FAILURE_CANCELLED: Final = "cancelled"
FAILURE_RECOVERED: Final = "recovered"
FAILURE_STOP: Final = "stop"
# New codes of the runtime (not in the baseline).
FAILURE_INHIBITION_NOT_OWNED: Final = "inhibition_not_owned"
FAILURE_INHIBITION_FAILED: Final = "inhibition_failed"
FAILURE_RADIATOR_RELEASED: Final = "radiator_released"
FAILURE_OVERRIDE_PROTECTION: Final = "override_protection"
FAILURE_OVERRIDE_ENDED: Final = "override_ended"
FAILURE_SUSPENDED: Final = "suspended"

# Request refusal reasons.
DENIED_BUSY: Final = "busy"
DENIED_RESERVED: Final = "reservation_held"
DENIED_OTHER_PDC: Final = "other_pdc_busy"
DENIED_OVERRIDE: Final = "override_not_off"
DENIED_TOKEN: Final = "stale_token"
DENIED_FOREIGN_INHIBITION: Final = "radiator_inhibited_foreign"
DENIED_INHIBITION_UNKNOWN: Final = "radiator_inhibition_unknown"

IGNORED_STALE: Final = "stale_token"
IGNORED_IDLE: Final = "idle"
IGNORED_HOLD: Final = "override_hold"
IGNORED_NO_CHANGE: Final = "no_change"

STATE_OFF: Final = "off"
STATE_HEAT: Final = "heat"
#: Affirmatively active modes of a split or of the PDC VTherm (``unknown``/``unavailable`` are not among them).
ACTIVE_MODES: Final = ("heat", "cool", "auto", "dry", "fan_only", "heat_cool")

SCHEMA_VERSION: Final = 1


def _non_negative(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be finite and >= 0")
    return number


def _finite(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


@dataclass(frozen=True, slots=True)
class PdcTransactionConfig:
    """Timeouts of the sequence (baseline values by default).

    ``heat_settle_s``, ``release_retry_s`` and ``return_retry_s`` are not in the
    baseline: the time a HEAT command in flight needs before a split ``off`` counts
    as the end of the session (cloud latency), the pause between two release
    attempts, and the pause before a return that was not confirmed is retried
    (``0`` disables the retry, as in the baseline).
    """

    radiator_timeout_s: float = 25.0
    target_timeout_s: float = 0.0
    heat_timeout_s: float = 20.0
    off_timeout_s: float = 20.0
    direct_off_timeout_s: float = 15.0
    heat_settle_s: float = 20.0
    release_retry_s: float = 30.0
    return_retry_s: float = 120.0

    def __post_init__(self) -> None:
        """Validate the timeouts."""
        for field in fields(self):
            _non_negative(field.name, getattr(self, field.name))


DEFAULT_CONFIG: Final = PdcTransactionConfig()


@dataclass(frozen=True, slots=True)
class PdcReservation:
    """Shared capacity/split reservation: at most one PDC holder at a time."""

    holder: str | None = None
    token: int | None = None

    def __post_init__(self) -> None:
        """Validate the reservation."""
        if (self.holder is None) != (self.token is None):
            raise ValueError("holder and token must be both set or both None")
        if self.holder is not None and not isinstance(self.holder, str):
            raise TypeError("holder must be a string")
        if self.token is not None and (isinstance(self.token, bool) or not isinstance(self.token, int) or self.token < 1):
            raise ValueError("token must be a positive integer")


def reserve(reservation: PdcReservation, room_key: str, token: int) -> tuple[PdcReservation, bool]:
    """Take the reservation if free. Idempotent for the same holder and token."""
    if reservation.holder is None:
        return PdcReservation(room_key, token), True
    return reservation, reservation.holder == room_key and reservation.token == token


def release(reservation: PdcReservation, room_key: str, token: int) -> PdcReservation:
    """Release only if ``room_key`` and ``token`` are the current holder (late releases are ignored)."""
    if reservation.holder == room_key and reservation.token == token:
        return PdcReservation()
    return reservation


@dataclass(frozen=True, slots=True)
class PdcObservation:
    """Facts read by the caller *after* the wait or the event.

    ``None`` means unknown/unavailable/missing and is never taken as success.

    * ``override_off``: the manual override flag is confirmed ``off``;
    * ``split_mode``/``pdc_mode``: physical split and PDC VTherm states;
    * ``radiator_stopped``: valve off, radiator VTherm off and inhibition reported
      (``heating_pdc_policy.radiator_stopped``);
    * ``radiator_inhibited``: the VTherm reports its inhibition flag (``False`` only
      when explicitly reported not inhibited);
    * ``target_confirmed``: the PDC setpoint equals the expected one;
    * ``conditions_ok``: the baseline re-check of the current phase (after the
      radiator stop, after the setpoint, after HEAT), computed by the caller.
    """

    token: int
    override_off: bool | None = None
    split_mode: str | None = None
    pdc_mode: str | None = None
    radiator_stopped: bool | None = None
    target_confirmed: bool | None = None
    conditions_ok: bool | None = None
    radiator_inhibited: bool | None = None

    def __post_init__(self) -> None:
        """Validate the observation."""
        if isinstance(self.token, bool) or not isinstance(self.token, int):
            raise TypeError("token must be an integer")
        for name in ("override_off", "radiator_stopped", "target_confirmed", "conditions_ok", "radiator_inhibited"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise TypeError(f"{name} must be a boolean or None")
        for name in ("split_mode", "pdc_mode"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{name} must be a string or None")


@dataclass(frozen=True, slots=True)
class PdcCommand:
    """A command to execute. The runtime must drop it if the token is no longer current."""

    kind: str
    token: int
    room_key: str
    value: float | None = None


@dataclass(frozen=True, slots=True)
class PdcTransaction:
    """State of one room's hand-over. ``token`` only grows; it is kept while idle.

    * ``inhibition_owned``: this transaction created the radiator inhibition and is
      the only one entitled to release it;
    * ``inhibited_before``: inhibition flag observed at the request;
    * ``heat_sent_at``: instant of a HEAT command whose effect on the split has not
      been seen yet;
    * ``override_exempt``: the return runs although the override is not ``off``;
    * ``manual``: session adopted from a manual override (no start sequence).
    """

    room_key: str
    token: int = 0
    phase: str = PHASE_IDLE
    started_at: float | None = None
    phase_started_at: float | None = None
    deadline: float | None = None
    inhibition_owned: bool = False
    inhibited_before: bool | None = None
    reservation_held: bool = False
    failure: str | None = None
    direct_off_sent: bool = False
    heat_sent_at: float | None = None
    override_exempt: bool = False
    release_attempts: int = 0
    manual: bool = False

    def __post_init__(self) -> None:
        """Validate the transaction."""
        if not isinstance(self.room_key, str) or not self.room_key:
            raise ValueError("room_key must be a non-empty string")
        if isinstance(self.token, bool) or not isinstance(self.token, int) or self.token < 0:
            raise ValueError("token must be a non-negative integer")
        if self.phase not in ALL_PHASES:
            raise ValueError(f"unknown phase {self.phase!r}")
        for name in ("started_at", "phase_started_at", "deadline", "heat_sent_at"):
            value = getattr(self, name)
            if value is not None:
                _finite(name, value)
        for name in ("inhibition_owned", "reservation_held", "direct_off_sent", "override_exempt", "manual"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a boolean")
        if self.inhibited_before is not None and not isinstance(self.inhibited_before, bool):
            raise TypeError("inhibited_before must be a boolean or None")
        if isinstance(self.release_attempts, bool) or not isinstance(self.release_attempts, int) or self.release_attempts < 0:
            raise ValueError("release_attempts must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class PdcStep:
    """Result of one call: new state, new reservation, commands and why nothing happened."""

    transaction: PdcTransaction
    reservation: PdcReservation
    commands: tuple[PdcCommand, ...] = ()
    accepted: bool = True
    reason: str | None = None


def _cmd(tx: PdcTransaction, kind: str, value: float | None = None) -> PdcCommand:
    return PdcCommand(kind, tx.token, tx.room_key, value)


def _enter(tx: PdcTransaction, phase: str, now: float, timeout: float | None) -> PdcTransaction:
    return replace(tx, phase=phase, phase_started_at=now, deadline=None if timeout is None else now + timeout)


def heat_settled(tx: PdcTransaction, now: float, config: PdcTransactionConfig = DEFAULT_CONFIG) -> bool:
    """A HEAT command in flight has had ``heat_settle_s`` to reach the split (or was never sent)."""
    return tx.heat_sent_at is None or now - tx.heat_sent_at >= config.heat_settle_s


def off_confirmed(tx: PdcTransaction, obs: PdcObservation | None, now: float, config: PdcTransactionConfig = DEFAULT_CONFIG) -> bool:
    """PDC OFF confirmed: the physical split **and** the PDC VTherm are ``off`` and no HEAT is still in flight."""
    return obs is not None and obs.split_mode == STATE_OFF and obs.pdc_mode == STATE_OFF and heat_settled(tx, now, config)


def next_wakeup(tx: PdcTransaction, now: float, config: PdcTransactionConfig = DEFAULT_CONFIG) -> float | None:
    """Earliest instant at which the runtime must call :func:`observe` again without an event."""
    candidates = [tx.deadline] if tx.deadline is not None else []
    if tx.phase in RETURN_PHASES and tx.heat_sent_at is not None and not heat_settled(tx, now, config):
        candidates.append(tx.heat_sent_at + config.heat_settle_s)
    return min(candidates) if candidates else None


def request_start(
    tx: PdcTransaction,
    reservation: PdcReservation,
    now: float,
    *,
    override_off: bool | None,
    other_pdc_busy: bool | None,
    radiator_inhibited_before: bool | None,
    config: PdcTransactionConfig = DEFAULT_CONFIG,
) -> PdcStep:
    """Accept a start (decision ``start`` of the policy) and reserve the capacity.

    Serialized decision and reservation: the caller calls this synchronously and
    only then awaits the radiator. A second simultaneous request finds the
    reservation taken (or the machine busy) and is refused without side effects.
    ``other_pdc_busy`` is true or unknown (``None``) when the other PDC is managed
    or its split is not confirmed ``off``: unknown refuses.

    ``radiator_inhibited_before`` is required and must be ``False``: an inhibition
    that is already present, or of unknown state, refuses the start. Only then the
    transaction claims the ownership of the inhibition it is about to place.
    """
    _finite("now", now)
    if tx.phase != PHASE_IDLE:
        return PdcStep(tx, reservation, accepted=False, reason=DENIED_BUSY)
    if override_off is not True:
        return PdcStep(tx, reservation, accepted=False, reason=DENIED_OVERRIDE)
    if other_pdc_busy is not False:
        return PdcStep(tx, reservation, accepted=False, reason=DENIED_OTHER_PDC)
    if radiator_inhibited_before is True:
        return PdcStep(tx, reservation, accepted=False, reason=DENIED_FOREIGN_INHIBITION)
    if radiator_inhibited_before is not False:
        return PdcStep(tx, reservation, accepted=False, reason=DENIED_INHIBITION_UNKNOWN)
    token = tx.token + 1
    new_reservation, granted = reserve(reservation, tx.room_key, token)
    if not granted:
        return PdcStep(tx, reservation, accepted=False, reason=DENIED_RESERVED)
    started = replace(
        tx,
        token=token,
        started_at=now,
        inhibition_owned=True,
        inhibited_before=False,
        reservation_held=True,
        failure=None,
        direct_off_sent=False,
        heat_sent_at=None,
        override_exempt=False,
        release_attempts=0,
        manual=False,
    )
    started = _enter(started, PHASE_WAITING_RADIATOR, now, config.radiator_timeout_s)
    return PdcStep(started, new_reservation, (_cmd(started, CMD_INHIBIT_RADIATOR),))


def adopt_manual(
    tx: PdcTransaction,
    reservation: PdcReservation,
    now: float,
    *,
    radiator_inhibited_before: bool | None,
    config: PdcTransactionConfig = DEFAULT_CONFIG,
) -> PdcStep:
    """A manual override found the split running: suspend the radiator for the duration of the session.

    Baseline ``override_sincronizza`` turns ``in_gestione`` on when the split is not
    off. There is no start sequence (the user already started the split): the
    session goes straight to ``override_hold``. The reservation is taken if free (a
    manual session is never refused for it; the managed flag already excludes the
    other PDC). An inhibition that is already present is not claimed.
    """
    _finite("now", now)
    if tx.phase != PHASE_IDLE:
        return PdcStep(tx, reservation, accepted=False, reason=DENIED_BUSY)
    if radiator_inhibited_before is None:
        return PdcStep(tx, reservation, accepted=False, reason=DENIED_INHIBITION_UNKNOWN)
    token = tx.token + 1
    new_reservation, granted = reserve(reservation, tx.room_key, token)
    owned = radiator_inhibited_before is False
    session = replace(
        tx,
        token=token,
        started_at=now,
        inhibition_owned=owned,
        inhibited_before=radiator_inhibited_before,
        reservation_held=granted,
        failure=None,
        direct_off_sent=False,
        heat_sent_at=None,
        override_exempt=False,
        release_attempts=0,
        manual=True,
    )
    session = _enter(session, PHASE_OVERRIDE_HOLD, now, None)
    commands = (_cmd(session, CMD_INHIBIT_RADIATOR),) if owned else ()
    return PdcStep(session, new_reservation if granted else reservation, commands)


def record_inhibition_result(
    tx: PdcTransaction,
    reservation: PdcReservation,
    now: float,
    *,
    token: int,
    inhibited_after: bool | None,
    transitioned: bool | None,
    obs: PdcObservation | None = None,
    config: PdcTransactionConfig = DEFAULT_CONFIG,
    call_sent: bool = True,
) -> PdcStep:
    """Settle the ownership claim after the inhibition call returned (or raised).

    ``inhibited_after`` is the inhibition flag read after the call (``None`` when
    unreadable); ``transitioned`` is true when the VTherm reports a new inhibition
    transition (``last_transition_at`` changed), i.e. when the call created the
    inhibition instead of finding it already there.

    * not inhibited after the call: nothing was placed, nothing to release;
    * inhibited but no transition: the inhibition is somebody else's. The claim is
      dropped (it will never be released by us) and a start is abandoned through
      the return path;
    * unreadable after a call that was sent (it may have been applied): the claim made
      before the call stands;
    * unreadable and no call was sent (``call_sent`` false, e.g. the flag was unknown
      before the call): nothing was placed by us, so the claim is dropped. An
      inhibition appearing later is somebody else's and is never released by us.
    """
    _finite("now", now)
    if token != tx.token:
        return PdcStep(tx, reservation, accepted=False, reason=IGNORED_STALE)
    if tx.phase not in (PHASE_WAITING_RADIATOR, PHASE_OVERRIDE_HOLD) or not tx.inhibition_owned:
        return PdcStep(tx, reservation, accepted=False, reason=IGNORED_NO_CHANGE)
    if inhibited_after is True and transitioned is not False:
        return PdcStep(tx, reservation)
    dropped = replace(tx, inhibition_owned=False)
    if inhibited_after is None and call_sent:
        return PdcStep(tx, reservation)
    if tx.phase == PHASE_OVERRIDE_HOLD:
        return PdcStep(dropped, reservation)
    failure = FAILURE_INHIBITION_NOT_OWNED if inhibited_after is True else FAILURE_INHIBITION_FAILED
    return _begin_return(dropped, reservation, obs, now, failure, config)


def _idle(tx: PdcTransaction, reservation: PdcReservation, commands: tuple[PdcCommand, ...]) -> PdcStep:
    out = list(commands)
    if tx.reservation_held:
        out.append(_cmd(tx, CMD_RELEASE_RESERVATION))
    done = replace(
        tx,
        phase=PHASE_IDLE,
        phase_started_at=None,
        deadline=None,
        inhibition_owned=False,
        reservation_held=False,
        direct_off_sent=False,
        heat_sent_at=None,
        override_exempt=False,
        release_attempts=0,
        manual=False,
    )
    return PdcStep(done, release(reservation, tx.room_key, tx.token), tuple(out))


def _begin_return(
    tx: PdcTransaction,
    reservation: PdcReservation,
    obs: PdcObservation | None,
    now: float,
    failure: str,
    config: PdcTransactionConfig,
    *,
    exempt: bool = False,
) -> PdcStep:
    """Enter the return path. A split and PDC already confirmed ``off`` skip the wait (the baseline would wait on a true template)."""
    returning = replace(tx, failure=failure, direct_off_sent=False, override_exempt=exempt or tx.override_exempt)
    if off_confirmed(returning, obs, now, config):
        return _finish_return(returning, reservation, now, config)
    returning = _enter(returning, PHASE_RETURNING, now, config.off_timeout_s)
    return PdcStep(returning, reservation, (_cmd(returning, CMD_PDC_OFF),))


def _finish_return(tx: PdcTransaction, reservation: PdcReservation, now: float, config: PdcTransactionConfig) -> PdcStep:
    """Split and PDC confirmed ``off``: now, and only now, give the radiator back if this transaction owns the inhibition."""
    if tx.inhibition_owned:
        releasing = replace(tx, phase=PHASE_RELEASING, phase_started_at=now, deadline=now + config.release_retry_s, release_attempts=1, direct_off_sent=False, heat_sent_at=None)
        return PdcStep(releasing, reservation, (_cmd(releasing, CMD_RELEASE_INHIBITION),))
    return _idle(tx, reservation, ())


def request_return(
    tx: PdcTransaction,
    reservation: PdcReservation,
    now: float,
    obs: PdcObservation | None = None,
    *,
    reason: str = FAILURE_STOP,
    override_exempt: bool = False,
    config: PdcTransactionConfig = DEFAULT_CONFIG,
) -> PdcStep:
    """Start the return to the radiator (policy stop, cancellation, retry after ``return_unconfirmed``).

    Allowed while running, during the start sequence, while frozen by an override
    that has ended (``obs.override_off`` true) and after an unconfirmed return.
    The override must be confirmed ``off`` before any command, like the baseline,
    unless ``override_exempt`` (override protections, resume of the automatic mode,
    split switched off during the override, general suspension).
    """
    _finite("now", now)
    if obs is not None and obs.token != tx.token:
        return PdcStep(tx, reservation, accepted=False, reason=IGNORED_STALE)
    if tx.phase == PHASE_IDLE:
        return PdcStep(tx, reservation, accepted=False, reason=IGNORED_IDLE)
    if tx.phase in RETURN_LIKE_PHASES:
        if override_exempt and not tx.override_exempt:
            return PdcStep(replace(tx, override_exempt=True), reservation, accepted=False, reason=IGNORED_NO_CHANGE)
        return PdcStep(tx, reservation, accepted=False, reason=IGNORED_NO_CHANGE)
    if not override_exempt and (obs is None or obs.override_off is not True):
        # Without a confirmed ``off`` override no command is sent (baseline conditions); nothing is released.
        return PdcStep(_hold(tx), reservation, accepted=False, reason=DENIED_OVERRIDE)
    return _begin_return(tx, reservation, obs, now, reason, config, exempt=override_exempt)


def cancel(
    tx: PdcTransaction,
    reservation: PdcReservation,
    now: float,
    obs: PdcObservation | None = None,
    config: PdcTransactionConfig = DEFAULT_CONFIG,
    *,
    override_exempt: bool = False,
) -> PdcStep:
    """Cancellation: the same safe return path, never a release of the radiator."""
    return request_return(tx, reservation, now, obs, reason=FAILURE_CANCELLED, override_exempt=override_exempt, config=config)


def _hold(tx: PdcTransaction) -> PdcTransaction:
    if tx.phase in (PHASE_IDLE, PHASE_RUNNING, PHASE_OVERRIDE_HOLD):
        return tx
    return replace(tx, phase=PHASE_OVERRIDE_HOLD, deadline=None)


def recover(tx: PdcTransaction, reservation: PdcReservation, now: float, obs: PdcObservation | None, config: PdcTransactionConfig = DEFAULT_CONFIG) -> PdcStep:
    """After a restart or reconnection with a persisted transaction.

    In-flight commands are unknown, so every non-idle phase except a verified
    ``running`` goes through the return path with a new token (old events and
    timers become stale). Nothing is released without a confirmed ``off`` split
    and PDC. A ``releasing`` phase whose OFF is still confirmed retries the release.
    """
    _finite("now", now)
    if tx.phase == PHASE_IDLE:
        return PdcStep(tx, reservation)
    if tx.phase == PHASE_RUNNING and obs is not None and obs.split_mode == STATE_HEAT and obs.pdc_mode == STATE_HEAT:
        return PdcStep(tx, reservation)
    if tx.phase == PHASE_OVERRIDE_HOLD and not tx.override_exempt and (obs is None or obs.override_off is not True):
        return PdcStep(tx, reservation)
    renewed = replace(tx, token=tx.token + 1)
    held = reservation
    if tx.reservation_held and reservation.holder == tx.room_key and reservation.token == tx.token:
        held = PdcReservation(tx.room_key, renewed.token)
    fresh_obs = None if obs is None else replace(obs, token=renewed.token)
    if not renewed.override_exempt and (fresh_obs is None or fresh_obs.override_off is not True):
        return PdcStep(_hold(renewed), held, accepted=False, reason=DENIED_OVERRIDE)
    if tx.phase == PHASE_RELEASING and off_confirmed(renewed, fresh_obs, now, config):
        return _finish_return(renewed, held, now, config)
    return _begin_return(renewed, held, fresh_obs, now, FAILURE_RECOVERED, config)


def confirm_release(
    tx: PdcTransaction,
    reservation: PdcReservation,
    now: float,
    *,
    token: int,
    released: bool | None,
    config: PdcTransactionConfig = DEFAULT_CONFIG,
) -> PdcStep:
    """Result of a release command: ``True`` ends the transaction, anything else keeps the ownership and retries later."""
    _finite("now", now)
    if token != tx.token or tx.phase != PHASE_RELEASING:
        return PdcStep(tx, reservation, accepted=False, reason=IGNORED_STALE if token != tx.token else IGNORED_NO_CHANGE)
    if released is True:
        return _idle(tx, reservation, ())
    retry = replace(tx, deadline=now + config.release_retry_s)
    return PdcStep(retry, reservation, accepted=False, reason="release_not_confirmed")


def observe(tx: PdcTransaction, reservation: PdcReservation, obs: PdcObservation, now: float, config: PdcTransactionConfig = DEFAULT_CONFIG) -> PdcStep:
    """Advance on an event or a timer tick. Idempotent: a duplicate observation emits nothing twice."""
    _finite("now", now)
    if obs.token != tx.token:
        return PdcStep(tx, reservation, accepted=False, reason=IGNORED_STALE)
    phase = tx.phase
    if phase in (PHASE_IDLE, PHASE_RUNNING):
        return PdcStep(tx, reservation, accepted=False, reason=IGNORED_IDLE if phase == PHASE_IDLE else IGNORED_NO_CHANGE)
    if phase == PHASE_OVERRIDE_HOLD:
        return PdcStep(tx, reservation, accepted=False, reason=IGNORED_HOLD)
    runs = tx.override_exempt or obs.override_off is True
    deadline = tx.deadline if tx.deadline is not None else now
    expired = now >= deadline
    if phase == PHASE_RETURN_UNCONFIRMED:
        if not runs:
            return PdcStep(tx, reservation, accepted=False, reason=IGNORED_NO_CHANGE)
        if off_confirmed(tx, obs, now, config):
            # A late confirmation completes the return (the OFF finally arrived).
            return _finish_return(tx, reservation, now, config)
        if tx.deadline is not None and expired:
            return _begin_return(tx, reservation, obs, now, FAILURE_RETURN_UNCONFIRMED, config)
        return PdcStep(tx, reservation, accepted=False, reason=IGNORED_NO_CHANGE)
    if not runs:
        return PdcStep(_hold(tx), reservation, accepted=False, reason=DENIED_OVERRIDE)
    if phase == PHASE_WAITING_RADIATOR:
        stopped = obs.radiator_stopped is True
        if not stopped and not expired:
            return PdcStep(tx, reservation, accepted=False, reason=IGNORED_NO_CHANGE)
        if stopped and not tx.inhibition_owned:
            # Somebody else's inhibition would let us start while the owner can reopen the radiator.
            return _begin_return(tx, reservation, obs, now, FAILURE_INHIBITION_NOT_OWNED, config)
        if stopped and obs.conditions_ok is True:
            waiting = _enter(tx, PHASE_WAITING_TARGET, now, config.target_timeout_s)
            return PdcStep(waiting, reservation, (_cmd(waiting, CMD_SET_TARGET),))
        failure = FAILURE_TRV_UNCONFIRMED if not stopped else FAILURE_CONDITIONS_CHANGED
        return _begin_return(tx, reservation, obs, now, failure, config)
    if phase == PHASE_WAITING_TARGET:
        confirmed = obs.target_confirmed is True
        stopped = obs.radiator_stopped is True
        if confirmed and stopped and obs.conditions_ok is True and tx.inhibition_owned:
            waiting = _enter(replace(tx, heat_sent_at=now), PHASE_WAITING_HEAT, now, config.heat_timeout_s)
            return PdcStep(waiting, reservation, (_cmd(waiting, CMD_PDC_HEAT),))
        if not confirmed and not expired:
            return PdcStep(tx, reservation, accepted=False, reason=IGNORED_NO_CHANGE)
        if not confirmed:
            failure = FAILURE_TARGET_UNCONFIRMED
        elif obs.conditions_ok is not True:
            failure = FAILURE_CONDITIONS_CHANGED
        elif not stopped:
            failure = FAILURE_RADIATOR_RELEASED
        else:
            failure = FAILURE_INHIBITION_NOT_OWNED
        return _begin_return(tx, reservation, obs, now, failure, config)
    if phase == PHASE_WAITING_HEAT:
        heated = obs.split_mode == STATE_HEAT
        stopped = obs.radiator_stopped is True
        if heated and stopped and obs.conditions_ok is True:
            return PdcStep(replace(tx, phase=PHASE_RUNNING, phase_started_at=now, deadline=None, heat_sent_at=None), reservation)
        if not expired and (not heated or (obs.radiator_stopped is None and obs.conditions_ok is True)):
            return PdcStep(tx, reservation, accepted=False, reason=IGNORED_NO_CHANGE)
        if not heated:
            failure = FAILURE_SPLIT_UNCONFIRMED
        elif obs.conditions_ok is not True:
            failure = FAILURE_CONDITIONS_CHANGED
        else:
            failure = FAILURE_RADIATOR_RELEASED
        return _begin_return(tx, reservation, obs, now, failure, config)
    if phase == PHASE_RETURNING:
        if off_confirmed(tx, obs, now, config):
            return _finish_return(tx, reservation, now, config)
        if not expired:
            return PdcStep(tx, reservation, accepted=False, reason=IGNORED_NO_CHANGE)
        direct = _enter(replace(tx, direct_off_sent=True), PHASE_RETURNING_DIRECT, now, config.direct_off_timeout_s)
        return PdcStep(direct, reservation, (_cmd(direct, CMD_SPLIT_OFF_DIRECT),))
    if phase == PHASE_RETURNING_DIRECT:
        if off_confirmed(tx, obs, now, config):
            return _finish_return(tx, reservation, now, config)
        if not expired:
            return PdcStep(tx, reservation, accepted=False, reason=IGNORED_NO_CHANGE)
        retry_at = now + config.return_retry_s if config.return_retry_s > 0 else None
        stuck = replace(tx, phase=PHASE_RETURN_UNCONFIRMED, deadline=retry_at, failure=FAILURE_RETURN_UNCONFIRMED)
        return PdcStep(stuck, reservation, accepted=False, reason=FAILURE_RETURN_UNCONFIRMED)
    if phase == PHASE_RELEASING:
        if obs.radiator_inhibited is False:
            # Somebody (or a restart) already gave the radiator back: nothing is left to release.
            return _idle(tx, reservation, ())
        if obs.split_mode in ACTIVE_MODES or obs.pdc_mode in ACTIVE_MODES:
            # The PDC came back while the release was pending: the radiator must not be released.
            return _begin_return(tx, reservation, obs, now, FAILURE_CONDITIONS_CHANGED, config)
        if expired and off_confirmed(tx, obs, now, config):
            retry = replace(tx, deadline=now + config.release_retry_s, release_attempts=tx.release_attempts + 1)
            return PdcStep(retry, reservation, (_cmd(retry, CMD_RELEASE_INHIBITION),))
        return PdcStep(tx, reservation, accepted=False, reason=IGNORED_NO_CHANGE)
    raise AssertionError(phase)


# --- persistence (versioned plain data, validated on load) ---------------------------------------

_TX_FIELDS: Final = tuple(f.name for f in fields(PdcTransaction))


def transaction_to_dict(tx: PdcTransaction) -> dict[str, Any]:
    """Plain JSON-able data of a transaction (``schema_version`` 1)."""
    data: dict[str, Any] = {name: getattr(tx, name) for name in _TX_FIELDS}
    data["schema_version"] = SCHEMA_VERSION
    return data


def transaction_from_dict(data: Any, room_key: str) -> PdcTransaction:
    """Rebuild a transaction; raises ``ValueError``/``TypeError`` on anything that is not valid (the caller must not guess)."""
    if not isinstance(data, Mapping):
        raise ValueError("transaction data must be a mapping")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported transaction schema")
    if data.get("room_key") != room_key:
        raise ValueError("transaction of another room")
    values = {name: data[name] for name in _TX_FIELDS if name in data}
    return PdcTransaction(**values)


def reservation_to_dict(reservation: PdcReservation) -> dict[str, Any]:
    """Plain data of the shared reservation."""
    return {"holder": reservation.holder, "token": reservation.token}


def reservation_from_dict(data: Any) -> PdcReservation:
    """Rebuild the reservation; raises on invalid data."""
    if not isinstance(data, Mapping):
        raise ValueError("reservation data must be a mapping")
    return PdcReservation(data.get("holder"), data.get("token"))
