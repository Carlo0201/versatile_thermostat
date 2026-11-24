"""Pure state machine of the radiator <-> heat pump (PDC) hand-over (M4).

It models the command sequence of the baseline automations
``*_scelta_radiatore_o_pompa_di_calore`` (start branch ``choose[2]`` and return
branch ``choose[0]``): reservation of the shared capacity, inhibition of the
radiator, wait for the radiator to stop, setpoint, HEAT, then, on the way back,
OFF to the PDC VTherm, wait for the physical split, direct OFF fallback.

Like ``heating_pdc_policy.py`` it is pure: no Home Assistant import, no I/O, no
global clock, no asyncio. The caller (a future runtime manager) feeds
:class:`PdcObservation` values read after each wait and executes the returned
:class:`PdcCommand` values. The machine never waits: it only exposes deadlines.

Safety rules enforced here (``SPECIFICA.md``, section PDC):

* the radiator inhibition is released **only** after the physical split is
  observed ``off``, and only if this transaction placed it (``inhibition_owned``).
  Timeout, unknown data, cancellation, restart or an override never release it;
* a request is accepted only if the shared reservation is free and the other PDC
  is neither managed nor running; the reservation is held until the return is
  confirmed;
* every transaction has a token (generation). Events, commands and timers carrying
  an old token are ignored, so a late answer cannot complete a newer transaction;
* an override that is not confirmed ``off`` (``on``, unknown, unavailable) freezes
  the machine without releasing anything, like the baseline script that stops at
  its ``override == off`` conditions and leaves the managed flag on;
* ``None``/unknown observations are never equated to ``off`` or to success.

The baseline waits are the default timeouts: radiator 25 s, HEAT 20 s, OFF 20 s,
direct OFF fallback 15 s. The setpoint has no wait in the baseline (checked once
right after the service call returns): ``target_timeout_s`` is 0 by default.

The conditions that the baseline re-checks with a template (``recheck_*`` and
``keep_running_ok`` of ``heating_pdc_policy``) are computed by the caller and
passed as booleans, so this module does not depend on the policy module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Final

# Phases.
PHASE_IDLE: Final = "idle"
PHASE_WAITING_RADIATOR: Final = "waiting_radiator"
PHASE_WAITING_TARGET: Final = "waiting_target"
PHASE_WAITING_HEAT: Final = "waiting_heat"
PHASE_RUNNING: Final = "running"
PHASE_RETURNING: Final = "returning"
PHASE_RETURNING_DIRECT: Final = "returning_direct"
PHASE_RETURN_UNCONFIRMED: Final = "return_unconfirmed"
PHASE_OVERRIDE_HOLD: Final = "override_hold"

START_PHASES: Final = (PHASE_WAITING_RADIATOR, PHASE_WAITING_TARGET, PHASE_WAITING_HEAT)
RETURN_PHASES: Final = (PHASE_RETURNING, PHASE_RETURNING_DIRECT)

# Commands (executed by the runtime; every command carries the transaction token).
CMD_INHIBIT_RADIATOR: Final = "inhibit_radiator"
CMD_SET_TARGET: Final = "set_pdc_target"
CMD_PDC_HEAT: Final = "set_pdc_heat"
CMD_PDC_OFF: Final = "set_pdc_off"
CMD_SPLIT_OFF_DIRECT: Final = "set_split_off_direct"
CMD_RELEASE_INHIBITION: Final = "release_inhibition"
CMD_RELEASE_RESERVATION: Final = "release_reservation"

# Failure codes (same vocabulary as the baseline ``*_pdc_transizione`` helper).
FAILURE_TRV_UNCONFIRMED: Final = "trv_unconfirmed"
FAILURE_TARGET_UNCONFIRMED: Final = "target_unconfirmed"
FAILURE_SPLIT_UNCONFIRMED: Final = "split_unconfirmed"
FAILURE_CONDITIONS_CHANGED: Final = "conditions_changed"
FAILURE_RETURN_UNCONFIRMED: Final = "return_unconfirmed"
FAILURE_CANCELLED: Final = "cancelled"
FAILURE_RECOVERED: Final = "recovered"
FAILURE_STOP: Final = "stop"

# Request refusal reasons.
DENIED_BUSY: Final = "busy"
DENIED_RESERVED: Final = "reservation_held"
DENIED_OTHER_PDC: Final = "other_pdc_busy"
DENIED_OVERRIDE: Final = "override_not_off"
DENIED_TOKEN: Final = "stale_token"

IGNORED_STALE: Final = "stale_token"
IGNORED_IDLE: Final = "idle"
IGNORED_HOLD: Final = "override_hold"
IGNORED_NO_CHANGE: Final = "no_change"

STATE_OFF: Final = "off"
STATE_HEAT: Final = "heat"


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
    """Timeouts of the sequence (baseline values by default)."""

    radiator_timeout_s: float = 25.0
    target_timeout_s: float = 0.0
    heat_timeout_s: float = 20.0
    off_timeout_s: float = 20.0
    direct_off_timeout_s: float = 15.0

    def __post_init__(self) -> None:
        """Validate the timeouts."""
        for name in ("radiator_timeout_s", "target_timeout_s", "heat_timeout_s", "off_timeout_s", "direct_off_timeout_s"):
            _non_negative(name, getattr(self, name))


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

    def __post_init__(self) -> None:
        """Validate the observation."""
        if isinstance(self.token, bool) or not isinstance(self.token, int):
            raise TypeError("token must be an integer")
        for name in ("override_off", "radiator_stopped", "target_confirmed", "conditions_ok"):
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
    """State of one room's hand-over. ``token`` only grows; it is kept while idle."""

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

    def __post_init__(self) -> None:
        """Validate the transaction."""
        if not isinstance(self.room_key, str) or not self.room_key:
            raise ValueError("room_key must be a non-empty string")
        if isinstance(self.token, bool) or not isinstance(self.token, int) or self.token < 0:
            raise ValueError("token must be a non-negative integer")
        for name in ("started_at", "phase_started_at", "deadline"):
            value = getattr(self, name)
            if value is not None:
                _finite(name, value)


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


