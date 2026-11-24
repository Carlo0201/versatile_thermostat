"""Pure command engine of the boiler orchestration (M2-B).

The module decides what the boiler controller must do about the master climate
(``climate.set_hvac_mode`` ``off``/``heat``): send, wait for the confirmation,
retry, give up, or stay silent. It is deliberately pure: no Home Assistant
import, no I/O, no asyncio, no global clock and no service call. Time is an
explicit absolute Unix timestamp, models are frozen and every function returns
new values. The Home Assistant manager executes the services and feeds the
results back through the ``on_service_*`` functions.

Terminology (``M2-B-SPEC.md``):

* a **request** is one wish to bring the master to ``heat`` or ``off``. It owns
  at most ``MAX_SENDS`` (4) sends: the first one plus three retries after waits
  of 60, 120 and 300 s. A retry follows a failure (the service raised or timed
  out, or the master did not report the commanded mode within 60 s of the
  service completion);
* the **service completion** (``service_completed_at``) only means that the
  service returned without error. It is never a confirmation;
* the **confirmation** is the observed master mode equal to the commanded mode
  (``last_confirmation_at``). ``unknown`` and ``unavailable`` never confirm;
* an M1 action of the opposite mode **supersedes** a pending request at once,
  without waiting for it: the opposite command is sent right away. A late
  observation of the replaced mode is not a confirmation of the new request
  (the confirmation compares with the mode of the request being tracked);
* an M1 action ``none`` supersedes a request whose mode has not been observed;
  when the master already shows the requested mode the request is confirmed
  (after a ``heat`` the M1 action is ``none``/``maintain``);
* after ``MAX_SENDS`` failures the request is **exhausted**: no further command
  towards that mode until the master changes observed state or the M1 action
  changes. A communication fault therefore cannot produce a storm of switches.

Ownership (``evaluate_ownership``) and the post setup/reload settling
(``update_settling``) are separate pure gates: ``step`` sends nothing unless the
caller is the owner and the controller is settled, and it cancels every
pending request as soon as the ownership is lost, without compensating
commands.

Timing conventions, all inclusive on the limit: a confirmation observed at
exactly 60 s after the service completion still counts (confirmation is checked
before the timeout); a retry is due exactly at ``retry_at``; the settling
window is complete at exactly 30 s.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

# --------------------------------------------------------------------- constants

ACTION_NONE: Final = "none"
COMMAND_HEAT: Final = "heat"
COMMAND_OFF: Final = "off"
COMMAND_MODES: Final = (COMMAND_HEAT, COMMAND_OFF)
# Master modes the controller understands. Anything else is not a known mode.
MASTER_KNOWN_MODES: Final = (COMMAND_OFF, COMMAND_HEAT)

CONFIRMATION_TIMEOUT_S: Final = 60.0
RETRY_DELAYS_S: Final = (60.0, 120.0, 300.0)
MAX_SENDS: Final = 1 + len(RETRY_DELAYS_S)
SERVICE_TIMEOUT_S: Final = 30.0
# A ``sending`` record older than this has lost its service task.
SEND_STALE_AFTER_S: Final = SERVICE_TIMEOUT_S + 5.0
# The ``heat`` record is saved before the service with this maximum wait.
HEAT_PERSIST_WAIT_S: Final = 5.0
# Post setup/reload settling: two consecutive valid evaluations this far apart.
SETTLE_MIN_INTERVAL_S: Final = 30.0

# Request statuses (``command_status`` of the diagnostics).
STATUS_SENDING: Final = "sending"
STATUS_PENDING: Final = "pending"
STATUS_CONFIRMED: Final = "confirmed"
STATUS_FAILED: Final = "failed"
STATUS_UNCONFIRMED: Final = "unconfirmed"
STATUS_SUPERSEDED: Final = "superseded"
STATUS_CANCELLED: Final = "cancelled"
STATUSES: Final = (
    STATUS_SENDING,
    STATUS_PENDING,
    STATUS_CONFIRMED,
    STATUS_FAILED,
    STATUS_UNCONFIRMED,
    STATUS_SUPERSEDED,
    STATUS_CANCELLED,
)

# Control statuses (``control_status`` of the diagnostics).
CONTROL_INACTIVE: Final = "inactive"
CONTROL_STARTING: Final = "starting"
CONTROL_SETTLING: Final = "settling"
CONTROL_BLOCKED_EXTERNAL_OWNER: Final = "blocked_external_owner"
CONTROL_BLOCKED_MASTER_UNKNOWN: Final = "blocked_master_unknown"
CONTROL_READY: Final = "ready"
CONTROL_PENDING: Final = "pending"
CONTROL_CONFIRMED: Final = "confirmed"
CONTROL_COMMAND_FAILED: Final = "command_failed"
CONTROL_STATUSES: Final = (
    CONTROL_INACTIVE,
    CONTROL_STARTING,
    CONTROL_SETTLING,
    CONTROL_BLOCKED_EXTERNAL_OWNER,
    CONTROL_BLOCKED_MASTER_UNKNOWN,
    CONTROL_READY,
    CONTROL_PENDING,
    CONTROL_CONFIRMED,
    CONTROL_COMMAND_FAILED,
)

# Reasons for which the controller is not the owner.
BLOCK_NOT_CONTROL: Final = "not_control"
BLOCK_NOT_ACTIVE: Final = "not_active"
BLOCK_NO_EXTERNAL_OWNER: Final = "no_external_owner"
BLOCK_EXTERNAL_OWNER: Final = "external_owner"
BLOCK_MASTER_UNKNOWN: Final = "master_unknown"
BLOCK_NO_VALID_EVALUATION: Final = "no_valid_evaluation"

# Transition events (one logbook entry each, never one per tick).
EVENT_SENT: Final = "sent"
EVENT_CONFIRMED: Final = "confirmed"
EVENT_FAILED: Final = "failed"
EVENT_EXHAUSTED: Final = "exhausted"
EVENT_SUPERSEDED: Final = "superseded"
EVENT_CANCELLED: Final = "cancelled"
EVENT_UNCONFIRMED: Final = "unconfirmed"

ERROR_CONFIRMATION_TIMEOUT: Final = "confirmation_timeout"
ERROR_SERVICE_TIMEOUT: Final = "service_timeout"
ERROR_SEND_LOST: Final = "send_lost"

OWNER_CONTROLLER: Final = "controller"

STORE_COMMAND_KEY: Final = "command"
STORE_VERSION: Final = 2


def _finite(value: Any) -> float | None:
    """A finite float or ``None`` (booleans and strings are not timestamps)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


