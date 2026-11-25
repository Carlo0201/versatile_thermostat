"""Boiler orchestration manager of the central configuration (M2-A observe, M2-B control).

One manager per central configuration entry. It reads a single coherent
snapshot of the configured entities, runs the pure collector and the M1 policy
with one clock value, keeps its own context in a versioned Home Assistant
``Store`` and publishes the diagnostic sensor.

* ``observe``: it never calls a service, nothing is commanded.
* ``control``: it sends ``climate.set_hvac_mode`` (``off``/``heat``) to the
  master, and only that. The decisions (send, confirm, retry, give up, cancel)
  belong to the pure module ``heating_boiler_command``; this manager executes
  the services and feeds the results back. The master is commanded only while
  the controller is the owner (``evaluate_ownership``: external controllers
  ``off``, master mode known, mode ``control``, current generation) and after
  the post setup/reload settling. The manager never switches an automation,
  helper or timer, and never writes the legacy timers or cycle helper.

Evaluation pipeline, kept in separate stages so that a failure of one stage
never blocks the following ones:

1. ``_compute``: collector + M1 with one clock, producing an immutable
   ``EvaluationResult``;
2. control plane (control only): settling, ownership, ``command.step``;
3. publication (only when the significant content changed, or at the
   heartbeat) and re-subscription;
4. command execution as a separate task (``_async_send``) that recomputes
   inputs, M1 and ownership with a fresh clock immediately before every send,
   retries included.

Scheduling: state change listeners on the relevant entities (attribute changes
included), a recovery evaluation every 15 s, a one-shot timer at the M1
deadline and one at the next command deadline (confirmation timeout or retry),
both only when in the future. Requests are coalesced into one evaluation per
loop iteration; nothing reschedules itself with a zero delay. An epoch token
invalidates every callback that survives ``async_stop``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from homeassistant.const import (
    ATTR_DOMAIN,
    ATTR_ENTITY_ID,
    ATTR_NAME,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
    EVENT_LOGBOOK_ENTRY,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from vtherm_api.log_collector import get_vtherm_logger

from .const import HO_STORAGE_KEY_PREFIX, HO_STORAGE_VERSION
from .heating_boiler_collector import (
    CYCLE_ORIGIN_LEGACY_SEED,
    CYCLE_ORIGIN_LOCAL_RESTORE,
    CYCLE_ORIGIN_NEW_OBSERVATION,
    MODE_CONTROL,
    MODE_DISABLED,
    MODE_OBSERVE,
    OWNER_OBSERVER,
    STATE_DISABLED,
    STATE_ERROR,
    STATE_STARTING,
    BoilerCollection,
    OrchestrationConfig,
    OrchestrationConfigError,
    build_boiler_diagnostics,
    collect_boiler_inputs,
    cycle_to_dict,
    empty_diagnostics,
    parse_orchestration_config,
    publication_signature,
)
from .heating_boiler_command import (
    CONTROL_INACTIVE,
    CONTROL_PENDING,
    CONTROL_STARTING,
    ERROR_SERVICE_TIMEOUT,
    HEAT_PERSIST_WAIT_S,
    MASTER_KNOWN_MODES,
    MAX_SENDS,
    OWNER_CONTROLLER,
    SERVICE_TIMEOUT_S,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_SENDING,
    STATUS_SUPERSEDED,
    STORE_COMMAND_KEY,
    CommandEvent,
    CommandInputs,
    CommandRecord,
    CommandStep,
    EVENT_SUPERSEDED,
    COMMAND_HEAT,
    ExternalOwner,
    Ownership,
    SettlingState,
    command_diagnostics,
    evaluate_ownership,
    migrate_store_data,
    on_service_completed,
    on_service_failed,
    record_from_dict,
    record_to_dict,
    restart_record,
    start_send,
    step as command_step,
    update_settling,
    COMMAND_OFF,
)
from .heating_boiler_policy import (
    BoilerCycle,
    BoilerDecision,
    BoilerSnapshot,
    QualifiedBoilerRoom,
    evaluate_boiler,
    parse_cycle_context,
)

_LOGGER = get_vtherm_logger(__name__)

EVALUATION_INTERVAL = timedelta(seconds=15)
STORE_SAVE_DELAY_S = 30
# A command record transition is saved soon, but never in the way of a command.
STORE_COMMAND_DELAY_S = 2
# A published evaluation is refreshed at least this often (Oikos accepts -2..+45 s,
# the poll runs every 15 s: a refresh every second tick keeps the age below 31 s).
PUBLISH_HEARTBEAT_S = 25.0
STORE_FLUSH_TIMEOUT_S = 5.0
# An error of the start (store, legacy seed) is shown this long, then released: it must
# not mask the errors of the following evaluations for the whole life of the manager.
STARTUP_ERROR_TTL_S = 60.0

LOGBOOK_ENTRY_MESSAGE = "message"  # same key as homeassistant.components.logbook (not imported: optional component)
LOGBOOK_PREFIX = "OIKOS_HEATING_V1|"
LOGBOOK_NAME = "Caldaia"

Publisher = Callable[[str, dict[str, Any]], None]


def _utc_clock() -> float:
    return dt_util.utcnow().timestamp()


def build_policy_snapshot(collection: BoilerCollection) -> BoilerSnapshot:
    """Translate the collected ``raw`` into the M1 snapshot (same field names)."""
    raw = collection.raw
    return BoilerSnapshot(
        ok=bool(raw["ok"]),
        master=raw["master"],
        count=int(raw["count"]),
        rooms=tuple(QualifiedBoilerRoom(slug=room["slug"], target=room["target"], gap=room["gap"], pilot_ok=room["pilot_ok"]) for room in raw["rooms"]),
        manual=raw["manual"],
        pilot=raw["pilot"],
        pilot_ready=raw["pilot_ready"],
        consent=raw["consent"],
    )


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """Immutable outcome of one computation: publication and command only consume it."""

    now: float
    collection: BoilerCollection
    decision: BoilerDecision

    @property
    def master_mode(self) -> str | None:
        """Master mode of the snapshot."""
        mode = self.collection.raw["master"]
        return mode if isinstance(mode, str) else None

    @property
    def evaluation_valid(self) -> bool:
        """Complete evaluation: valid count, supported unit and a known master mode."""
        return bool(self.collection.count_raw_valid and self.collection.temperature_unit_supported and self.master_mode in MASTER_KNOWN_MODES)


class _OrchestrationStore(Store):
    """``Store`` of the manager: version 2 adds the command record (migration from M2-A)."""

    async def _async_migrate_func(self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]) -> dict[str, Any]:
        return migrate_store_data(old_major_version, old_minor_version, old_data)


class _OneShotTimer:
    """A single cancellable timer at an absolute instant.

    The callback checks that it belongs to the timer currently scheduled, so a
    callback already queued when the timer is replaced can not clear the new one.
    """

    def __init__(self, hass: HomeAssistant, on_fire: Callable[[], None]) -> None:
        self._hass = hass
        self._on_fire = on_fire
        self._unsub: CALLBACK_TYPE | None = None
        self._token: object | None = None
        self.at: float | None = None

    @property
    def is_scheduled(self) -> bool:
        """True while a timer is armed."""
        return self._unsub is not None

    @callback
    def cancel(self) -> None:
        """Cancel the timer."""
        if self._unsub is not None:
            self._unsub()
        self._unsub = None
        self._token = None
        self.at = None

    @callback
    def schedule(self, at: float | None, now: float) -> None:
        """Arm the timer at ``at`` when it is strictly in the future; passed instants rely on polling."""
        if at is None or at <= now:
            self.cancel()
            return
        if self.at == at and self._unsub is not None:
            return
        self.cancel()
        token = object()

        @callback
        def _fire(_now: datetime) -> None:
            if self._token is not token:
                return
            self._unsub = None
            self._token = None
            self.at = None
            self._on_fire()

        self._token = token
        self.at = at
        self._unsub = async_track_point_in_utc_time(self._hass, _fire, dt_util.utc_from_timestamp(at))


class FeatureHeatingOrchestrationManager:
    """Observer or controller of the boiler orchestration for one central configuration."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        section: Any,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._clock = clock or _utc_clock
        self._config: OrchestrationConfig | None
        self._config_error: str | None
        try:
            self._config = parse_orchestration_config(section)
            self._config_error = None
        except OrchestrationConfigError as err:
            self._config = None
            self._config_error = f"config_{err.code}" + (f":{err.key}" if err.key else "")

        self._publishers: list[Publisher] = []
        self._own_entity_ids: set[str] = set()
        self._running = False
        self._stopped = False
        self._epoch = 0
        self._generation = 0
        self._unsubs: list[CALLBACK_TYPE] = []
        self._once_unsubs: dict[str, CALLBACK_TYPE] = {}
        self._state_unsub: CALLBACK_TYPE | None = None
        self._deadline_timer = _OneShotTimer(hass, self._on_timer)
        self._command_timer = _OneShotTimer(hass, self._on_timer)
        self._pending: Any = None
        self._watched: tuple[str, ...] = ()
        self._inputs_ready = False

        self._store: Store | None = None
        self._cycle: BoilerCycle | None = None
        self._cycle_origin: str | None = None
        self._cycle_loaded = False
        self._legacy_seeded = False
        self._last_saved_key: Any = None
        self._pending_key: Any = None
        self._dirty = False
        self._save_tasks: set[asyncio.Task] = set()
        self._startup_error: str | None = None
        self._startup_error_at: float | None = None
        self._persist_error: str | None = None
        self._last_reason: str | None = None

        # Publication (significant change or heartbeat).
        self._last_signature: Any = None
        self._last_publish_at: float | None = None

        # Control plane (M2-B).
        self._record: CommandRecord | None = None
        self._startup_events: tuple[CommandEvent, ...] = ()
        self._settling = SettlingState()
        self._complete_evaluation_seen = False
        self._ownership = Ownership(False, None, ())
        self._control_status = CONTROL_INACTIVE
        self._send_lock = asyncio.Lock()
        self._send_task: asyncio.Task | None = None
        self._send_mode: str | None = None
        self._send_seq = 0
        self._budget: tuple = ()  # per-mode send budget (not persisted)
        self._abandon_streak = 0  # consecutive abandoned sends: at most one immediate re-evaluation
        self._inflight_id: int | None = None  # send id of the request whose service call is running

        if self._config_error is not None:
            self._state = STATE_ERROR
            self._attributes = empty_diagnostics(MODE_DISABLED, last_error=self._config_error)
        elif self.mode == MODE_DISABLED:
            self._state = STATE_DISABLED
            self._attributes = empty_diagnostics(MODE_DISABLED)
        else:
            self._state = STATE_STARTING
            self._attributes = empty_diagnostics(self.mode, owner=OWNER_OBSERVER, control=self._control_fields(CONTROL_STARTING if self.mode == MODE_CONTROL else None))

    # ------------------------------------------------------------------ status

    @property
    def mode(self) -> str:
        """Effective mode: ``disabled``, ``observe`` or ``control`` (``disabled`` for an invalid config)."""
        if self._config is None:
            return MODE_DISABLED
        return self._config.mode

    @property
    def entry_id(self) -> str:
        """Central configuration entry id."""
        return self._entry_id

    @property
    def config(self) -> OrchestrationConfig | None:
        """Validated configuration (``None`` when invalid)."""
        return self._config

    @property
    def is_running(self) -> bool:
        """True while listeners and timers are owned."""
        return self._running

    @property
    def state(self) -> str:
        """Last published state."""
        return self._state

    @property
    def attributes(self) -> dict[str, Any]:
        """Last published attributes."""
        return self._attributes

    @property
    def generation(self) -> int:
        """Number of published evaluations."""
        return self._generation

    @property
    def cycle(self) -> BoilerCycle | None:
        """Cycle context of the manager (never shared with the legacy helpers)."""
        return self._cycle

    @property
    def cycle_origin(self) -> str | None:
        """``legacy_seed`` / ``local_restore`` / ``new_observation``."""
        return self._cycle_origin

    @property
    def watched_entity_ids(self) -> tuple[str, ...]:
        """Entities currently listened to."""
        return self._watched

    @property
    def store_key(self) -> str:
        """Storage key of the cycle context."""
        return f"{HO_STORAGE_KEY_PREFIX}.{self._entry_id}"

    @property
    def command_record(self) -> CommandRecord | None:
        """The last command request (``None`` when there is none)."""
        return self._record

    @property
    def control_status(self) -> str:
        """``control_status`` of the last evaluation."""
        return self._control_status

    @property
    def is_owner(self) -> bool:
        """True when the interlock of the last evaluation was satisfied."""
        return self._ownership.is_owner

    @property
    def is_settled(self) -> bool:
        """True after the post setup/reload settling."""
        return self._settling.settled

    # --------------------------------------------------------------- publishing

    @callback
    def attach(self, publisher: Publisher, entity_id: str | None = None) -> CALLBACK_TYPE:
        """Register a publisher (the diagnostic sensor) and push the current state."""
        self._publishers.append(publisher)
        if entity_id:
            self._own_entity_ids.add(entity_id)
        publisher(self._state, self._attributes)

        @callback
        def _detach() -> None:
            if publisher in self._publishers:
                self._publishers.remove(publisher)
            if entity_id:
                self._own_entity_ids.discard(entity_id)

        return _detach

    @callback
    def _publish(self, state: str, attributes: dict[str, Any]) -> None:
        """Publish to every sensor; a failing publisher never blocks the others."""
        self._state = state
        self._attributes = attributes
        for publisher in list(self._publishers):
            try:
                publisher(state, attributes)
            except Exception as err:  # noqa: BLE001 - a publisher must never stop the controller
                _LOGGER.error("%s - a publisher failed: %s", self, err)

    @callback
    def _publish_special(self, state: str, attributes: dict[str, Any]) -> None:
        """Publish ``starting``/``error``: the next evaluation is always published."""
        self._last_signature = None
        self._publish(state, attributes)

    @callback
    def _publish_evaluation(self, state: str, attributes: dict[str, Any], now: float) -> bool:
        """Publish an evaluation when its significant content changed or at the heartbeat.

        Oikos accepts a diagnostic whose ``evaluated_at`` is at most 45 s old, so
        an unchanged decision is refreshed every ``PUBLISH_HEARTBEAT_S``; between
        two heartbeats only a significant change is published. This keeps the
        number of state writes (and recorder rows) low although the climates
        report sample updates continuously. ``generation`` counts publications.
        """
        signature = publication_signature(state, attributes)
        due = self._last_publish_at is None or not 0 <= now - self._last_publish_at < PUBLISH_HEARTBEAT_S
        if signature == self._last_signature and not due:
            return False
        self._generation += 1
        attributes = {**attributes, "generation": self._generation}
        self._last_signature = signature
        self._last_publish_at = now
        self._publish(state, attributes)
        return True

    # ---------------------------------------------------------------- lifecycle

    async def async_start(self) -> None:
        """Start. Disabled or invalid configurations own nothing."""
        if self._running or self._stopped:
            return
        if self._config is None or self.mode not in (MODE_OBSERVE, MODE_CONTROL):
            self._publish(self._state, self._attributes)
            return
        self._store = _OrchestrationStore(self._hass, HO_STORAGE_VERSION, self.store_key)
        await self._async_load_store()
        if self._stopped:
            return
        self._running = True
        epoch = self._epoch

        @callback
        def _on_interval(_now: datetime) -> None:
            if epoch == self._epoch:
                self.async_request_evaluation()

        @callback
        def _on_started(_event: Event) -> None:
            self._once_unsubs.pop(EVENT_HOMEASSISTANT_STARTED, None)
            if epoch == self._epoch:
                self.async_request_evaluation()

        async def _on_stop(_event: Event) -> None:
            self._once_unsubs.pop(EVENT_HOMEASSISTANT_STOP, None)
            if epoch == self._epoch:
                await self.async_stop()

        self._unsubs.append(async_track_time_interval(self._hass, _on_interval, EVALUATION_INTERVAL))
        if self._hass.state is not CoreState.running:
            self._once_unsubs[EVENT_HOMEASSISTANT_STARTED] = self._hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)
        self._once_unsubs[EVENT_HOMEASSISTANT_STOP] = self._hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)
        self._resubscribe(self._config.configured_entity_ids())
        for event in self._startup_events:
            self._log_event(event)
        self._startup_events = ()
        self.async_request_evaluation()

    async def async_stop(self) -> None:
        """Cancel every owned task, listener and timer; flush the stored context.

        Pending confirmations, retries and the command in flight are cancelled and
        late callbacks are ignored through the epoch. No compensation command is
        sent; the unconfirmed request is read again from the master at the next start.
        """
        if self._stopped:
            return
        self._stopped = True
        self._running = False
        self._epoch += 1
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        # One-shot listeners remove themselves from this dict when they fire.
        for unsub in self._once_unsubs.values():
            unsub()
        self._once_unsubs.clear()
        if self._state_unsub is not None:
            self._state_unsub()
            self._state_unsub = None
        self._deadline_timer.cancel()
        self._command_timer.cancel()
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
        task = self._send_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.wait({task}, timeout=STORE_FLUSH_TIMEOUT_S)
        self._send_task = None
        self._send_mode = None
        if self._save_tasks:
            await asyncio.wait(set(self._save_tasks), timeout=STORE_FLUSH_TIMEOUT_S)
        if self._store is not None and self._persistent_key() != self._last_saved_key:
            try:
                await self._store.async_save(self._store_data())
                self._dirty = False
            except (HomeAssistantError, OSError, ValueError, TypeError) as err:
                # A storage failure never prevents the stop.
                _LOGGER.warning("%s - cannot save the context on stop: %s", self, err)

    # --------------------------------------------------------------- storage

    async def _async_load_store(self) -> None:
        assert self._store is not None
        try:
            data = await self._store.async_load()
        except (HomeAssistantError, OSError, ValueError, TypeError, NotImplementedError) as err:
            _LOGGER.warning("%s - cannot load the stored context: %s", self, err)
            self._startup_error = "store_load_failed"
            return
        if data is None:
            return
        if not isinstance(data, Mapping) or not isinstance(data.get("cycle"), Mapping):
            self._startup_error = "store_invalid"
            return
        self._cycle = parse_cycle_context(data["cycle"])
        self._cycle_origin = CYCLE_ORIGIN_LOCAL_RESTORE
        self._cycle_loaded = True
        self._legacy_seeded = bool(data.get("legacy_seeded", False))
        # A request that was not confirmed before the stop is never considered confirmed.
        self._record, self._startup_events = restart_record(record_from_dict(data.get(STORE_COMMAND_KEY)))
        self._last_saved_key = self._persistent_key()
        if self._startup_events:
            self._last_saved_key = None  # the unconfirmed status must be saved

    def _persistent_key(self) -> tuple[Any, ...]:
        return (json.dumps(cycle_to_dict(self._cycle), sort_keys=True), json.dumps(record_to_dict(self._record), sort_keys=True), self._legacy_seeded)

    def _store_data(self) -> dict[str, Any]:
        self._last_saved_key = self._persistent_key()
        self._pending_key = None
        self._dirty = False
        return {
            "cycle": cycle_to_dict(self._cycle),
            "legacy_seeded": self._legacy_seeded,
            "first_origin": self._cycle_origin,
            "saved_at": self._clock(),
            STORE_COMMAND_KEY: record_to_dict(self._record),
        }

    @callback
    def _schedule_save(self, delay: float = STORE_SAVE_DELAY_S) -> None:
        """Deferred write: it never blocks an evaluation or a command."""
        if self._store is None:
            return
        key = self._persistent_key()
        # Re-arming the timer at every evaluation would postpone the write for ever.
        if key == self._last_saved_key or key == self._pending_key:
            return
        self._pending_key = key
        self._dirty = True
        self._store.async_delay_save(self._store_data, delay)

    async def _async_save_task(self, data: dict[str, Any]) -> None:
        assert self._store is not None
        try:
            await self._store.async_save(data)
        except (HomeAssistantError, OSError, ValueError, TypeError) as err:
            _LOGGER.warning("%s - cannot save the command record: %s", self, err)
            self._persist_error = "command_save_failed"
            self._last_saved_key = None
        else:
            self._persist_error = None

    async def _async_persist_before_send(self, mode: str) -> None:
        """Save the command record immediately before the service call.

        ``off`` has priority: the write is queued and never awaited, the service
        starts at once. ``heat`` waits at most ``HEAT_PERSIST_WAIT_S`` and goes on
        whatever happens, recording the failure in ``last_error``.
        """
        if self._store is None:
            return
        task = self._hass.async_create_task(self._async_save_task(self._store_data()), name="vtherm heating orchestration save")
        self._save_tasks.add(task)
        task.add_done_callback(self._save_tasks.discard)
        if mode == COMMAND_OFF:
            return
        try:
            async with asyncio.timeout(HEAT_PERSIST_WAIT_S):
                await asyncio.shield(task)
        except TimeoutError:
            _LOGGER.warning("%s - the command record is not saved yet, sending anyway", self)
            self._persist_error = "command_save_timeout"

    @callback
    def _seed_once(self) -> None:
        """Choose the initial cycle once: local restore, legacy seed or empty."""
        if self._cycle_loaded:
            return
        self._cycle_loaded = True
        legacy = self._config.legacy_cycle_entity_id if self._config else None
        if legacy and not self._legacy_seeded:
            state = self._hass.states.get(legacy)
            value = None
            if state is not None and state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                try:
                    value = json.loads(state.state)
                except ValueError:
                    value = None
            if isinstance(value, Mapping):
                self._cycle = parse_cycle_context(value)
                self._cycle_origin = CYCLE_ORIGIN_LEGACY_SEED
                self._legacy_seeded = True
                return
            self._startup_error = "legacy_seed_invalid"
        self._cycle = None
        self._cycle_origin = CYCLE_ORIGIN_NEW_OBSERVATION

    # ------------------------------------------------------------- scheduling

    @callback
    def async_request_evaluation(self, *_: Any) -> None:
        """Coalesce evaluation requests into one run on the next loop iteration."""
        if not self._running or self._pending is not None:
            return
        epoch = self._epoch
        self._pending = self._hass.loop.call_soon(self._run_pending, epoch)

    @callback
    def _run_pending(self, epoch: int) -> None:
        self._pending = None
        if epoch != self._epoch or not self._running:
            return
        self._evaluate()

    @callback
    def _on_timer(self) -> None:
        self.async_request_evaluation()

    @callback
    def _watch_targets(self, entity_ids: tuple[str, ...]) -> tuple[str, ...]:
        owners = self._config.external_owner_entity_ids if self._config else ()
        return tuple(sorted((set(entity_ids) | set(owners)) - self._own_entity_ids))

    @callback
    def _resubscribe(self, entity_ids: tuple[str, ...]) -> None:
        ids = self._watch_targets(entity_ids)
        if ids == self._watched and self._state_unsub is not None:
            return
        if self._state_unsub is not None:
            self._state_unsub()
            self._state_unsub = None
        self._watched = ids
        if not ids:
            return
        epoch = self._epoch

        @callback
        def _on_state(_event: Event) -> None:
            if epoch == self._epoch:
                self.async_request_evaluation()

        self._state_unsub = async_track_state_change_event(self._hass, list(ids), _on_state)

    # ------------------------------------------------------------- evaluation

    def _compute(self, now: float) -> EvaluationResult:
        """Collector + M1 on one snapshot and one clock value. Pure with respect to the manager."""
        assert self._config is not None
        collection = collect_boiler_inputs(
            self._hass.states,
            self._config,
            now,
            temperature_unit=self._hass.config.units.temperature_unit,
            local_tz=dt_util.get_default_time_zone(),
        )
        decision = evaluate_boiler(build_policy_snapshot(collection), self._cycle, now)
        return EvaluationResult(now=now, collection=collection, decision=decision)

    def _external_owners_now(self) -> tuple[ExternalOwner, ...]:
        config = self._config
        if config is None:
            return ()
        owners = []
        for entity_id in config.external_owner_entity_ids:
            state = self._hass.states.get(entity_id)
            owners.append(ExternalOwner(entity_id, None if state is None else str(state.state)))
        return tuple(owners)

    def _can_send(self, epoch: int) -> bool:
        return epoch == self._epoch and self._running and not self._stopped and not self._hass.is_stopping

    def _read_ownership(self, master_mode: str | None, evaluation_valid: bool) -> Ownership:
        """The ownership interlock, read from the current states."""
        return evaluate_ownership(
            control_requested=self.mode == MODE_CONTROL,
            active=self._running and not self._stopped,
            external_owners=self._external_owners_now(),
            master_mode=master_mode,
            evaluation_valid=evaluation_valid,
        )

    def _control_fields(self, status: str | None = None) -> dict[str, Any]:
        """Diagnostic control fields; the external controllers are shown whenever configured."""
        ownership = Ownership(self._ownership.is_owner, self._ownership.block_reason, self._external_owners_now())
        return command_diagnostics(self._record, ownership, status if status is not None else self._control_status)

    @callback
    def _evaluate(self) -> None:
        """One evaluation. No stage failure may stop the following ones."""
        config = self._config
        assert config is not None
        now = self._clock()

        own = self._own_entity_ids & (set(config.configured_entity_ids()) | {config.legacy_cycle_entity_id} | set(config.external_owner_entity_ids))
        if own:
            self._deadline_timer.cancel()
            self._publish_special(
                STATE_ERROR, empty_diagnostics(self.mode, owner=OWNER_OBSERVER, evaluated_at=now, last_error="config_self_reference", control=self._control_fields())
            )
            return

        if not self._inputs_ready:
            missing = [entity_id for entity_id in config.required_entity_ids() if self._hass.states.get(entity_id) is None]
            if missing or self._hass.state is not CoreState.running:
                detail = "waiting_for_entities:" + ",".join(missing) if missing else "waiting_for_home_assistant"
                self._publish_special(
                    STATE_STARTING,
                    empty_diagnostics(
                        self.mode, owner=OWNER_OBSERVER, evaluated_at=now, last_error=detail, control=self._control_fields(CONTROL_STARTING if self.mode == MODE_CONTROL else None)
                    ),
                )
                return
            self._inputs_ready = True

        self._seed_once()
        try:
            result = self._compute(now)
        except Exception as err:  # noqa: BLE001 - defensive: an unexpected error never stops the controller
            _LOGGER.exception("%s - evaluation failed: %s", self, err)
            self._deadline_timer.cancel()
            self._publish_special(
                STATE_ERROR,
                empty_diagnostics(self.mode, owner=OWNER_OBSERVER, evaluated_at=now, last_error=f"evaluation_error:{type(err).__name__}", control=self._control_fields()),
            )
            self._resubscribe(config.configured_entity_ids())
            return

        self._cycle = result.decision.cycle
        self._schedule_save()

        step: CommandStep | None = None
        try:
            step = self._control_plane(result)
        except Exception as err:  # noqa: BLE001 - the control plane failed: no command, observation continues
            _LOGGER.exception("%s - control plane failed: %s", self, err)
            self._control_status = CONTROL_INACTIVE
            self._ownership = Ownership(False, None, self._external_owners_now())  # never claim an ownership that was not verified

        try:
            self._publish_result(result)
        except Exception as err:  # noqa: BLE001 - a publication error never blocks the command
            _LOGGER.exception("%s - publication failed: %s", self, err)
        try:
            self._resubscribe(result.collection.watched_entity_ids)
            self._deadline_timer.schedule(result.decision.deadline, now)
            self._command_timer.schedule(None if step is None else step.wake_at, now)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("%s - scheduling failed: %s", self, err)

        if step is not None and step.send is not None:
            self._launch_send(step)

    # ------------------------------------------------------------ control plane

    def _control_plane(self, result: EvaluationResult) -> CommandStep | None:
        """Settling, ownership and command decision of one evaluation (control only)."""
        if self.mode != MODE_CONTROL:
            self._ownership = Ownership(False, None, self._external_owners_now())
            self._control_status = CONTROL_INACTIVE
            return None
        valid = result.evaluation_valid
        if valid:
            self._complete_evaluation_seen = True
        self._settling = update_settling(self._settling, now=result.now, valid=valid, action=result.decision.action)
        ownership = self._read_ownership(result.master_mode, self._complete_evaluation_seen)
        step = command_step(
            self._record,
            CommandInputs(
                now=result.now,
                action=result.decision.action,
                master_mode=result.master_mode,
                ownership=ownership,
                settled=self._settling.settled,
                shutting_down=self._hass.is_stopping,
            ),
            self._budget,
        )
        self._apply_step(step, ownership, reason=result.decision.reason, valves=result.collection.valid_valve_count)
        return step

    @callback
    def _apply_step(self, step: CommandStep, ownership: Ownership, *, reason: str | None = None, valves: int | None = None) -> None:
        """Adopt the record of a step, log its transitions and cancel a send that no longer applies."""
        previous = self._record
        self._record = step.record
        self._budget = step.memory
        self._ownership = ownership
        self._control_status = step.control_status
        for event in step.events:
            self._log_event(event, reason=reason, valves=valves)
        if step.record != previous:
            self._schedule_save(STORE_COMMAND_DELAY_S)
        task = self._send_task
        inflight = self._inflight_id
        if task is None or task.done() or task is asyncio.current_task() or inflight is None:
            return
        record = self._record
        # A replaced or cancelled request must not keep its service call alive (the
        # opposite command goes first). A confirmed one is left to finish by itself.
        replaced_by_opposite = any(event.kind == EVENT_SUPERSEDED and event.detail in (f"action:{COMMAND_HEAT}", f"action:{COMMAND_OFF}") for event in step.events)
        # An action that merely became ``none`` does not interrupt a service call: its outcome is ignored by send id (review M2-B M-1).
        if (
            not ownership.is_owner
            or record is None
            or record.send_id != inflight
            or (record.status == STATUS_SUPERSEDED and replaced_by_opposite)
            or record.status == STATUS_CANCELLED
        ):
            task.cancel()

    @callback
    def _launch_send(self, step: CommandStep) -> None:
        """Start the send task of a decision (one command in flight at a time)."""
        if step.send is None or not self._running:
            return
        task = self._send_task
        if task is not None and not task.done() and self._send_mode == step.send:
            return
        self._send_mode = step.send
        task = self._hass.async_create_task(self._async_send(self._epoch, step.send, step.attempt), name=f"vtherm boiler command {step.send}")
        # The task starts eagerly and may already be over (nothing to send after the revalidation).
        self._send_task, self._send_mode = (None, None) if task.done() else (task, step.send)

    async def _async_send(self, epoch: int, mode: str, attempt: int) -> None:
        """Revalidate with a fresh clock, then command the master and register the outcome."""
        try:
            async with self._send_lock:
                await self._async_send_locked(epoch, mode, attempt)
        finally:
            if self._send_task is asyncio.current_task():
                self._send_task = None
                self._send_mode = None

    async def _async_send_locked(self, epoch: int, mode: str, attempt: int) -> None:
        config = self._config
        if config is None or not self._can_send(epoch):
            return

        # Interlock, inputs and M1 are recomputed with a new clock before every send, retries included.
        now = self._clock()
        try:
            result = self._compute(now)
        except Exception as err:  # noqa: BLE001
            # Nothing is sent; the next poll decides again (the attempt was not consumed).
            _LOGGER.exception("%s - revalidation before the command failed: %s", self, err)
            return
        self._cycle = result.decision.cycle
        ownership = self._read_ownership(result.master_mode, self._complete_evaluation_seen)
        fresh = command_step(
            self._record,
            CommandInputs(
                now=now, action=result.decision.action, master_mode=result.master_mode, ownership=ownership, settled=self._settling.settled, shutting_down=self._hass.is_stopping
            ),
            self._budget,
        )
        self._apply_step(fresh, ownership, reason=result.decision.reason, valves=result.collection.valid_valve_count)
        if fresh.send != mode or fresh.attempt != attempt or not ownership.is_owner:
            # The action changed, the master moved or the ownership is gone: nothing is sent.
            self._abandoned()
            return

        self._send_seq += 1
        send_id = self._send_seq
        try:
            record, event = start_send(self._record, mode=mode, attempt=attempt, now=now, send_id=send_id, base_master=result.master_mode)
        except ValueError as err:
            _LOGGER.error("%s - inconsistent command request: %s", self, err)
            return
        self._record = record
        self._abandon_streak = 0
        self._control_status = CONTROL_PENDING
        self._log_event(event, reason=result.decision.reason, valves=result.collection.valid_valve_count)
        self.async_request_evaluation()
        self._inflight_id = send_id
        try:
            await self._async_execute(epoch, mode, send_id, result, config)
        finally:
            if self._inflight_id == send_id:
                self._inflight_id = None

    async def _async_execute(self, epoch: int, mode: str, send_id: int, result: EvaluationResult, config: OrchestrationConfig) -> None:
        """Persist the record, call the service and register the outcome of one send."""
        await self._async_persist_before_send(mode)
        if not self._can_send(epoch):
            return
        # The save may have taken a while: the request must still be ours and still unsent.
        current = self._record
        if current is None or current.send_id != send_id or current.status != STATUS_SENDING:
            self._abandoned()
            return
        # Last look at the interlock after a possible wait for the save.
        if not self._read_ownership(result.master_mode, self._complete_evaluation_seen).is_owner:
            _LOGGER.warning("%s - ownership lost before the service call: %s not sent", self, mode)
            self._cancel_unsent(send_id)
            return

        error = self._unsupported_mode_error(config.master_entity_id, mode)
        if error is None:
            try:
                async with asyncio.timeout(SERVICE_TIMEOUT_S):
                    await self._hass.services.async_call("climate", "set_hvac_mode", {"entity_id": config.master_entity_id, "hvac_mode": mode}, blocking=True)
            except TimeoutError:
                error = ERROR_SERVICE_TIMEOUT
                _LOGGER.warning("%s - climate.set_hvac_mode %s timed out after %s s", self, mode, SERVICE_TIMEOUT_S)
            except Exception as err:  # noqa: BLE001 - any service failure is registered and retried with backoff
                error = f"service_error:{type(err).__name__}"
                _LOGGER.warning("%s - climate.set_hvac_mode %s failed: %s", self, mode, err)
        if not self._can_send(epoch):
            return  # unloaded meanwhile: the late outcome is ignored

        outcome = self._clock()
        if error is None:
            self._record = on_service_completed(self._record, now=outcome, send_id=send_id)
        else:
            self._record, events = on_service_failed(self._record, now=outcome, send_id=send_id, error=error)
            for failure in events:
                self._log_event(failure, reason=result.decision.reason)
        self._schedule_save(STORE_COMMAND_DELAY_S)
        self.async_request_evaluation()

    def _abandoned(self) -> None:
        """A send was dropped: re-evaluate at once only the first time, then the normal cadence decides.

        An immediate request after every abandonment would spin the loop if the recomputation kept
        disagreeing with the evaluation that launched the send (review M2-B I-3).
        """
        self._abandon_streak += 1
        if self._abandon_streak <= 1:
            self.async_request_evaluation()

    def _unsupported_mode_error(self, master_entity_id: str | None, mode: str) -> str | None:
        """The master must expose the mode: a missing one is a failure, never a blind call."""
        state = self._hass.states.get(master_entity_id) if master_entity_id else None
        modes = state.attributes.get("hvac_modes") if state is not None else None
        if isinstance(modes, (list, tuple)) and modes and mode not in [str(item) for item in modes]:
            return "hvac_mode_unsupported"
        return None

    def _cancel_unsent(self, send_id: int) -> None:
        """The ownership was lost between the record and the service call."""
        record = self._record
        if record is not None and record.send_id == send_id and record.is_active:
            self._record = replace(record, status=STATUS_CANCELLED, retry_at=None)
            self._log_event(CommandEvent("cancelled", record.mode, record.attempts, "ownership_lost_before_send"))
            self._schedule_save(STORE_COMMAND_DELAY_S)
        self._abandoned()

    # -------------------------------------------------------------- publication

    def _last_error(self, collection: BoilerCollection, now: float) -> str | None:
        """Per evaluation, with an explicit priority: unit, command, persistence, startup."""
        if self._startup_error is not None:
            if self._startup_error_at is None:
                self._startup_error_at = now
            elif now - self._startup_error_at >= STARTUP_ERROR_TTL_S:
                self._startup_error = None
                self._startup_error_at = None
        if not collection.temperature_unit_supported:
            return "temperature_unit_unsupported"
        record = self._record
        if record is not None and record.status == STATUS_FAILED and record.error:
            return record.error
        if self._persist_error:
            return self._persist_error
        return self._startup_error

    @callback
    def _publish_result(self, result: EvaluationResult) -> None:
        collection, decision = result.collection, result.decision
        control: dict[str, Any] | None = None
        owner = OWNER_OBSERVER
        if self.mode == MODE_CONTROL:
            control = command_diagnostics(self._record, self._ownership, self._control_status)
            if self._ownership.is_owner:
                owner = OWNER_CONTROLLER
        elif self._config is not None and self._config.external_owner_entity_ids:
            control = {"external_owners": [{"entity_id": item.entity_id, "state": item.state} for item in self._external_owners_now()]}
        last_error = self._last_error(collection, result.now)
        attributes = build_boiler_diagnostics(
            collection,
            decision,
            mode=self.mode,
            owner=owner,
            generation=self._generation + 1,
            cycle_origin=self._cycle_origin or CYCLE_ORIGIN_NEW_OBSERVATION,
            last_error=last_error,
            control=control,
        )
        if decision.reason != self._last_reason:
            _LOGGER.debug("%s - boiler decision %s/%s (%s)", self, decision.action, decision.reason, self.mode)
            self._last_reason = decision.reason
        self._publish_evaluation(decision.reason, attributes, result.now)

    # ------------------------------------------------------------------ logbook

    @callback
    def _log_event(self, event: CommandEvent, *, reason: str | None = None, valves: int | None = None) -> None:
        """One logbook entry per transition (never per tick), with the Oikos prefix."""
        config = self._config
        if config is None or config.master_entity_id is None:
            return
        parts = [f"{LOGBOOK_PREFIX}boiler", event.kind, event.mode or "-", f"attempt={event.attempt}/{MAX_SENDS}"]
        if reason or self._last_reason:
            parts.append(f"reason={reason or self._last_reason}")
        if valves is not None:
            parts.append(f"valves={valves}")
        if event.detail:
            parts.append(f"detail={event.detail}")
        message = "|".join(parts)
        _LOGGER.info("%s - %s", self, message)
        self._hass.bus.async_fire(EVENT_LOGBOOK_ENTRY, {ATTR_NAME: LOGBOOK_NAME, LOGBOOK_ENTRY_MESSAGE: message, ATTR_DOMAIN: "climate", ATTR_ENTITY_ID: config.master_entity_id})

    def __str__(self) -> str:
        return f"HeatingOrchestration[{self._entry_id}]"