def request_start(
    tx: PdcTransaction,
    reservation: PdcReservation,
    now: float,
    *,
    override_off: bool | None,
    other_pdc_busy: bool | None,
    radiator_inhibited_before: bool | None = None,
    config: PdcTransactionConfig = DEFAULT_CONFIG,
) -> PdcStep:
    """Accept a start (decision ``start`` of the policy) and reserve the capacity.

    Serialized decision and reservation: the caller calls this synchronously and
    only then awaits the radiator. A second simultaneous request finds the
    reservation taken (or the machine busy) and is refused without side effects.
    ``other_pdc_busy`` is true or unknown (``None``) when the other PDC is managed
    or its split is not confirmed ``off``: unknown refuses.
    """
    _finite("now", now)
    if tx.phase != PHASE_IDLE:
        return PdcStep(tx, reservation, accepted=False, reason=DENIED_BUSY)
    if override_off is not True:
        return PdcStep(tx, reservation, accepted=False, reason=DENIED_OVERRIDE)
    if other_pdc_busy is not False:
        return PdcStep(tx, reservation, accepted=False, reason=DENIED_OTHER_PDC)
    token = tx.token + 1
    new_reservation, granted = reserve(reservation, tx.room_key, token)
    if not granted:
        return PdcStep(tx, reservation, accepted=False, reason=DENIED_RESERVED)
    started = replace(
        tx,
        token=token,
        started_at=now,
        inhibition_owned=True,
        inhibited_before=radiator_inhibited_before,
        reservation_held=True,
        failure=None,
        direct_off_sent=False,
    )
    started = _enter(started, PHASE_WAITING_RADIATOR, now, config.radiator_timeout_s)
    return PdcStep(started, new_reservation, (_cmd(started, CMD_INHIBIT_RADIATOR),))


def _begin_return(tx: PdcTransaction, reservation: PdcReservation, obs: PdcObservation | None, now: float, failure: str, config: PdcTransactionConfig) -> PdcStep:
    """Enter the return path. A split already confirmed ``off`` skips the wait (the baseline would wait on a true template)."""
    already_off = obs is not None and obs.split_mode == STATE_OFF and obs.pdc_mode == STATE_OFF
    returning = replace(tx, failure=failure, direct_off_sent=False)
    if already_off:
        return _finish_return(returning, reservation, ())
    returning = _enter(returning, PHASE_RETURNING, now, config.off_timeout_s)
    return PdcStep(returning, reservation, (_cmd(returning, CMD_PDC_OFF),))


