"""Boiler orchestration manager of the central configuration (M2-A: observe only).

One manager per central configuration entry. In ``observe`` it reads a single
coherent snapshot of the configured entities, runs the pure collector and the
M1 policy with one clock value, keeps its own cycle context in a versioned Home
Assistant ``Store`` and publishes the diagnostic sensor. It never calls a
service: no climate, helper, timer, inhibition or automation is touched.

Scheduling: state change listeners on the relevant entities (attribute changes
included), a recovery evaluation every 15 s and a one-shot timer at the M1
deadline when it is in the future. Requests are coalesced into one evaluation
per loop iteration; nothing reschedules itself with a zero delay. An epoch
token invalidates every callback that survives ``async_stop``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP, STATE_UNAVAILABLE, STATE_UNKNOWN
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


class FeatureHeatingOrchestrationManager:
    """Observer of the boiler orchestration for one central configuration."""

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
        self._deadline_unsub: CALLBACK_TYPE | None = None
        self._deadline_at: float | None = None
        self._pending: Any = None
        self._watched: tuple[str, ...] = ()
        self._inputs_ready = False

        self._store: Store | None = None
        self._cycle: BoilerCycle | None = None
        self._cycle_origin: str | None = None
        self._cycle_loaded = False
        self._legacy_seeded = False
        self._last_saved_cycle: dict[str, Any] | None = None
        self._dirty = False
        self._last_error: str | None = None
        self._last_reason: str | None = None

        if self._config_error is not None:
            self._state = STATE_ERROR
            self._attributes = empty_diagnostics(MODE_DISABLED, last_error=self._config_error)
        elif self.mode == MODE_DISABLED:
            self._state = STATE_DISABLED
            self._attributes = empty_diagnostics(MODE_DISABLED)
        else:
            self._state = STATE_STARTING
            self._attributes = empty_diagnostics(self.mode, owner=OWNER_OBSERVER)

    # ------------------------------------------------------------------ status

    @property
    def mode(self) -> str:
        """Effective mode: ``observe`` or ``disabled`` (also for invalid config)."""
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
        """Observer cycle context (never shared with the legacy helpers)."""
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
        self._state = state
        self._attributes = attributes
        for publisher in list(self._publishers):
            publisher(state, attributes)

    # ---------------------------------------------------------------- lifecycle

    async def async_start(self) -> None:
        """Start observing. Disabled or invalid configurations own nothing."""
        if self._running or self._stopped:
            return
        if self._config is None or self.mode != MODE_OBSERVE:
            self._publish(self._state, self._attributes)
            return
        self._store = Store(self._hass, HO_STORAGE_VERSION, self.store_key)
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
        self.async_request_evaluation()

    async def async_stop(self) -> None:
        """Cancel every owned listener/timer and flush a pending cycle write."""
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
        self._cancel_deadline()
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
        if self._store is not None and self._dirty:
            try:
                await self._store.async_save(self._store_data())
                self._dirty = False
            except (HomeAssistantError, OSError, ValueError, TypeError) as err:
                # A storage failure never prevents the stop.
                _LOGGER.warning("%s - cannot save the observer cycle on stop: %s", self, err)

    # --------------------------------------------------------------- storage

    async def _async_load_store(self) -> None:
        assert self._store is not None
        try:
            data = await self._store.async_load()
        except (HomeAssistantError, OSError, ValueError, TypeError) as err:
            _LOGGER.warning("%s - cannot load the observer cycle: %s", self, err)
            self._last_error = "store_load_failed"
            return
        if data is None:
            return
        if not isinstance(data, Mapping) or not isinstance(data.get("cycle"), Mapping):
            self._last_error = "store_invalid"
            return
        self._cycle = parse_cycle_context(data["cycle"])
        self._cycle_origin = CYCLE_ORIGIN_LOCAL_RESTORE
        self._cycle_loaded = True
        self._legacy_seeded = bool(data.get("legacy_seeded", False))
        self._last_saved_cycle = cycle_to_dict(self._cycle)

    def _store_data(self) -> dict[str, Any]:
        return {
            "cycle": cycle_to_dict(self._cycle),
            "legacy_seeded": self._legacy_seeded,
            "first_origin": self._cycle_origin,
            "saved_at": self._clock(),
        }

    @callback
    def _schedule_save(self) -> None:
        current = cycle_to_dict(self._cycle)
        if self._store is None or current == self._last_saved_cycle:
            return
        self._last_saved_cycle = current
        self._dirty = True
        self._store.async_delay_save(self._delayed_data, STORE_SAVE_DELAY_S)

    @callback
    def _delayed_data(self) -> dict[str, Any]:
        self._dirty = False
        return self._store_data()

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
            self._last_error = "legacy_seed_invalid"
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
    def _resubscribe(self, entity_ids: tuple[str, ...]) -> None:
        ids = tuple(sorted(set(entity_ids) - self._own_entity_ids))
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

    @callback
    def _cancel_deadline(self) -> None:
        if self._deadline_unsub is not None:
            self._deadline_unsub()
            self._deadline_unsub = None
        self._deadline_at = None

    @callback
    def _schedule_deadline(self, deadline: float | None, now: float) -> None:
        """One-shot timer at a future M1 deadline; passed deadlines rely on polling."""
        if deadline is None or deadline <= now:
            self._cancel_deadline()
            return
        if self._deadline_at == deadline and self._deadline_unsub is not None:
            return
        self._cancel_deadline()
        epoch = self._epoch

        @callback
        def _on_deadline(_now: datetime) -> None:
            self._deadline_unsub = None
            self._deadline_at = None
            if epoch == self._epoch:
                self.async_request_evaluation()

        self._deadline_at = deadline
        self._deadline_unsub = async_track_point_in_utc_time(self._hass, _on_deadline, dt_util.utc_from_timestamp(deadline))

    # ------------------------------------------------------------- evaluation

    @callback
    def _evaluate(self) -> None:
        """One evaluation: single clock, single snapshot, collector + M1, publish."""
        config = self._config
        assert config is not None
        now = self._clock()

        own = self._own_entity_ids & (set(config.configured_entity_ids()) | {config.legacy_cycle_entity_id})
        if own:
            self._cancel_deadline()
            self._publish(STATE_ERROR, empty_diagnostics(self.mode, owner=OWNER_OBSERVER, evaluated_at=now, last_error="config_self_reference"))
            return

        if not self._inputs_ready:
            missing = [entity_id for entity_id in config.required_entity_ids() if self._hass.states.get(entity_id) is None]
            if missing or self._hass.state is not CoreState.running:
                detail = "waiting_for_entities:" + ",".join(missing) if missing else "waiting_for_home_assistant"
                self._publish(STATE_STARTING, empty_diagnostics(self.mode, owner=OWNER_OBSERVER, evaluated_at=now, last_error=detail))
                return
            self._inputs_ready = True

        self._seed_once()
        try:
            collection = collect_boiler_inputs(
                self._hass.states,
                config,
                now,
                temperature_unit=self._hass.config.units.temperature_unit,
                local_tz=dt_util.get_default_time_zone(),
            )
            decision: BoilerDecision = evaluate_boiler(build_policy_snapshot(collection), self._cycle, now)
        except (TypeError, ValueError, KeyError) as err:  # defensive: never stop the observer
            _LOGGER.error("%s - evaluation failed: %s", self, err)
            self._cancel_deadline()
            self._publish(STATE_ERROR, empty_diagnostics(self.mode, owner=OWNER_OBSERVER, evaluated_at=now, last_error=f"evaluation_error:{type(err).__name__}"))
            return

        self._cycle = decision.cycle
        self._schedule_save()
        self._generation += 1
        last_error = self._last_error
        if last_error is None and not collection.temperature_unit_supported:
            last_error = "temperature_unit_unsupported"
        attributes = build_boiler_diagnostics(
            collection,
            decision,
            mode=self.mode,
            owner=OWNER_OBSERVER,
            generation=self._generation,
            cycle_origin=self._cycle_origin or CYCLE_ORIGIN_NEW_OBSERVATION,
            last_error=last_error,
        )
        if decision.reason != self._last_reason:
            _LOGGER.debug("%s - observed boiler proposal %s/%s", self, decision.action, decision.reason)
            self._last_reason = decision.reason
        self._publish(decision.reason, attributes)
        self._resubscribe(collection.watched_entity_ids)
        self._schedule_deadline(decision.deadline, now)

    def __str__(self) -> str:
        return f"HeatingOrchestration[{self._entry_id}]"