# ------------------------------------------------------------------------ models


@dataclass(frozen=True, slots=True)
class CommandRecord:
    """Persisted state of the last request (``{mode, at, attempts, status}`` and context).

    ``at`` is the start of the last send, ``send_id`` identifies that send so a
    late service result of an older send is ignored. ``last_confirmation_at``
    is carried over from one request to the next.
    """

    mode: str
    status: str
    attempts: int
    at: float
    send_id: int = 0
    service_completed_at: float | None = None
    last_confirmation_at: float | None = None
    base_master: str | None = None  # master mode observed when the request started (context only)
    retry_at: float | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        """Validate the record."""
        if self.mode not in COMMAND_MODES:
            raise ValueError(f"unknown command mode {self.mode!r}")
        if self.status not in STATUSES:
            raise ValueError(f"unknown command status {self.status!r}")
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int) or not 0 <= self.attempts <= MAX_SENDS:
            raise ValueError("attempts must be an integer between 0 and MAX_SENDS")
        for name in ("at", "service_completed_at", "last_confirmation_at", "retry_at"):
            value = getattr(self, name)
            if value is not None and _finite(value) is None:
                raise ValueError(f"{name} must be a finite number")
        if _finite(self.at) is None:
            raise ValueError("at must be a finite number")

    @property
    def is_active(self) -> bool:
        """Sending, waiting for the confirmation, or waiting for a retry."""
        return self.status in (STATUS_SENDING, STATUS_PENDING) or (self.status == STATUS_FAILED and self.retry_at is not None)

    @property
    def is_exhausted(self) -> bool:
        """Failed after ``MAX_SENDS`` sends: no more commands towards this mode."""
        return self.status == STATUS_FAILED and self.retry_at is None and self.attempts >= MAX_SENDS