def _finish_return(tx: PdcTransaction, reservation: PdcReservation, commands: tuple[PdcCommand, ...]) -> PdcStep:
    """Split confirmed ``off``: now, and only now, release what this transaction owns."""
    out = list(commands)
    if tx.inhibition_owned:
        out.append(_cmd(tx, CMD_RELEASE_INHIBITION))
    if tx.reservation_held:
        out.append(_cmd(tx, CMD_RELEASE_RESERVATION))
    done = replace(tx, phase=PHASE_IDLE, phase_started_at=None, deadline=None, inhibition_owned=False, reservation_held=False, direct_off_sent=False)
    return PdcStep(done, release(reservation, tx.room_key, tx.token), tuple(out))


def request_return(
    tx: PdcTransaction,
    reservation: PdcReservation,
    now: float,
    obs: PdcObservation | None = None,
    *,
    reason: str = FAILURE_STOP,
    config: PdcTransactionConfig = DEFAULT_CONFIG,
) -> PdcStep:
    """Start the return to the radiator (policy stop, cancellation, retry after ``return_unconfirmed``).

    Allowed while running, during the start sequence, while frozen by an override
    that has ended (``obs.override_off`` true) and after an unconfirmed return.
    The override must be confirmed ``off`` before any command, like the baseline.
    """
    _finite("now", now)
    if obs is not None and obs.token != tx.token:
        return PdcStep(tx, reservation, accepted=False, reason=IGNORED_STALE)
    if tx.phase == PHASE_IDLE:
        return PdcStep(tx, reservation, accepted=False, reason=IGNORED_IDLE)
    if tx.phase in RETURN_PHASES:
        return PdcStep(tx, reservation, accepted=False, reason=IGNORED_NO_CHANGE)
    if obs is None or obs.override_off is not True:
        # Without a confirmed ``off`` override no command is sent (baseline conditions); nothing is released.
        return PdcStep(_hold(tx), reservation, accepted=False, reason=DENIED_OVERRIDE)
    return _begin_return(tx, reservation, obs, now, reason, config)


def cancel(tx: PdcTransaction, reservation: PdcReservation, now: float, obs: PdcObservation | None = None, config: PdcTransactionConfig = DEFAULT_CONFIG) -> PdcStep:
    """Cancellation: the same safe return path, never a release of the radiator."""
    return request_return(tx, reservation, now, obs, reason=FAILURE_CANCELLED, config=config)


def _hold(tx: PdcTransaction) -> PdcTransaction:
    if tx.phase in (PHASE_IDLE, PHASE_RUNNING, PHASE_OVERRIDE_HOLD):
        return tx
    return replace(tx, phase=PHASE_OVERRIDE_HOLD, deadline=None)


def recover(tx: PdcTransaction, reservation: PdcReservation, now: float, obs: PdcObservation | None, config: PdcTransactionConfig = DEFAULT_CONFIG) -> PdcStep:
    """After a restart or reconnection with a persisted transaction.

    In-flight commands are unknown, so every non-idle phase except a verified
    ``running`` goes through the return path with a new token (old events and
    timers become stale). Nothing is released without a confirmed ``off`` split.
    """
    _finite("now", now)
    if tx.phase == PHASE_IDLE:
        return PdcStep(tx, reservation)
    if tx.phase == PHASE_RUNNING and obs is not None and obs.split_mode == STATE_HEAT and obs.pdc_mode == STATE_HEAT:
        return PdcStep(tx, reservation)
    if tx.phase == PHASE_OVERRIDE_HOLD and (obs is None or obs.override_off is not True):
        return PdcStep(tx, reservation)
    renewed = replace(tx, token=tx.token + 1)
    held = reservation
    if tx.reservation_held and reservation.holder == tx.room_key and reservation.token == tx.token:
        held = PdcReservation(tx.room_key, renewed.token)
    fresh_obs = None if obs is None else replace(obs, token=renewed.token)
    if fresh_obs is None or fresh_obs.override_off is not True:
        return PdcStep(_hold(renewed), held, accepted=False, reason=DENIED_OVERRIDE)
    return _begin_return(renewed, held, fresh_obs, now, FAILURE_RECOVERED, config)


