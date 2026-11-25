"""Home Assistant independent engine of the PDC runtime (W2).

``feature_heating_pdc_manager.py`` is the thin Home Assistant glue (listeners,
``Store``, sensors, services). Everything that decides or sequences lives here so
that it can be tested with fakes and a controlled clock, without Home Assistant:

* one serialized pass per room (a worker that re-runs when an event arrived while
  it was awaiting, so no event is lost during a transaction);
* ``observe``: evaluation and diagnostics only, **no command, no transaction**;
* ``control``: the M3 policy decides, the M4 machine sequences the hand-over, the
  M5 model tracks the manual override; commands go through :class:`PdcPorts`;
* the interlock on ``external_owner_entity_ids``, the shared reservation, the
  write-ahead persistence, the recovery after a restart;
* the entry points of the general suspension (:meth:`PdcRuntime.async_suspend`).

Rules that this engine adds to the pure modules (documented in
``M4-M5-RUNTIME-ESITO.md``):

* every state that decides a command is read again after each ``await``; a command
  whose token is no longer current, or whose preconditions no longer hold, is not
  sent;
* the PDC HEAT command is re-validated against a fresh read immediately before the
  call;
* the inhibition release is re-validated (split and PDC VTherm confirmed ``off``)
  immediately before the call and is never executed while the general suspension
  holds the radiator inhibition;
* losing the interlock freezes the room: no command, no release, no compensation;
* a failure of the persistence never blocks a stop or a return (it only blocks the
  *start* of a transaction, whose ownership claim must be durable first).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final, Protocol

from .heating_pdc_collector import (
    MODE_CONTROL,
    MODE_DISABLED,
    MODE_OBSERVE,
    OWNER_CONTROLLER,
    OWNER_OBSERVER,
    STATE_BLOCKED,
    STATE_ERROR,
    STATE_STARTING,
    STATE_SUSPENDED,
    PdcCollection,
    PdcConfig,
    PdcLocalInputs,
    PdcOtherView,
    PdcRoomConfig,
    aggregate_others,
    build_observation,
    build_pdc_diagnostics,
    collect_pdc_snapshot,
    device_state,
    empty_pdc_diagnostics,
    json_safe,
    lookup,
    radiator_released_while_running,
    read_inhibition,
    read_other_view,
    to_timestamp,
    unsafe_during_start,
)
from .heating_pdc_policy import (
    EMPTY_CONTEXT,
    PDC_ACTION_SET_SETPOINT,
    PDC_ACTION_START,
    PDC_ACTION_STOP,
    SPLIT_ACTIVE_MODES,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    PdcCycle,
    PdcDecision,
    PdcSnapshot,
    PdcStartContext,
    evaluate_pdc,
    observe_split_mode,
    other_pdc_free,
    override_protection_stop,
    parse_pdc_cycle,
    split_setpoint,
)
from .heating_pdc_provenance import (
    EMPTY_REGISTER,
    PROVENANCE_UNKNOWN,
    PROVENANCE_USER,
    REGISTERED_SERVICES,
    SOURCE_DEVICE_CHANGE,
    SOURCE_DEVICE_FOUND_RUNNING,
    SOURCE_USER_COMMAND,
    ExpectedEffect,
    ExpectedRegister,
    OverrideState,
    classify_device_change,
    device_found_running,
    is_user_command,
    override_expired,
    register_command,
    restore_override,
    start_override,
)
from .heating_pdc_transaction import (
    CMD_INHIBIT_RADIATOR,
    CMD_PDC_HEAT,
    CMD_PDC_OFF,
    CMD_RELEASE_INHIBITION,
    CMD_RELEASE_RESERVATION,
    CMD_SET_TARGET,
    CMD_SPLIT_OFF_DIRECT,
    FAILURE_CONDITIONS_CHANGED,
    FAILURE_OVERRIDE_ENDED,
    FAILURE_OVERRIDE_PROTECTION,
    FAILURE_RADIATOR_RELEASED,
    FAILURE_STOP,
    FAILURE_SUSPENDED,
    PHASE_IDLE,
    PHASE_OVERRIDE_HOLD,
    PHASE_RELEASING,
    PHASE_RETURN_UNCONFIRMED,
    PHASE_RUNNING,
    PHASE_WAITING_HEAT,
    RETURN_LIKE_PHASES,
    START_PHASES,
    PdcCommand,
    PdcReservation,
    PdcStep,
    PdcTransaction,
    adopt_manual,
    confirm_release,
    next_wakeup,
    observe,
    recover,
    record_inhibition_result,
    request_return,
    request_start,
    reservation_from_dict,
    reservation_to_dict,
    transaction_from_dict,
    transaction_to_dict,
)

_LOGGER = logging.getLogger(__name__)

STORE_SCHEMA_VERSION: Final = 1
MAX_PASS_ITERATIONS: Final = 8
CLASSIFY_DELAY_S: Final = 1.0
MIRROR_DOMAINS: Final = ("input_boolean",)
PROTECTION_EMERGENCY_GAP_S: Final = 5.0
SUSPEND_REPEAT_S: Final = 30.0
SUSPEND_UNREACHABLE_S: Final = 90.0
SUSPEND_MAX_ATTEMPTS: Final = 5
SYNC_REPEAT_S: Final = 60.0
RECOVERY_WAIT_S: Final = 120.0
RECOVERY_RECHECK_S: Final = 5.0
PROTECTIVE_FAILURES: Final = (FAILURE_OVERRIDE_PROTECTION, FAILURE_SUSPENDED)
RETURN_WAIT_PHASES: Final = (*RETURN_LIKE_PHASES, PHASE_RETURN_UNCONFIRMED)

SUSPENSION_NONE: Final = "none"
SUSPENSION_SHUTTING_DOWN: Final = "shutting_down"
SUSPENSION_CONFIRMED: Final = "confirmed"
SUSPENSION_UNREACHABLE: Final = "unreachable"

REFUSED_NOT_CONTROL: Final = "not_in_control"
REFUSED_BLOCKED: Final = "ownership_blocked"
REFUSED_ALREADY: Final = "already_suspended"
REFUSED_NOT_SUSPENDED: Final = "not_suspended"


class PdcControlUnavailableError(RuntimeError):
    """``control`` was requested from a host that did not opt in (``allow_control``): nothing is created, nothing can be commanded."""


class PdcPorts(Protocol):
    """Everything the engine needs from its host. Implemented by the Home Assistant manager and by test fakes."""

    def now(self) -> float:
        """Current time, absolute Unix seconds."""

    def ready(self) -> bool:
        """Home Assistant finished starting (states are restored): before that nothing is decided."""

    def states(self) -> Mapping[str, Any]:
        """Live read access to the entity states (``get(entity_id)``)."""

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        """Call a service as the integration itself (no user context). Raises on failure."""

    async def set_inhibition(self, entity_id: str, stop_underlyings: bool) -> None:
        """VTherm API ``set_inhibition``. Raises on failure."""

    async def unset_inhibition(self, entity_id: str) -> None:
        """VTherm API ``unset_inhibition``. Raises on failure."""

    def inhibition_started(self, entity_id: str) -> bool | None:
        """VTherm API ``is_inhibition_started`` (``None`` when it cannot be asked)."""

    async def save(self, data: dict[str, Any]) -> None:
        """Persist the runtime data now (versioned store). Raises on failure."""

    def call_later(self, delay_s: float, callback: Callable[[], None]) -> Callable[[], None]:
        """One-shot timer; returns its cancel function."""

    def publish(self, room_key: str, state: str, attributes: dict[str, Any]) -> None:
        """Publish the room diagnostics."""

    def notify(self, notification_id: str, title: str | None, message: str | None) -> None:
        """Create (``message`` set) or dismiss (``None``) a persistent notification."""

    def spawn(self, coro: Awaitable[Any], name: str) -> asyncio.Future[Any]:
        """Run a background coroutine owned by the host (cancelled when the host stops)."""


@dataclass(slots=True)
class PdcSuspension:
    """General suspension of the heating, persisted so that it survives a restart."""

    active: bool = False
    since: float | None = None
    reason: str | None = None
    hold_radiator_inhibition: bool = True


@dataclass(frozen=True, slots=True)
class PdcSuspendResult:
    """Outcome of :meth:`PdcRuntime.async_suspend` / ``async_resume`` (state names are stable)."""

    accepted: bool
    state: str
    reason: str | None = None
    rooms: tuple[tuple[str, str], ...] = ()


@dataclass(slots=True)
class _Room:
    cfg: PdcRoomConfig
    tx: PdcTransaction
    ctx: PdcStartContext = EMPTY_CONTEXT
    cycle_raw: dict[str, Any] | None = None
    cycle: PdcCycle = field(default_factory=PdcCycle)
    cycle_seeded: bool = False
    override: OverrideState = field(default_factory=OverrideState)
    register: ExpectedRegister = EMPTY_REGISTER
    managed_changed_at: float | None = None
    resume_pending: bool = False
    recovered: bool = False
    inputs_ready: bool = False
    rerun: bool = False
    worker: asyncio.Future[Any] | None = None
    wake_cancel: Callable[[], None] | None = None
    wake_at: float | None = None
    decision: PdcDecision | None = None
    snapshot: PdcSnapshot | None = None
    split_action: str | None = None
    generation: int = 0
    last_error: str | None = None
    last_denied: str | None = None
    blocked_by: tuple[str, ...] = ()
    release_held: bool = False
    suspend_state: str = SUSPENSION_NONE
    suspend_attempts: int = 0
    emergency_last: float | None = None
    last_sync: tuple[Any, ...] | None = None
    last_state: str = STATE_STARTING
    last_attributes: dict[str, Any] = field(default_factory=dict)
    mirrored_managed: bool | None = None
    mirrored_override: bool | None = None
    classify_pending: int = 0
    recovery_since: float | None = None


class PdcRuntime:
    """The PDC engine of one central configuration."""

    def __init__(self, config: PdcConfig, ports: PdcPorts, *, stored: Mapping[str, Any] | None = None, allow_control: bool = False) -> None:
        if config.mode == MODE_CONTROL and not allow_control:
            # The execution code (transactions, commands, suspension) is not reachable unless the host explicitly opts in.
            raise PdcControlUnavailableError("the PDC control is not available in this release")
        self._config = config
        self._ports = ports
        self._epoch = 0
        self._stopped = False
        self._started = False
        self._reservation = PdcReservation()
        self._suspension = PdcSuspension()
        self._save_lock = asyncio.Lock()
        self._save_needed = False
        self._last_save_error: str | None = None
        self._rooms: dict[str, _Room] = {room.room_key: _Room(room, PdcTransaction(room.room_key)) for room in config.rooms}
        self._load_errors: list[str] = []
        if stored is not None:
            self._load(stored)

    # ------------------------------------------------------------------ status

    @property
    def mode(self) -> str:
        """Configured mode."""
        return self._config.mode

    @property
    def config(self) -> PdcConfig:
        """Validated configuration."""
        return self._config

    @property
    def reservation(self) -> PdcReservation:
        """The shared capacity reservation."""
        return self._reservation

    @property
    def suspension(self) -> PdcSuspension:
        """The persisted general suspension."""
        return self._suspension

    @property
    def room_keys(self) -> tuple[str, ...]:
        """Configured rooms."""
        return tuple(self._rooms)

    @property
    def load_errors(self) -> tuple[str, ...]:
        """Persisted sections that were invalid and dropped."""
        return tuple(self._load_errors)

    def transaction(self, room_key: str) -> PdcTransaction:
        """Current transaction of a room."""
        return self._rooms[room_key].tx

    def override(self, room_key: str) -> OverrideState:
        """Current manual override of a room."""
        return self._rooms[room_key].override

    def attributes(self, room_key: str) -> tuple[str, dict[str, Any]]:
        """Last published ``(state, attributes)`` of a room."""
        room = self._rooms[room_key]
        return room.last_state, room.last_attributes

    @property
    def is_controlling(self) -> bool:
        """Control mode with the runtime running."""
        return self._config.mode == MODE_CONTROL and self._started and not self._stopped

    def suspension_state(self) -> str:
        """Aggregate state of the general suspension: ``none``, ``shutting_down``, ``confirmed`` or ``unreachable``."""
        if not self._suspension.active:
            return SUSPENSION_NONE
        states = [room.suspend_state for room in self._rooms.values()]
        if SUSPENSION_UNREACHABLE in states:
            return SUSPENSION_UNREACHABLE
        if all(state == SUSPENSION_CONFIRMED for state in states):
            return SUSPENSION_CONFIRMED
        return SUSPENSION_SHUTTING_DOWN

    # --------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Begin evaluating. In ``control`` the first pass recovers a persisted transaction."""
        if self._started or self._stopped or self._config.mode == MODE_DISABLED:
            return
        self._started = True
        for room in self._rooms.values():
            attributes = empty_pdc_diagnostics(room.cfg.room_key, self._config.mode, owner=None)
            room.last_state, room.last_attributes = STATE_STARTING, attributes
            self._ports.publish(room.cfg.room_key, STATE_STARTING, attributes)
        self.request_evaluation()

    async def async_stop(self) -> None:
        """Cancel every timer and worker. Nothing is released, no compensation is sent."""
        if self._stopped:
            return
        self._stopped = True
        self._epoch += 1
        for room in self._rooms.values():
            self._cancel_wake(room)
            worker, room.worker = room.worker, None
            if worker is not None and not worker.done():
                worker.cancel()
        if self._save_needed:
            await self._save()

    # --------------------------------------------------------------- scheduling

    def request_evaluation(self, room_key: str | None = None) -> None:
        """Coalesce an evaluation request: one worker per room, re-run when an event arrived while it was busy."""
        if not self._started or self._stopped:
            return
        for key, room in self._rooms.items():
            if room_key is not None and key != room_key:
                continue
            room.rerun = True
            if room.worker is None or room.worker.done():
                room.worker = self._ports.spawn(self._work(room, self._epoch), f"pdc_runtime_{key}")

    async def _work(self, room: _Room, epoch: int) -> None:
        try:
            # No await between the last check of ``rerun`` and the end of the task: a request made after it spawns a new worker.
            while room.rerun and epoch == self._epoch:
                room.rerun = False
                await self.async_run_pass(room.cfg.room_key)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - never let the worker die silently
            _LOGGER.exception("PDC runtime: pass failed for %s", room.cfg.room_key)
            room.last_error = f"pass_error:{type(err).__name__}"

    async def async_drain(self) -> None:
        """Wait until every room worker is idle (tests and shutdown)."""
        for _ in range(100):
            workers = [r.worker for r in self._rooms.values() if r.worker is not None and not r.worker.done()]
            if not workers:
                return
            await asyncio.gather(*workers, return_exceptions=True)

    def _cancel_wake(self, room: _Room) -> None:
        if room.wake_cancel is not None:
            room.wake_cancel()
        room.wake_cancel = None
        room.wake_at = None

    def _schedule_wake(self, room: _Room, at: float | None, now: float) -> None:
        if at is None:
            self._cancel_wake(room)
            return
        if room.wake_at == at and room.wake_cancel is not None:
            return
        self._cancel_wake(room)
        epoch = self._epoch
        key = room.cfg.room_key

        def _fire() -> None:
            room.wake_cancel = None
            room.wake_at = None
            if epoch == self._epoch:
                self.request_evaluation(key)

        room.wake_at = at
        room.wake_cancel = self._ports.call_later(max(0.05, at - now), _fire)

    # ---------------------------------------------------------------- persistence

    def _store_data(self) -> dict[str, Any]:
        rooms: dict[str, Any] = {}
        for key, room in self._rooms.items():
            override = room.override
            rooms[key] = {
                "transaction": transaction_to_dict(room.tx),
                "override": {"active": override.active, "started_at": override.started_at, "until": override.until, "source": override.source, "provenance": override.provenance},
                "cycle": room.cycle_raw,
                "cycle_seeded": room.cycle_seeded,
                "managed_changed_at": room.managed_changed_at,
                "register": {k: [e.value, e.expires_at] for k, e in room.register.effects.items()},
                "resume_pending": room.resume_pending,
            }
        susp = self._suspension
        return json_safe(
            {
                "schema_version": STORE_SCHEMA_VERSION,
                "reservation": reservation_to_dict(self._reservation),
                "suspension": {"active": susp.active, "since": susp.since, "reason": susp.reason, "hold_radiator_inhibition": susp.hold_radiator_inhibition},
                "rooms": rooms,
            }
        )

    async def _save(self) -> bool:
        """Persist now. Returns ``False`` (and records the error) on failure; never raises."""
        async with self._save_lock:
            self._save_needed = False
            try:
                await self._ports.save(self._store_data())
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - a storage failure never blocks a stop
                self._save_needed = True
                self._last_save_error = f"store_save_failed:{type(err).__name__}"
                _LOGGER.warning("PDC runtime: cannot save the state: %s", err)
                return False
        self._last_save_error = None
        return True

    def _load(self, data: Mapping[str, Any]) -> None:
        """Rebuild the persisted state; anything invalid is dropped and reported (never guessed)."""
        if not isinstance(data, Mapping) or data.get("schema_version") != STORE_SCHEMA_VERSION:
            self._load_errors.append("store_invalid")
            return
        try:
            self._reservation = reservation_from_dict(data.get("reservation") or {})
        except (TypeError, ValueError):
            self._load_errors.append("reservation_invalid")
        susp = data.get("suspension")
        if isinstance(susp, Mapping) and isinstance(susp.get("active"), bool):
            reason = susp.get("reason")
            self._suspension = PdcSuspension(
                susp["active"], to_timestamp(susp.get("since")), reason if isinstance(reason, str) else None, susp.get("hold_radiator_inhibition") is not False
            )
        rooms = data.get("rooms")
        for key, room in self._rooms.items():
            item = rooms.get(key) if isinstance(rooms, Mapping) else None
            if isinstance(item, Mapping):
                self._load_room(key, room, item)
        self._reconcile_reservation()

    def _load_room(self, key: str, room: _Room, item: Mapping[str, Any]) -> None:
        try:
            room.tx = transaction_from_dict(item.get("transaction"), key)
        except (TypeError, ValueError):
            self._load_errors.append(f"transaction_invalid:{key}")
        stored = item.get("override")
        if isinstance(stored, Mapping) and isinstance(stored.get("active"), bool):
            source, provenance = stored.get("source"), stored.get("provenance")
            try:
                room.override = OverrideState(
                    stored["active"],
                    to_timestamp(stored.get("started_at")),
                    to_timestamp(stored.get("until")),
                    source if isinstance(source, str) else None,
                    provenance if isinstance(provenance, str) else "none",
                )
            except (TypeError, ValueError):
                self._load_errors.append(f"override_invalid:{key}")
        cycle = item.get("cycle")
        if isinstance(cycle, Mapping):
            room.cycle_raw = dict(cycle)
            room.cycle = parse_pdc_cycle(room.cycle_raw)
        room.cycle_seeded = item.get("cycle_seeded") is True
        room.managed_changed_at = to_timestamp(item.get("managed_changed_at"))
        effects: dict[str, ExpectedEffect] = {}
        register = item.get("register")
        if isinstance(register, Mapping):
            for field_key, pair in register.items():
                expires = to_timestamp(pair[1]) if isinstance(pair, (list, tuple)) and len(pair) == 2 else None
                if expires is not None:
                    effects[field_key] = ExpectedEffect(pair[0], expires)
        room.register = ExpectedRegister(effects)
        room.resume_pending = item.get("resume_pending") is True

    def _reconcile_reservation(self) -> None:
        """A reservation without a live holder (idle transaction, other token) would block the other PDC forever: drop it."""
        holder = self._reservation.holder
        if holder is None:
            return
        room = self._rooms.get(holder)
        if room is None or room.tx.phase == PHASE_IDLE or not room.tx.reservation_held or room.tx.token != self._reservation.token:
            self._reservation = PdcReservation()
            self._save_needed = True

    # -------------------------------------------------------------- reading

    @staticmethod
    def _managed_of(room: _Room) -> str:
        return STATE_ON if room.tx.phase != PHASE_IDLE else STATE_OFF

    def _local_inputs(self, room: _Room) -> PdcLocalInputs | None:
        if self._config.mode != MODE_CONTROL:
            return None
        return PdcLocalInputs(
            override=STATE_ON if room.override.active else STATE_OFF,
            managed=self._managed_of(room),
            managed_changed_at=room.managed_changed_at,
            cycle=room.cycle,
        )

    def _other_view(self, states: Mapping[str, Any], room: _Room) -> PdcOtherView:
        control = self._config.mode == MODE_CONTROL
        views = [read_other_view(states, other, self._managed_of(self._rooms[other.room_key]) if control else None) for other in self._config.others(room.cfg.room_key)]
        return aggregate_others(views)

    def _collect(self, room: _Room, now: float) -> PdcCollection:
        """One coherent read; in ``control`` the local cycle is updated first from the observed split mode."""
        states = self._ports.states()
        collection = collect_pdc_snapshot(states, room.cfg, self._config, local=self._local_inputs(room), other=self._other_view(states, room))
        if self._config.mode == MODE_CONTROL:
            self._seed_cycle(room, states)
            update = observe_split_mode(room.cycle_raw if room.cycle_raw is not None else {}, collection.snapshot.split_mode, collection.snapshot.target, now)
            if update is not None:
                room.cycle_raw = update
                room.cycle = parse_pdc_cycle(update)
                self._save_needed = True
                collection = collect_pdc_snapshot(states, room.cfg, self._config, local=self._local_inputs(room), other=self._other_view(states, room))
        room.snapshot = collection.snapshot
        room.split_action = collection.split_action
        return collection

    def _seed_cycle(self, room: _Room, states: Mapping[str, Any]) -> None:
        """Once: take the cycle of the legacy helper (read only) when the runtime has none."""
        if room.cycle_seeded or room.cycle_raw is not None:
            room.cycle_seeded = True
            return
        room.cycle_seeded = True
        self._save_needed = True
        view = lookup(states, room.cfg.cycle_entity_id)
        if view.state in (None, STATE_UNKNOWN, STATE_UNAVAILABLE):
            return
        try:
            value = json.loads(view.state)
        except ValueError:
            return
        if isinstance(value, Mapping):
            room.cycle_raw = dict(value)
            room.cycle = parse_pdc_cycle(value)

    def _blocked_by(self, room: _Room) -> tuple[str, ...]:
        """External owners that are not confirmed ``off`` (an unavailable or missing owner is not confirmed)."""
        states = self._ports.states()
        return tuple(entity_id for entity_id in room.cfg.external_owner_entity_ids if lookup(states, entity_id).state != STATE_OFF)

    # -------------------------------------------------------------- the pass

    async def async_run_pass(self, room_key: str) -> None:
        """One serialized evaluation of a room (the worker calls this; tests call it directly)."""
        room = self._rooms[room_key]
        if self._config.mode == MODE_DISABLED or self._stopped:
            return
        epoch = self._epoch
        now = self._ports.now()
        if not room.inputs_ready:
            states = self._ports.states()
            mandatory = (
                self._config.power_entity_id,
                self._config.central_mode_entity_id,
                room.cfg.radiator_entity_id,
                room.cfg.radiator_valve_entity_id,
                room.cfg.temperature_entity_id,
                room.cfg.window_entity_id,
                room.cfg.split_entity_id,
                room.cfg.pdc_entity_id,
                room.cfg.automatic_entity_id,
                room.cfg.schedule_stop_entity_id,
            )
            missing = [entity_id for entity_id in mandatory if lookup(states, entity_id).state is None]
            if missing or not self._ports.ready():
                detail = "waiting_for_entities:" + ",".join(missing[:4]) if missing else "waiting_for_home_assistant"
                self._publish_empty(room, STATE_STARTING, detail, now)
                return
            room.inputs_ready = True
        try:
            if self._config.mode == MODE_OBSERVE:
                self._observe_pass(room, now)
                return
            room.last_error = None
            room.blocked_by = self._blocked_by(room)
            if room.blocked_by:
                self._cancel_wake(room)
                self._publish_blocked(room, now)
                return
            await self._control_pass(room, epoch)
        except (TypeError, ValueError, KeyError) as err:  # defensive: never stop the engine, never command on a doubtful read
            _LOGGER.error("PDC runtime: evaluation failed for %s: %s", room_key, err)
            self._publish_empty(room, STATE_ERROR, f"evaluation_error:{type(err).__name__}", now)

    def _observe_pass(self, room: _Room, now: float) -> None:
        collection = self._collect(room, now)
        decision = evaluate_pdc(collection.snapshot, room.ctx, now, room.cfg.policy)
        room.ctx = decision.context
        room.decision = decision
        room.generation += 1
        self._publish(room, decision.reason, decision, owner=OWNER_OBSERVER, now=now)
        self._schedule_wake(room, decision.deadline if decision.deadline is not None and decision.deadline > now else None, now)

    async def _control_pass(self, room: _Room, epoch: int) -> None:
        for _ in range(MAX_PASS_ITERATIONS):
            if epoch != self._epoch:
                return
            now = self._ports.now()
            collection = self._collect(room, now)
            if not await self._control_step(room, collection, now):
                break
        if epoch != self._epoch:
            return
        if self._save_needed:
            await self._save()
        self._finish_pass(room, self._ports.now())

    async def _control_step(self, room: _Room, collection: PdcCollection, now: float) -> bool:
        """One decision step. Returns ``True`` when something changed or was sent (the caller reads again)."""
        if not room.recovered:
            if self._recovery_must_wait(room, collection, now):
                return False
            room.recovered = True
            return await self._recover(room, collection, now)
        if self._suspension.active:
            room.decision = evaluate_pdc(collection.snapshot, EMPTY_CONTEXT, now, room.cfg.policy)  # diagnostics only, never acted on
            return await self._suspend_step(room, collection, now)
        if room.classify_pending:
            # A split change is being classified (the baseline's 1 s + 2 s settle): do not decide on a change that may be a manual command.
            return False
        if room.override.active:
            room.decision = evaluate_pdc(collection.snapshot, EMPTY_CONTEXT, now, room.cfg.policy)  # diagnostics only, never acted on
            return await self._override_step(room, collection, now)
        phase = room.tx.phase
        if phase == PHASE_OVERRIDE_HOLD:
            # the override ended: the session is over
            return await self._return_step(room, collection, now, FAILURE_OVERRIDE_ENDED, exempt=False)
        if phase in START_PHASES or phase in RETURN_WAIT_PHASES:
            return await self._drive(room, collection, now)
        if phase == PHASE_RUNNING:
            return await self._running_step(room, collection, now)
        return await self._idle_step(room, collection, now)

    # ---------------------------------------------------------------- recovery

    @staticmethod
    def _recovery_must_wait(room: _Room, collection: PdcCollection, now: float) -> bool:
        """A persisted transaction is recovered against readable devices: wait (bounded) while the split or the PDC VTherm are not back yet."""
        if room.tx.phase == PHASE_IDLE:
            return False
        if room.recovery_since is None:
            room.recovery_since = now
        snapshot = collection.snapshot
        unreadable = any(mode in (None, STATE_UNKNOWN, STATE_UNAVAILABLE) for mode in (snapshot.split_mode, snapshot.pdc_mode))
        return unreadable and now - room.recovery_since < RECOVERY_WAIT_S

    async def _recover(self, room: _Room, collection: PdcCollection, now: float) -> bool:
        """After a restart: expire the override that ran out, then send any persisted transaction through the safe path."""
        override_before = room.override
        room.override = restore_override(room.override, now)
        if room.override != override_before:
            self._save_needed = True
            self._mirror_override(room)
        self._mirror_managed(room)
        if room.tx.phase == PHASE_IDLE:
            return True
        obs = build_observation(room.tx.token, collection.snapshot, now, room.cfg.policy, phase=room.tx.phase, override_off=not room.override.active)
        await self._apply(room, recover(room.tx, self._reservation, now, obs, self._config.transaction), now)
        return True

    # -------------------------------------------------------------- transaction

    async def _apply(self, room: _Room, step: PdcStep, now: float) -> bool:
        """Adopt a machine step: state first, persisted before the commands run (write-ahead), then the commands."""
        old_tx, old_res = room.tx, self._reservation
        changed = step.transaction != old_tx or step.reservation != old_res
        room.tx, self._reservation = step.transaction, step.reservation
        if changed:
            self._on_tx_changed(room, old_tx, now)
        if step.commands or changed:
            await self._save()
        if step.commands:
            await self._execute(room, step.commands)
        return changed or bool(step.commands)

    def _on_tx_changed(self, room: _Room, old: PdcTransaction, now: float) -> None:
        self._save_needed = True
        if (old.phase == PHASE_IDLE) != (room.tx.phase == PHASE_IDLE):
            room.managed_changed_at = now
            room.ctx = EMPTY_CONTEXT
            self._mirror_managed(room)
        notification = f"{room.cfg.room_key}_pdc_return_unconfirmed"
        if room.tx.phase == PHASE_RETURN_UNCONFIRMED and old.phase != PHASE_RETURN_UNCONFIRMED:
            self._ports.notify(
                notification,
                "Pompa di calore: spegnimento non confermato",
                f"{room.cfg.room_key}: lo split non conferma OFF. Il radiatore resta sospeso finche' lo spegnimento non e' confermato. Verificare il dispositivo e SmartThings.",
            )
        if room.tx.phase == PHASE_IDLE and old.phase != PHASE_IDLE:
            self._ports.notify(notification, None, None)
            room.release_held = False
            if room.resume_pending:
                self._finish_resume(room)

    def _finish_resume(self, room: _Room) -> None:
        room.resume_pending = False
        room.override = OverrideState()
        self._save_needed = True
        self._mirror_override(room)

    async def _drive(self, room: _Room, collection: PdcCollection, now: float) -> bool:
        """Advance a transaction in flight with a fresh observation (after every wait)."""
        tx = room.tx
        obs = build_observation(tx.token, collection.snapshot, now, room.cfg.policy, phase=tx.phase, override_off=not room.override.active)
        if tx.phase in START_PHASES and obs.override_off is True and unsafe_during_start(collection.snapshot, now, room.cfg.policy, phase=tx.phase):
            step = request_return(tx, self._reservation, now, obs, reason=FAILURE_CONDITIONS_CHANGED, config=self._config.transaction)
        else:
            step = observe(tx, self._reservation, obs, now, self._config.transaction)
        return await self._apply(room, step, now)

    async def _return_step(self, room: _Room, collection: PdcCollection, now: float, reason: str, *, exempt: bool) -> bool:
        tx = room.tx
        if tx.phase == PHASE_IDLE:
            return False
        obs = build_observation(tx.token, collection.snapshot, now, room.cfg.policy, phase=tx.phase, override_off=not room.override.active)
        if tx.phase in RETURN_WAIT_PHASES:
            if exempt and not tx.override_exempt:
                return await self._apply(room, request_return(tx, self._reservation, now, obs, reason=reason, override_exempt=True, config=self._config.transaction), now)
            return await self._drive(room, collection, now)
        step = request_return(tx, self._reservation, now, obs, reason=reason, override_exempt=exempt, config=self._config.transaction)
        return await self._apply(room, step, now)

    async def _running_step(self, room: _Room, collection: PdcCollection, now: float) -> bool:
        snapshot = collection.snapshot
        if radiator_released_while_running(snapshot, room.tx):
            return await self._return_step(room, collection, now, FAILURE_RADIATOR_RELEASED, exempt=False)
        decision = evaluate_pdc(snapshot, room.ctx, now, room.cfg.policy)
        room.decision = decision
        if decision.action == PDC_ACTION_STOP:
            return await self._return_step(room, collection, now, decision.stop_reason or FAILURE_STOP, exempt=False)
        if decision.action == PDC_ACTION_SET_SETPOINT and decision.setpoint is not None:
            # once per pass: the next event or tick reads it again (a setpoint the device rounds differently never loops)
            await self._command(room, "climate", "set_temperature", room.cfg.pdc_entity_id, {"temperature": decision.setpoint})
        return False

    async def _idle_step(self, room: _Room, collection: PdcCollection, now: float) -> bool:
        decision = evaluate_pdc(collection.snapshot, room.ctx, now, room.cfg.policy)
        room.ctx = decision.context
        room.decision = decision
        if decision.action != PDC_ACTION_START:
            return False
        return await self._start(room, collection, now)

    async def _start(self, room: _Room, collection: PdcCollection, now: float) -> bool:
        snapshot = collection.snapshot
        if self._ports.inhibition_started(room.cfg.radiator_entity_id) is not True:
            room.last_denied = "inhibition_not_started"
            return False
        before_tx, before_res = room.tx, self._reservation
        step = request_start(
            room.tx,
            self._reservation,
            now,
            override_off=not room.override.active,
            other_pdc_busy=not other_pdc_free(snapshot),
            radiator_inhibited_before=snapshot.radiator_inhibited,
            config=self._config.transaction,
        )
        if not step.accepted:
            room.last_denied = step.reason
            return False
        room.last_denied = None
        return await self._begin_session(room, step, before_tx, before_res, now)

    async def _begin_session(self, room: _Room, step: PdcStep, before_tx: PdcTransaction, before_res: PdcReservation, now: float) -> bool:
        """The ownership claim must be durable before the radiator is touched: no durable claim, no session."""
        room.tx, self._reservation = step.transaction, step.reservation
        if not await self._save():
            room.tx, self._reservation = before_tx, before_res
            room.last_error = self._last_save_error
            return False
        self._on_tx_changed(room, before_tx, now)
        await self._execute(room, step.commands)
        return True

    # ----------------------------------------------------------- command execution

    async def _command(self, room: _Room, domain: str, service: str, entity_id: str, data: dict[str, Any] | None = None) -> bool:
        payload = {"entity_id": entity_id, **(data or {})}
        try:
            await self._ports.call_service(domain, service, payload)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - the baseline continues on error and re-reads the state
            room.last_error = f"command_failed:{domain}.{service}:{type(err).__name__}"
            _LOGGER.warning("PDC runtime: %s.%s on %s failed: %s", domain, service, entity_id, err)
            return False
        return True

    async def _execute(self, room: _Room, commands: tuple[PdcCommand, ...]) -> None:
        """Run the machine commands. Each one is dropped if its token is no longer current or the interlock was lost."""
        epoch = self._epoch
        for cmd in commands:
            if epoch != self._epoch or cmd.token != room.tx.token or cmd.room_key != room.cfg.room_key:
                continue
            room.blocked_by = self._blocked_by(room)
            if room.blocked_by:
                return  # ownership lost between the decision and the command: freeze, no compensation
            now = self._ports.now()
            if cmd.kind == CMD_INHIBIT_RADIATOR:
                await self._do_inhibit(room, cmd)
            elif cmd.kind == CMD_SET_TARGET:
                setpoint = split_setpoint(self._collect(room, now).snapshot.target, room.cfg.policy)
                if setpoint is not None:
                    await self._command(room, "climate", "set_temperature", room.cfg.pdc_entity_id, {"temperature": setpoint})
            elif cmd.kind == CMD_PDC_HEAT:
                await self._do_heat(room, now)
            elif cmd.kind == CMD_PDC_OFF:
                await self._command(room, "climate", "set_hvac_mode", room.cfg.pdc_entity_id, {"hvac_mode": "off"})
                if room.tx.override_exempt and room.tx.failure in PROTECTIVE_FAILURES:
                    # protections and the suspension do not wait for the PDC VTherm: the split goes off at once (baseline protections)
                    split = lookup(self._ports.states(), room.cfg.split_entity_id).state
                    if split is not None and split != STATE_OFF:
                        await self._command(room, "climate", "set_hvac_mode", room.cfg.split_entity_id, {"hvac_mode": "off"})
            elif cmd.kind == CMD_SPLIT_OFF_DIRECT:
                await self._command(room, "climate", "set_hvac_mode", room.cfg.split_entity_id, {"hvac_mode": "off"})
            elif cmd.kind == CMD_RELEASE_INHIBITION:
                await self._do_release(room, cmd, now)
            elif cmd.kind == CMD_RELEASE_RESERVATION:
                continue  # already applied to the shared reservation by the machine step

    async def _do_inhibit(self, room: _Room, cmd: PdcCommand) -> None:
        """Place the inhibition, then settle the ownership claim from what the VTherm reports (it may not have been us)."""
        entity_id = room.cfg.radiator_entity_id
        inhibited_now, marker_before = read_inhibition(lookup(self._ports.states(), entity_id).attributes)
        if inhibited_now is not True:  # somebody inhibited it since the request: the call would be a no-op that looks like ours
            try:
                await self._ports.set_inhibition(entity_id, True)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                room.last_error = f"inhibition_failed:{type(err).__name__}"
                _LOGGER.warning("PDC runtime: set_inhibition(%s) failed: %s", entity_id, err)
        if cmd.token != room.tx.token:
            return
        inhibited_after, marker_after = read_inhibition(lookup(self._ports.states(), entity_id).attributes)
        transitioned: bool | None = (marker_after != marker_before) if inhibited_after is True and marker_after is not None else None
        if inhibited_now is True:
            transitioned = False
        now = self._ports.now()
        collection = self._collect(room, now)
        obs = build_observation(room.tx.token, collection.snapshot, now, room.cfg.policy, phase=room.tx.phase, override_off=not room.override.active)
        step = record_inhibition_result(
            room.tx, self._reservation, now, token=cmd.token, inhibited_after=inhibited_after, transitioned=transitioned, obs=obs, config=self._config.transaction
        )
        if step.transaction != room.tx or step.commands:
            await self._apply(room, step, now)

    async def _do_heat(self, room: _Room, now: float) -> None:
        """Last re-validation before HEAT: radiator stop, target and conditions confirmed on a fresh read."""
        collection = self._collect(room, now)
        tx = room.tx
        obs = build_observation(tx.token, collection.snapshot, now, room.cfg.policy, phase="waiting_target", override_off=not room.override.active)
        safe = (
            tx.phase == PHASE_WAITING_HEAT
            and tx.inhibition_owned
            and obs.override_off is True
            and obs.radiator_stopped is True
            and obs.target_confirmed is True
            and obs.conditions_ok is True
            and not self._suspension.active
        )
        if not safe:
            room.last_error = "heat_revalidation_failed"
            step = request_return(tx, self._reservation, now, obs, reason=FAILURE_CONDITIONS_CHANGED, override_exempt=self._suspension.active, config=self._config.transaction)
            await self._apply(room, step, now)
            return
        await self._command(room, "climate", "set_hvac_mode", room.cfg.pdc_entity_id, {"hvac_mode": "heat"})

    async def _do_release(self, room: _Room, cmd: PdcCommand, now: float) -> None:
        """Give the radiator back: only if owned, only against a fresh split and PDC ``off``, never while the suspension holds it."""
        tx = room.tx
        if tx.phase != PHASE_RELEASING or not tx.inhibition_owned:
            return
        if self._suspension.active and self._suspension.hold_radiator_inhibition:
            room.release_held = True
            await self._apply(room, confirm_release(tx, self._reservation, now, token=cmd.token, released=None, config=self._config.transaction), now)
            return
        room.release_held = False
        snapshot = self._collect(room, now).snapshot
        if snapshot.split_mode != STATE_OFF or snapshot.pdc_mode != STATE_OFF:
            # the PDC came back while we were about to release: the next observation returns it
            await self._apply(room, confirm_release(tx, self._reservation, now, token=cmd.token, released=None, config=self._config.transaction), now)
            return
        released = False
        try:
            await self._ports.unset_inhibition(room.cfg.radiator_entity_id)
            released = True
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - ownership is kept and the release retried
            room.last_error = f"release_failed:{type(err).__name__}"
            _LOGGER.warning("PDC runtime: unset_inhibition(%s) failed: %s", room.cfg.radiator_entity_id, err)
        now = self._ports.now()
        await self._apply(room, confirm_release(room.tx, self._reservation, now, token=cmd.token, released=released, config=self._config.transaction), now)

    # --------------------------------------------------------------- mirrors

    def _mirror(self, entity_id: str | None, on: bool) -> None:
        """Compatibility copy of a legacy helper (``input_boolean``). Never a precondition, never read back in ``control``."""
        if not entity_id or entity_id.split(".", 1)[0] not in MIRROR_DOMAINS:
            return
        if lookup(self._ports.states(), entity_id).state == (STATE_ON if on else STATE_OFF):
            return  # already in sync: nothing is written (a blocked or untouched room stays untouched)
        epoch = self._epoch
        domain = entity_id.split(".", 1)[0]

        async def _call() -> None:
            if epoch != self._epoch:
                return
            try:
                await self._ports.call_service(domain, "turn_on" if on else "turn_off", {"entity_id": entity_id})
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("PDC runtime: mirror of %s failed: %s", entity_id, err)

        self._ports.spawn(_call(), "pdc_runtime_mirror")

    def _mirror_managed(self, room: _Room) -> None:
        on = room.tx.phase != PHASE_IDLE
        if room.mirrored_managed != on:
            room.mirrored_managed = on
            self._mirror(room.cfg.managed_entity_id, on)

    def _mirror_override(self, room: _Room) -> None:
        on = room.override.active
        if room.mirrored_override != on:
            room.mirrored_override = on
            self._mirror(room.cfg.override_entity_id, on)

    # --------------------------------------------------------- manual override (M5)

    def on_call_service(self, domain: str, service: str, data: Mapping[str, Any] | None, user_id: str | None) -> None:
        """A climate service call was made (``call_service`` event). Registers expected effects; an authenticated user starts the override."""
        if domain != "climate" or self._config.mode != MODE_CONTROL or not self._started or self._stopped:
            return
        data = data or {}
        targets = data.get("entity_id", [])
        ids = [targets] if isinstance(targets, str) else (list(targets) if isinstance(targets, (list, tuple)) else [])
        now = self._ports.now()
        for room in self._rooms.values():
            if room.cfg.split_entity_id in ids and service in REGISTERED_SERVICES:
                room.register = register_command(room.register, service, data, now)
                self._save_needed = True
            if is_user_command(user_id, service, targets, (room.cfg.split_entity_id, room.cfg.pdc_entity_id)):
                if not self._suspension.active:  # during the suspension the command is undone, not adopted
                    room.override = start_override(room.override, now, SOURCE_USER_COMMAND, PROVENANCE_USER)
                    self._save_needed = True
                    self._mirror_override(room)
                self.request_evaluation(room.cfg.room_key)

    def on_state_changed(self, entity_id: str, old_state: Any, new_state: Any) -> None:
        """A watched entity changed. A split change is classified against the expected effects (after the baseline's 1 s)."""
        if not self._started or self._stopped:
            return
        if self._config.mode == MODE_CONTROL:
            for room in self._rooms.values():
                if entity_id == room.cfg.split_entity_id:
                    self._classify_later(room, old_state, new_state)
        self.request_evaluation()

    def _classify_later(self, room: _Room, old_state: Any, new_state: Any) -> None:
        epoch = self._epoch
        split = room.cfg.split_entity_id
        old = None if old_state is None else device_state(lookup({split: old_state}, split))
        new = None if new_state is None else device_state(lookup({split: new_state}, split))

        room.classify_pending += 1

        def _classify() -> None:
            room.classify_pending = max(0, room.classify_pending - 1)
            if epoch != self._epoch or self._suspension.active:
                self.request_evaluation(room.cfg.room_key)
                return
            now = self._ports.now()
            started = False
            if classify_device_change(room.register, old, new, now).manual:
                room.override = start_override(room.override, now, SOURCE_DEVICE_CHANGE, PROVENANCE_UNKNOWN)
                started = True
            elif device_found_running(
                None if old is None else old.state, None if new is None else new.state, override_off=not room.override.active, managed_off=room.tx.phase == PHASE_IDLE
            ):
                room.override = start_override(room.override, now, SOURCE_DEVICE_FOUND_RUNNING, PROVENANCE_UNKNOWN)
                started = True
            if started:
                self._save_needed = True
                self._mirror_override(room)
            self.request_evaluation(room.cfg.room_key)

        self._ports.call_later(CLASSIFY_DELAY_S, _classify)

    async def _override_step(self, room: _Room, collection: PdcCollection, now: float) -> bool:
        """Manual override: end of the override and protections first, then the session with the split."""
        snapshot = collection.snapshot
        if override_expired(room.override, now) or room.resume_pending:
            room.resume_pending = True
            return await self._resume_step(room, collection, now)
        if override_protection_stop(snapshot, now, room.cfg.policy):
            if room.tx.phase != PHASE_IDLE:
                return await self._return_step(room, collection, now, FAILURE_OVERRIDE_PROTECTION, exempt=True)
            if snapshot.split_mode in SPLIT_ACTIVE_MODES or snapshot.pdc_mode in SPLIT_ACTIVE_MODES:
                return await self._emergency_off(room, snapshot, now, PROTECTION_EMERGENCY_GAP_S)
            return False
        if room.tx.phase in START_PHASES:
            return await self._drive(room, collection, now)  # the override freezes a start in progress
        if snapshot.split_mode in SPLIT_ACTIVE_MODES:
            if room.tx.phase == PHASE_IDLE:
                return await self._adopt(room, collection, now)
            if room.tx.phase in RETURN_WAIT_PHASES:
                return await self._drive(room, collection, now)
            await self._sync_pdc(room, collection, now)
            return False
        if snapshot.split_mode == STATE_OFF and room.tx.phase != PHASE_IDLE:
            # the user switched the split off during the override: baseline "se split OFF: PDC OFF e in_gestione OFF"
            return await self._return_step(room, collection, now, FAILURE_OVERRIDE_ENDED, exempt=True)
        return False

    async def _adopt(self, room: _Room, collection: PdcCollection, now: float) -> bool:
        before_tx, before_res = room.tx, self._reservation
        step = adopt_manual(room.tx, self._reservation, now, radiator_inhibited_before=collection.snapshot.radiator_inhibited, config=self._config.transaction)
        if not step.accepted:
            room.last_denied = step.reason
            return False
        return await self._begin_session(room, step, before_tx, before_res, now)

    async def _sync_pdc(self, room: _Room, collection: PdcCollection, now: float) -> None:
        """Copy the manual split state to the PDC VTherm (baseline ``override_sincronizza``), skipping our own expected effects."""
        snapshot, device = collection.snapshot, collection.split_device
        if device is None:
            return

        wanted: list[tuple[str, dict[str, Any]]] = []
        temperature = device.temperature
        if temperature is not None and 16.0 <= temperature <= 30.0 and snapshot.pdc_setpoint != temperature:
            wanted.append(("set_temperature", {"temperature": temperature}))
        if device.state in SPLIT_ACTIVE_MODES and snapshot.pdc_mode != device.state:
            wanted.append(("set_hvac_mode", {"hvac_mode": device.state}))
        signature = (temperature, device.state)
        if wanted and room.last_sync is not None and room.last_sync[0] == signature and now - room.last_sync[1] < SYNC_REPEAT_S:
            return  # the same copy was sent a moment ago and the VTherm did not take it: do not hammer it
        for service, data in wanted:
            await self._command(room, "climate", service, room.cfg.pdc_entity_id, data)
        if wanted:
            room.last_sync = (signature, now)

    async def _emergency_off(self, room: _Room, snapshot: PdcSnapshot, now: float, min_gap_s: float) -> bool:
        """PDC VTherm OFF, then the split OFF at once, without a session (protections and suspension). Rate limited."""
        if room.emergency_last is not None and now - room.emergency_last < min_gap_s:
            return False
        room.emergency_last = now
        if snapshot.pdc_mode != STATE_OFF:
            await self._command(room, "climate", "set_hvac_mode", room.cfg.pdc_entity_id, {"hvac_mode": "off"})
        if snapshot.split_mode != STATE_OFF:
            await self._command(room, "climate", "set_hvac_mode", room.cfg.split_entity_id, {"hvac_mode": "off"})
        return False

    async def _resume_step(self, room: _Room, collection: PdcCollection, now: float) -> bool:
        """End of the override (expiry or explicit resume): back to automatic through the safe return; the override ends when OFF is confirmed."""
        snapshot = collection.snapshot
        if room.tx.phase != PHASE_IDLE:
            return await self._return_step(room, collection, now, FAILURE_OVERRIDE_ENDED, exempt=True)
        if snapshot.split_mode == STATE_OFF and snapshot.pdc_mode == STATE_OFF:
            self._finish_resume(room)
            return True
        return await self._emergency_off(room, snapshot, now, PROTECTION_EMERGENCY_GAP_S)

    async def async_resume_automatic(self, room_key: str) -> bool:
        """The user asks to leave the manual override now (baseline ``riprendi_automatico``). ``False`` when there is nothing to resume."""
        room = self._rooms.get(room_key)
        if room is None or not self.is_controlling or not room.override.active:
            return False
        room.resume_pending = True
        self._save_needed = True
        self.request_evaluation(room_key)
        return True

    # ------------------------------------------------------- general suspension

    async def async_suspend(self, reason: str = "manual", *, hold_radiator_inhibition: bool = True) -> PdcSuspendResult:
        """Stop the PDC rooms and block new starts until :meth:`async_resume`.

        Only in ``control`` and while every room is owned (no interlock): otherwise
        nothing is sent and nothing is persisted. The block is persisted first; the
        shutdown is then confirmed room by room (a service call proves nothing,
        unknown/unavailable is not OFF). Active overrides are cancelled. With
        ``hold_radiator_inhibition`` (default) the radiators stay inhibited until the
        resume, so that giving them back cannot reopen a valve during the shutdown.
        """
        if not self.is_controlling:
            return PdcSuspendResult(False, SUSPENSION_NONE, REFUSED_NOT_CONTROL)
        if any(self._blocked_by(room) for room in self._rooms.values()):
            return PdcSuspendResult(False, SUSPENSION_NONE, REFUSED_BLOCKED)
        if self._suspension.active:
            return PdcSuspendResult(True, self.suspension_state(), REFUSED_ALREADY, self._room_states())
        self._suspension = PdcSuspension(True, self._ports.now(), reason, hold_radiator_inhibition)
        for room in self._rooms.values():
            room.override = OverrideState()
            room.resume_pending = False
            room.suspend_state = SUSPENSION_SHUTTING_DOWN
            room.suspend_attempts = 0
            room.emergency_last = None
            self._mirror_override(room)
        await self._save()  # the block comes first; a failed write never prevents the shutdown
        self.request_evaluation()
        return PdcSuspendResult(True, SUSPENSION_SHUTTING_DOWN, None, self._room_states())

    async def async_resume(self) -> PdcSuspendResult:
        """Lift the suspension. Nothing is switched back on: the normal policy re-evaluates from the current inputs."""
        if not self.is_controlling:
            return PdcSuspendResult(False, SUSPENSION_NONE, REFUSED_NOT_CONTROL)
        if not self._suspension.active:
            return PdcSuspendResult(False, SUSPENSION_NONE, REFUSED_NOT_SUSPENDED)
        now = self._ports.now()
        self._suspension = PdcSuspension()
        for room in self._rooms.values():
            room.suspend_state = SUSPENSION_NONE
            room.release_held = False
            room.ctx = EMPTY_CONTEXT
            if room.tx.phase == PHASE_RELEASING:
                room.tx = replace(room.tx, deadline=now)  # the held release is attempted at the next pass
        await self._save()
        self.request_evaluation()
        return PdcSuspendResult(True, SUSPENSION_NONE, None, self._room_states())

    def _room_states(self) -> tuple[tuple[str, str], ...]:
        return tuple((key, room.suspend_state) for key, room in self._rooms.items())

    async def _suspend_step(self, room: _Room, collection: PdcCollection, now: float) -> bool:
        snapshot = collection.snapshot
        progressed = False
        if room.tx.phase != PHASE_IDLE:
            progressed = await self._return_step(room, collection, now, FAILURE_SUSPENDED, exempt=True)
        elif snapshot.pdc_mode in SPLIT_ACTIVE_MODES or snapshot.split_mode in SPLIT_ACTIVE_MODES:
            if room.suspend_attempts < SUSPEND_MAX_ATTEMPTS:
                before = room.emergency_last
                await self._emergency_off(room, snapshot, now, SUSPEND_REPEAT_S)
                if room.emergency_last != before:
                    room.suspend_attempts += 1
        self._update_suspend_state(room, self._collect(room, now).snapshot if progressed else snapshot, now)
        return progressed

    def _update_suspend_state(self, room: _Room, snapshot: PdcSnapshot, now: float) -> None:
        settled = room.tx.phase == PHASE_IDLE or (room.tx.phase == PHASE_RELEASING and room.release_held)
        if snapshot.split_mode == STATE_OFF and snapshot.pdc_mode == STATE_OFF and settled:
            room.suspend_state, room.suspend_attempts = SUSPENSION_CONFIRMED, 0
            return
        unknown = any(mode in (None, STATE_UNKNOWN, STATE_UNAVAILABLE) for mode in (snapshot.split_mode, snapshot.pdc_mode))
        waited = self._suspension.since is not None and now - self._suspension.since >= SUSPEND_UNREACHABLE_S
        if (unknown and waited) or room.suspend_attempts >= SUSPEND_MAX_ATTEMPTS:
            room.suspend_state = SUSPENSION_UNREACHABLE
        else:
            room.suspend_state = SUSPENSION_SHUTTING_DOWN

    # ------------------------------------------------------------- diagnostics

    def _finish_pass(self, room: _Room, now: float) -> None:
        decision = room.decision
        room.generation += 1
        wake: list[float | None] = [next_wakeup(room.tx, now, self._config.transaction)]
        if room.tx.phase in (PHASE_IDLE, PHASE_RUNNING) and decision is not None:
            wake.append(decision.deadline)
        if self._suspension.active:
            wake.append(now + SUSPEND_REPEAT_S)
        if room.override.active:
            wake.append(room.override.until)
        if not room.recovered:
            wake.append(now + RECOVERY_RECHECK_S)
        times = [w for w in wake if w is not None and w > now]
        self._schedule_wake(room, min(times) if times else None, now)
        in_flight = room.tx.phase not in (PHASE_IDLE, PHASE_RUNNING)
        if self._suspension.active:
            state = STATE_SUSPENDED
        elif in_flight:
            state = room.tx.phase
        else:
            state = decision.reason if decision is not None else STATE_STARTING
        # a decision taken before the transaction began says nothing about a hand-over in flight
        self._publish(room, state, None if in_flight else decision, owner=OWNER_CONTROLLER, now=now)

    def _publish(self, room: _Room, state: str, decision: PdcDecision | None, *, owner: str | None, now: float) -> None:
        error = room.last_error or self._last_save_error or (self._load_errors[0] if self._load_errors else None) or room.last_denied
        attributes = build_pdc_diagnostics(
            room_key=room.cfg.room_key,
            mode=self._config.mode,
            owner=owner,
            decision=decision,
            snapshot=room.snapshot,
            tx=room.tx,
            reservation=self._reservation,
            override=room.override,
            policy=room.cfg.policy,
            evaluated_at=now,
            generation=room.generation,
            last_error=error,
            suspension=room.suspend_state if self._suspension.active else None,
            blocked_by=room.blocked_by,
            split_action=room.split_action,
            release_held=room.release_held,
            reason=self._reason_of(room),
        )
        phase = room.tx.phase
        if self._config.mode == MODE_CONTROL and (phase in START_PHASES or phase in RETURN_WAIT_PHASES):
            # what the machine is doing now (the policy proposal belongs to the decision taken before the hand-over began)
            attributes["proposed_action"] = "heat" if phase in START_PHASES else "off"
        room.last_state, room.last_attributes = state, attributes
        self._ports.publish(room.cfg.room_key, state, attributes)

    def _reason_of(self, room: _Room) -> str | None:
        if self._suspension.active:
            return STATE_SUSPENDED
        if room.tx.phase not in (PHASE_IDLE, PHASE_RUNNING) and self._config.mode == MODE_CONTROL:
            return room.tx.phase
        return None

    def _publish_empty(self, room: _Room, state: str, error: str, now: float) -> None:
        owner = OWNER_OBSERVER if self._config.mode == MODE_OBSERVE else (OWNER_CONTROLLER if self._config.mode == MODE_CONTROL else None)
        attributes = empty_pdc_diagnostics(
            room.cfg.room_key, self._config.mode, owner=owner if state == STATE_ERROR else None, evaluated_at=now, last_error=error, generation=room.generation
        )
        room.last_state, room.last_attributes = state, attributes
        self._ports.publish(room.cfg.room_key, state, attributes)

    def _publish_blocked(self, room: _Room, now: float) -> None:
        attributes = empty_pdc_diagnostics(
            room.cfg.room_key, self._config.mode, owner=None, evaluated_at=now, last_error="external_owner_active:" + ",".join(room.blocked_by), generation=room.generation
        )
        attributes["blocked_by"] = list(room.blocked_by)
        attributes["transaction"] = {"id": room.tx.token or None, "phase": room.tx.phase, "started_at": room.tx.started_at, "deadline": room.tx.deadline}
        room.last_state, room.last_attributes = STATE_BLOCKED, attributes
        self._ports.publish(room.cfg.room_key, STATE_BLOCKED, attributes)