@dataclass(frozen=True, slots=True)
class ExternalOwner:
    """State of one external controller that must be ``off`` (``None``: entity missing)."""

    entity_id: str
    state: str | None

    @property
    def is_off(self) -> bool:
        """Only the exact state ``off`` releases the ownership."""
        return self.state == "off"


@dataclass(frozen=True, slots=True)
class Ownership:
    """Result of the ownership interlock."""

    is_owner: bool
    block_reason: str | None
    external_owners: tuple[ExternalOwner, ...] = ()


@dataclass(frozen=True, slots=True)
class CommandInputs:
    """Everything ``step`` needs, from one coherent evaluation."""

    now: float
    action: str  # M1 action: none / heat / off
    master_mode: str | None  # observed master mode
    ownership: Ownership
    settled: bool
    shutting_down: bool = False

    def __post_init__(self) -> None:
        """Validate the inputs."""
        if _finite(self.now) is None:
            raise ValueError("now must be a finite number")
        if self.action not in (ACTION_NONE, *COMMAND_MODES):
            raise ValueError(f"unknown action {self.action!r}")


@dataclass(frozen=True, slots=True)
class CommandEvent:
    """One transition, for the logbook and the logs."""

    kind: str
    mode: str | None
    attempt: int
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class CommandStep:
    """Decision of one ``step``.

    ``record`` is the record to keep. ``send`` is the mode to send now (the
    caller must revalidate with fresh inputs and then call ``start_send``);
    ``attempt`` is the number of that send. ``wake_at`` is the next instant at
    which ``step`` must run again even if nothing is observed.
    """

    record: CommandRecord | None
    send: str | None
    attempt: int
    control_status: str
    wake_at: float | None
    events: tuple[CommandEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class SettlingState:
    """Progress of the post setup/reload settling."""

    anchor_at: float | None = None
    anchor_action: str | None = None
    settled: bool = False


# --------------------------------------------------------------------- ownership


def evaluate_ownership(
    *,
    control_requested: bool,
    active: bool,
    external_owners: Sequence[ExternalOwner],
    master_mode: str | None,
    evaluation_valid: bool,
) -> Ownership:
    """The controller owns the master only if every condition holds.

    1. the mode is ``control`` (``control_requested``);
    2. the manager generation is current and not cancelled (``active``);
    3. every external controller has the exact state ``off`` (``on``,
       ``unknown``, ``unavailable`` and a missing entity all block, and so does
       an empty list: the disappearance of the external controller never grants
       the ownership);
    4. the master has a known mode, ``off`` or ``heat``;
    and a complete evaluation (valid inputs) has been seen since the start.
    """
    owners = tuple(external_owners)
    if not control_requested:
        return Ownership(False, BLOCK_NOT_CONTROL, owners)
    if not active:
        return Ownership(False, BLOCK_NOT_ACTIVE, owners)
    if not owners:
        return Ownership(False, BLOCK_NO_EXTERNAL_OWNER, owners)
    if any(not owner.is_off for owner in owners):
        return Ownership(False, BLOCK_EXTERNAL_OWNER, owners)
    if master_mode not in MASTER_KNOWN_MODES:
        return Ownership(False, BLOCK_MASTER_UNKNOWN, owners)
    if not evaluation_valid:
        return Ownership(False, BLOCK_NO_VALID_EVALUATION, owners)
    return Ownership(True, None, owners)


def control_status_when_blocked(ownership: Ownership) -> str:
    """``control_status`` of a controller that is not the owner."""
    reason = ownership.block_reason
    if reason in (BLOCK_EXTERNAL_OWNER, BLOCK_NO_EXTERNAL_OWNER):
        return CONTROL_BLOCKED_EXTERNAL_OWNER
    if reason == BLOCK_MASTER_UNKNOWN:
        return CONTROL_BLOCKED_MASTER_UNKNOWN
    if reason == BLOCK_NO_VALID_EVALUATION:
        return CONTROL_STARTING
    return CONTROL_INACTIVE


# ---------------------------------------------------------------------- settling


def update_settling(state: SettlingState, *, now: float, valid: bool, action: str) -> SettlingState:
    """Advance the settling after setup or reload.

    No command, ``off`` included, until two consecutive valid evaluations at
    least ``SETTLE_MIN_INTERVAL_S`` apart proposed the same M1 action: during a
    reload the rooms are transiently not eligible and an ``off`` proposed then
    is not trustworthy. An invalid evaluation, a different action or a clock
    that went backwards restarts the window. Once settled the state is kept.
    """
    if state.settled:
        return state
    if not valid:
        return SettlingState()
    if state.anchor_at is None or state.anchor_action != action or now < state.anchor_at:
        return SettlingState(anchor_at=now, anchor_action=action)
    if now - state.anchor_at >= SETTLE_MIN_INTERVAL_S:
        return replace(state, settled=True)
    return state


# ------------------------------------------------------------------ transitions


def _retry_delay(attempts: int) -> float:
    return RETRY_DELAYS_S[attempts - 1]


def _fail(record: CommandRecord, now: float, error: str) -> tuple[CommandRecord, tuple[CommandEvent, ...]]:
    """Register a failed send: schedule the next retry or declare the request exhausted."""
    failed = replace(record, status=STATUS_FAILED, error=error)
    events = [CommandEvent(EVENT_FAILED, record.mode, record.attempts, error)]
    if record.attempts >= MAX_SENDS:
        failed = replace(failed, retry_at=None)
        events.append(CommandEvent(EVENT_EXHAUSTED, record.mode, record.attempts, error))
    else:
        failed = replace(failed, retry_at=now + _retry_delay(record.attempts))
    return failed, tuple(events)


def start_send(
    previous: CommandRecord | None,
    *,
    mode: str,
    attempt: int,
    now: float,
    send_id: int,
    base_master: str | None,
) -> tuple[CommandRecord, CommandEvent]:
    """Record that a send begins (after the revalidation, before the service call).

    ``attempt`` 1 starts a new request; a higher number must continue a failed
    request of the same mode that is waiting for its retry.
    """
    if mode not in COMMAND_MODES:
        raise ValueError(f"unknown command mode {mode!r}")
    if not 1 <= attempt <= MAX_SENDS:
        raise ValueError("attempt out of range")
    last_confirmation = None if previous is None else previous.last_confirmation_at
    if attempt == 1:
        if previous is not None and (previous.is_active or previous.is_exhausted):
            raise ValueError("a new request cannot replace an unresolved one")
        base = base_master
    else:
        if previous is None or previous.mode != mode or previous.status != STATUS_FAILED or previous.retry_at is None or previous.attempts != attempt - 1:
            raise ValueError("a retry needs the failed request of the same mode")
        base = previous.base_master
    record = CommandRecord(
        mode=mode,
        status=STATUS_SENDING,
        attempts=attempt,
        at=now,
        send_id=send_id,
        last_confirmation_at=last_confirmation,
        base_master=base,
    )
    return record, CommandEvent(EVENT_SENT, mode, attempt)


def on_service_completed(record: CommandRecord | None, *, now: float, send_id: int) -> CommandRecord | None:
    """The service returned without error: pending, not confirmed."""
    if record is None or record.send_id != send_id:
        return record
    if record.status == STATUS_SENDING:
        return replace(record, status=STATUS_PENDING, service_completed_at=now, error=None)
    if record.status == STATUS_CONFIRMED and record.service_completed_at is None:
        return replace(record, service_completed_at=now)
    return record


def on_service_failed(record: CommandRecord | None, *, now: float, send_id: int, error: str) -> tuple[CommandRecord | None, tuple[CommandEvent, ...]]:
    """The service raised or timed out. A request already confirmed or replaced is untouched."""
    if record is None or record.send_id != send_id or record.status != STATUS_SENDING:
        return record, ()
    return _fail(record, now, error)


def restart_record(record: CommandRecord | None) -> tuple[CommandRecord | None, tuple[CommandEvent, ...]]:
    """After a restart a request that was not confirmed becomes ``unconfirmed``.

    It is never considered confirmed: the master state is read again.
    """
    if record is None or record.status not in (STATUS_SENDING, STATUS_PENDING, STATUS_FAILED):
        return record, ()
    restored = replace(record, status=STATUS_UNCONFIRMED, retry_at=None)
    return restored, (CommandEvent(EVENT_UNCONFIRMED, record.mode, record.attempts, "restart"),)


def _superseded(record: CommandRecord, detail: str) -> tuple[CommandRecord, tuple[CommandEvent, ...]]:
    return replace(record, status=STATUS_SUPERSEDED, retry_at=None), (CommandEvent(EVENT_SUPERSEDED, record.mode, record.attempts, detail),)


def _advance(record: CommandRecord, inputs: CommandInputs) -> tuple[CommandRecord, tuple[CommandEvent, ...]]:
    """Move an unresolved request (active or exhausted) according to the observation."""
    now = inputs.now
    action = inputs.action
    if action in COMMAND_MODES and action != record.mode:
        # The opposite action replaces the request immediately.
        return _superseded(record, f"action:{action}")
    if inputs.master_mode == record.mode:
        confirmed = replace(record, status=STATUS_CONFIRMED, retry_at=None, last_confirmation_at=now, error=None)
        return confirmed, (CommandEvent(EVENT_CONFIRMED, record.mode, record.attempts),)
    if action != record.mode:
        return _superseded(record, f"action:{action}")
    if record.status == STATUS_SENDING and now >= record.at + SEND_STALE_AFTER_S:
        return _fail(record, now, ERROR_SEND_LOST)
    if record.status == STATUS_PENDING and now >= (record.service_completed_at if record.service_completed_at is not None else record.at) + CONFIRMATION_TIMEOUT_S:
        return _fail(record, now, ERROR_CONFIRMATION_TIMEOUT)
    # An exhausted request stays blocked while the master keeps its state and the M1 action
    # stays the same. A change of the observed master state is the confirmation above (the
    # master has only two known modes), and a change of action is the supersede above.
    return record, ()


def _wake_at(record: CommandRecord | None) -> float | None:
    if record is None or not record.is_active:
        return None
    if record.status == STATUS_PENDING:
        return (record.service_completed_at if record.service_completed_at is not None else record.at) + CONFIRMATION_TIMEOUT_S
    if record.status == STATUS_FAILED:
        return record.retry_at
    return record.at + SEND_STALE_AFTER_S


def step(record: CommandRecord | None, inputs: CommandInputs) -> CommandStep:
    """One decision. Idempotent: applying its record and calling again adds no event."""
    ownership = inputs.ownership
    events: list[CommandEvent] = []
    current = record

    if not ownership.is_owner or inputs.shutting_down:
        # Everything pending is cancelled: confirmations, retries, timers. No compensation.
        if current is not None and current.is_active:
            detail = "shutdown" if ownership.is_owner else ownership.block_reason
            current = replace(current, status=STATUS_CANCELLED, retry_at=None)
            events.append(CommandEvent(EVENT_CANCELLED, current.mode, current.attempts, detail))
        status = CONTROL_INACTIVE if inputs.shutting_down else control_status_when_blocked(ownership)
        return CommandStep(current, None, 0, status, None, tuple(events))

    if current is not None and (current.is_active or current.is_exhausted):
        current, advanced = _advance(current, inputs)
        events.extend(advanced)

    send: str | None = None
    attempt = 0
    if current is not None and current.status == STATUS_FAILED and current.retry_at is not None:
        if inputs.now >= current.retry_at:
            send, attempt = current.mode, current.attempts + 1
    elif current is None or not (current.is_active or current.is_exhausted):
        if inputs.action in COMMAND_MODES and inputs.master_mode in MASTER_KNOWN_MODES and inputs.master_mode != inputs.action:
            send, attempt = inputs.action, 1
    if send is not None and not inputs.settled:
        send, attempt = None, 0

    if current is not None and current.is_exhausted:
        status = CONTROL_COMMAND_FAILED
    elif send is not None or (current is not None and current.is_active):
        status = CONTROL_PENDING
    elif not inputs.settled:
        status = CONTROL_SETTLING
    elif current is not None and current.status == STATUS_CONFIRMED and inputs.master_mode == current.mode:
        status = CONTROL_CONFIRMED
    else:
        status = CONTROL_READY
    wake = None if send is not None else _wake_at(current)
    return CommandStep(current, send, attempt, status, wake, tuple(events))


# ------------------------------------------------------------------ diagnostics


def command_diagnostics(record: CommandRecord | None, ownership: Ownership, control_status: str) -> dict[str, Any]:
    """The M2-B fields of the diagnostics (schema 1.1, compatible additions)."""
    return {
        "control_status": control_status,
        "command_status": None if record is None else record.status,
        "commanded_mode": None if record is None else record.mode,
        "last_command_at": None if record is None or record.attempts == 0 else record.at,
        "last_confirmation_at": None if record is None else record.last_confirmation_at,
        "service_completed_at": None if record is None else record.service_completed_at,
        "command_attempts": 0 if record is None else record.attempts,
        "command_retry_at": None if record is None else record.retry_at,
        "external_owners": [{"entity_id": owner.entity_id, "state": owner.state} for owner in ownership.external_owners],
    }


# ------------------------------------------------------------------ persistence


def record_to_dict(record: CommandRecord | None) -> dict[str, Any] | None:
    """Serialize for the ``Store`` (``retry_at`` is not kept: a restart resolves the request)."""
    if record is None:
        return None
    return {
        "mode": record.mode,
        "at": record.at,
        "attempts": record.attempts,
        "status": record.status,
        "send_id": record.send_id,
        "service_completed_at": record.service_completed_at,
        "last_confirmation_at": record.last_confirmation_at,
        "base_master": record.base_master,
        "error": record.error,
    }


def record_from_dict(value: Any) -> CommandRecord | None:
    """Parse a stored record; anything invalid reads as no record."""
    if not isinstance(value, Mapping):
        return None
    try:
        at = _finite(value.get("at"))
        attempts = value.get("attempts")
        if at is None or isinstance(attempts, bool) or not isinstance(attempts, int):
            return None
        send_id = value.get("send_id", 0)
        error = value.get("error")
        base_master = value.get("base_master")
        return CommandRecord(
            mode=value.get("mode"),
            status=value.get("status"),
            attempts=attempts,
            at=at,
            send_id=send_id if isinstance(send_id, int) and not isinstance(send_id, bool) and send_id >= 0 else 0,
            service_completed_at=_finite(value.get("service_completed_at")),
            last_confirmation_at=_finite(value.get("last_confirmation_at")),
            base_master=base_master if isinstance(base_master, str) else None,
            retry_at=None,
            error=error if isinstance(error, str) else None,
        )
    except (TypeError, ValueError):
        return None


def migrate_store_data(old_major_version: int, old_minor_version: int, data: Mapping[str, Any]) -> dict[str, Any]:
    """Migrate the stored data to ``STORE_VERSION``.

    Version 1 (M2-A) holds only the cycle: version 2 adds ``command`` (absent).
    A newer version cannot be read by this code: ``NotImplementedError`` is
    raised and the caller falls back to a new observation.
    """
    del old_minor_version
    if old_major_version == 1:
        migrated = dict(data) if isinstance(data, Mapping) else {}
        migrated.setdefault(STORE_COMMAND_KEY, None)
        return migrated
    if old_major_version == STORE_VERSION:
        return dict(data) if isinstance(data, Mapping) else {}
    raise NotImplementedError(f"unsupported store version {old_major_version}")