def observe(tx: PdcTransaction, reservation: PdcReservation, obs: PdcObservation, now: float, config: PdcTransactionConfig = DEFAULT_CONFIG) -> PdcStep:
    """Advance on an event or a timer tick. Idempotent: a duplicate observation emits nothing twice."""
    _finite("now", now)
    if obs.token != tx.token:
        return PdcStep(tx, reservation, accepted=False, reason=IGNORED_STALE)
    phase = tx.phase
    if phase in (PHASE_IDLE, PHASE_RUNNING, PHASE_RETURN_UNCONFIRMED):
        return PdcStep(tx, reservation, accepted=False, reason=IGNORED_IDLE if phase == PHASE_IDLE else IGNORED_NO_CHANGE)
    if phase == PHASE_OVERRIDE_HOLD:
        return PdcStep(tx, reservation, accepted=False, reason=IGNORED_HOLD)
    if obs.override_off is not True:
        return PdcStep(_hold(tx), reservation, accepted=False, reason=DENIED_OVERRIDE)
    deadline = tx.deadline if tx.deadline is not None else now
    expired = now >= deadline
    if phase == PHASE_WAITING_RADIATOR:
        if obs.radiator_stopped is not True and not expired:
            return PdcStep(tx, reservation, accepted=False, reason=IGNORED_NO_CHANGE)
        if obs.conditions_ok is True:
            waiting = _enter(tx, PHASE_WAITING_TARGET, now, config.target_timeout_s)
            return PdcStep(waiting, reservation, (_cmd(waiting, CMD_SET_TARGET),))
        failure = FAILURE_TRV_UNCONFIRMED if obs.radiator_stopped is not True else FAILURE_CONDITIONS_CHANGED
        return _begin_return(tx, reservation, obs, now, failure, config)
    if phase == PHASE_WAITING_TARGET:
        if obs.conditions_ok is True:
            waiting = _enter(tx, PHASE_WAITING_HEAT, now, config.heat_timeout_s)
            return PdcStep(waiting, reservation, (_cmd(waiting, CMD_PDC_HEAT),))
        if obs.target_confirmed is not True and not expired:
            return PdcStep(tx, reservation, accepted=False, reason=IGNORED_NO_CHANGE)
        failure = FAILURE_TARGET_UNCONFIRMED if obs.target_confirmed is not True else FAILURE_CONDITIONS_CHANGED
        return _begin_return(tx, reservation, obs, now, failure, config)
    if phase == PHASE_WAITING_HEAT:
        heated = obs.split_mode == STATE_HEAT
        if not heated and not expired:
            return PdcStep(tx, reservation, accepted=False, reason=IGNORED_NO_CHANGE)
        if heated and obs.conditions_ok is True:
            return PdcStep(replace(tx, phase=PHASE_RUNNING, phase_started_at=now, deadline=None), reservation)
        failure = FAILURE_SPLIT_UNCONFIRMED if not heated else FAILURE_CONDITIONS_CHANGED
        return _begin_return(tx, reservation, obs, now, failure, config)
    if phase == PHASE_RETURNING:
        if obs.split_mode == STATE_OFF:
            return _finish_return(tx, reservation, ())
        if not expired:
            return PdcStep(tx, reservation, accepted=False, reason=IGNORED_NO_CHANGE)
        direct = _enter(replace(tx, direct_off_sent=True), PHASE_RETURNING_DIRECT, now, config.direct_off_timeout_s)
        return PdcStep(direct, reservation, (_cmd(direct, CMD_SPLIT_OFF_DIRECT),))
    if phase == PHASE_RETURNING_DIRECT:
        if obs.split_mode == STATE_OFF:
            return _finish_return(tx, reservation, ())
        if not expired:
            return PdcStep(tx, reservation, accepted=False, reason=IGNORED_NO_CHANGE)
        stuck = replace(tx, phase=PHASE_RETURN_UNCONFIRMED, deadline=None, failure=FAILURE_RETURN_UNCONFIRMED)
        return PdcStep(stuck, reservation, accepted=False, reason=FAILURE_RETURN_UNCONFIRMED)
    raise AssertionError(phase)
