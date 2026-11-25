"""PDC (heat pump) runtime manager of the central configuration (W2).

One manager per central configuration entry, parallel to the boiler manager
(``feature_heating_orchestration_manager.py``) and independent of it. It is the thin
Home Assistant glue around :class:`heating_pdc_runtime.PdcRuntime`, which holds every
decision and sequence and is tested without Home Assistant:

* listeners on the state changes of every configured entity, on the climate service
  calls (the manual override provenance), a 15 s tick and the Home Assistant start/stop;
* a versioned ``Store`` (never ``.storage`` directly) for the transactions, the
  reservation, the cycle, the overrides and the general suspension;
* the VTherm inhibition through the existing ``vtherm_api`` facade
  (``set_inhibition`` / ``unset_inhibition`` / ``is_inhibition_started``);
* the diagnostic publishers (one sensor per PDC room, plus the suspension state);
* the entry points of the general suspension and of the manual override resume.

Modes (``pdc_mode`` of the configuration, independent of the boiler ``mode``):

* ``disabled``: nothing is read, listened to or sent;
* ``observe``: reads and publishes; **no command, no inhibition, no transaction, no write**;
* ``control``: commands the PDC rooms, only when every ``external_owner_entity_ids`` of the
  room is confirmed ``off`` (never switched off automatically) and the whole PDC section
  declares them explicitly.

Integration interface for the central API (documented in ``M4-M5-RUNTIME-ESITO.md``):
``FeatureHeatingPdcManager(hass, entry_id, section)`` with ``section =
config["heating_orchestration"]["pdc_runtime"]``, ``await manager.async_start()`` after the
platforms are set up, ``await manager.async_stop()`` on unload, ``manager.attach(room_key,
publisher, entity_id)`` for each room sensor.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.const import EVENT_CALL_SERVICE, EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Context, CoreState, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from vtherm_api.log_collector import get_vtherm_logger

from .heating_pdc_collector import (
    MODE_CONTROL,
    MODE_DISABLED,
    STATE_DISABLED,
    STATE_ERROR,
    STATE_STARTING,
    PdcConfig,
    PdcConfigError,
    empty_pdc_diagnostics,
    parse_pdc_config,
)
from .heating_pdc_runtime import PdcRuntime, PdcSuspendResult

_LOGGER = get_vtherm_logger(__name__)

#: Key of the sub-section of ``heating_orchestration`` that this manager reads.
CONF_PDC_RUNTIME = "pdc_runtime"
#: Key (next to the sub-section) of the PDC mode: ``disabled`` | ``observe`` | ``control``.
CONF_PDC_MODE = "pdc_mode"
#: Key (next to the sub-section) of the explicit confirmation of the PDC ``control`` mode.
CONF_PDC_CONTROL_CONFIRMED = "pdc_control_confirmed"
#: Release gate for the runtime. Control still requires a valid, explicitly confirmed configuration.
PDC_CONTROL_AVAILABLE = True
PDC_STORAGE_VERSION = 1
PDC_STORAGE_KEY_PREFIX = "versatile_thermostat.heating_orchestration_pdc"
EVALUATION_INTERVAL = timedelta(seconds=15)
SERVICE_TIMEOUT_S = 30.0
PUBLISH_REFRESH_S = 14.0
#: Attributes that change at every pass without carrying information: they do not by themselves trigger a sensor update.
PUBLISH_VOLATILE_KEYS = ("evaluated_at", "generation", "deadline")

Publisher = Callable[[str, dict[str, Any]], None]


def _utc_clock() -> float:
    return dt_util.utcnow().timestamp()


def extract_pdc_section(heating_orchestration_section: Any) -> Mapping[str, Any] | None:
    """The PDC sub-section of the ``heating_orchestration`` configuration, or ``None`` when it is absent (absent means disabled).

    ``pdc_mode`` (``disabled``/``observe``/``control``), when present next to the sub-section, is the PDC mode and
    overrides the ``mode`` of the sub-section: the PDC has its own mode, independent of the boiler ``mode``.
    """
    if not isinstance(heating_orchestration_section, Mapping):
        return None
    section = heating_orchestration_section.get(CONF_PDC_RUNTIME)
    if not isinstance(section, Mapping):
        return None
    mode = heating_orchestration_section.get(CONF_PDC_MODE)
    merged = {**section, "mode": mode} if mode is not None else dict(section)
    merged.pop("control_confirmed", None)  # Only the explicit section-level confirmation is authoritative.
    confirmed = heating_orchestration_section.get(CONF_PDC_CONTROL_CONFIRMED)
    if confirmed is not None:
        # The confirmation lives next to the sub-section (the flow writes it there); the parser
        # expects it inside the section it validates, under its own key.
        merged["control_confirmed"] = confirmed
    return merged


class _VThermInhibition:
    """The existing VTherm inhibition API (``vtherm_api`` facade). Imported late: it is only needed in ``control``."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def _api(self) -> Any:
        from vtherm_api.vtherm_api import VThermAPI  # pylint: disable=import-outside-toplevel

        return VThermAPI.get_vtherm_api(self._hass)

    async def set(self, entity_id: str, stop_underlyings: bool) -> None:
        """Enter the inhibition (idempotent in the core: the caller reads the flag before and after)."""
        await self._api().set_inhibition(entity_id, stop_underlyings)

    async def unset(self, entity_id: str) -> None:
        """Leave the inhibition (the core reconciles the underlyings before opening the gate)."""
        await self._api().unset_inhibition(entity_id)

    def started(self, entity_id: str) -> bool | None:
        """The VTherm accepts inhibition operations (its snapshot was restored)."""
        try:
            return bool(self._api().is_inhibition_started(entity_id))
        except (LookupError, TypeError, ValueError):
            return None


class _HassPorts:
    """``PdcPorts`` of the runtime on a real Home Assistant."""

    def __init__(self, manager: FeatureHeatingPdcManager, inhibition: Any) -> None:
        self._manager = manager
        self._hass = manager.hass
        self._inhibition = inhibition
        self._commands_allowed = manager.mode == MODE_CONTROL and PDC_CONTROL_AVAILABLE

    def _guard(self) -> None:
        """Second barrier of ``observe``: even a bug of the engine cannot reach a service, the inhibition or the store."""
        if not self._commands_allowed:
            raise HomeAssistantError("the PDC runtime is not in control: no command, inhibition or write is allowed")

    def now(self) -> float:
        return self._manager.clock()

    def ready(self) -> bool:
        return self._hass.state is CoreState.running

    def states(self) -> Any:
        return self._hass.states

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        self._guard()
        # No user in the context: the runtime's own commands are never taken as a user command by the provenance.
        async with asyncio.timeout(SERVICE_TIMEOUT_S):
            await self._hass.services.async_call(domain, service, data, blocking=True, context=Context())

    async def set_inhibition(self, entity_id: str, stop_underlyings: bool) -> None:
        self._guard()
        await self._inhibition.set(entity_id, stop_underlyings)

    async def unset_inhibition(self, entity_id: str) -> None:
        self._guard()
        await self._inhibition.unset(entity_id)

    def inhibition_started(self, entity_id: str) -> bool | None:
        return self._inhibition.started(entity_id) if self._commands_allowed else None

    async def save(self, data: dict[str, Any]) -> None:
        self._guard()
        store = self._manager.store
        if store is None:
            raise HomeAssistantError("store not ready")
        await store.async_save(data)

    def call_later(self, delay_s: float, callback_fn: Callable[[], None]) -> Callable[[], None]:
        @callback
        def _fire(_now: datetime) -> None:
            callback_fn()

        return async_call_later(self._hass, delay_s, _fire)

    def publish(self, room_key: str, state: str, attributes: dict[str, Any]) -> None:
        self._manager.publish(room_key, state, attributes)

    def notify(self, notification_id: str, title: str | None, message: str | None) -> None:
        full_id = f"{self._manager.notification_prefix}_{notification_id}"
        if message is None:
            persistent_notification.async_dismiss(self._hass, full_id)
        else:
            persistent_notification.async_create(self._hass, message, title=title, notification_id=full_id)

    def spawn(self, coro: Awaitable[Any], name: str) -> asyncio.Future[Any]:
        return self._hass.async_create_task(coro, name=name, eager_start=False)


class FeatureHeatingPdcManager:
    """PDC runtime of one central configuration."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        section: Any,
        *,
        clock: Callable[[], float] | None = None,
        inhibition: Any | None = None,
    ) -> None:
        self.hass = hass
        self._entry_id = entry_id
        self._clock = clock or _utc_clock
        self._inhibition = inhibition or _VThermInhibition(hass)
        self._config: PdcConfig | None
        self._config_error: str | None
        try:
            self._config = parse_pdc_config(section if section is not None else {}, allow_control=PDC_CONTROL_AVAILABLE)
            self._config_error = None
        except PdcConfigError as err:
            self._config = None
            self._config_error = f"config_{err.code}" + (f":{err.key}" if err.key else "")
        self._publishers: dict[str, list[Publisher]] = {}
        self._suspension_publishers: list[Publisher] = []
        self._states: dict[str, tuple[str, dict[str, Any]]] = {}
        self._last_pushed: dict[str, tuple[str, dict[str, Any], float]] = {}
        self._runtime: PdcRuntime | None = None
        self._store: Store | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._once_unsubs: dict[str, CALLBACK_TYPE] = {}
        self._running = False
        self._stopped = False
        self._epoch = 0
        self._publish_initial()

    # ------------------------------------------------------------------ status

    @property
    def mode(self) -> str:
        """Effective mode: ``disabled`` also for an invalid configuration (never a control by default)."""
        return MODE_DISABLED if self._config is None else self._config.mode

    @property
    def entry_id(self) -> str:
        """Central configuration entry id."""
        return self._entry_id

    @property
    def config(self) -> PdcConfig | None:
        """Validated configuration (``None`` when invalid)."""
        return self._config

    @property
    def config_error(self) -> str | None:
        """Stable code of an invalid configuration."""
        return self._config_error

    @property
    def runtime(self) -> PdcRuntime | None:
        """The engine (``None`` until started and while disabled)."""
        return self._runtime

    @property
    def store(self) -> Store | None:
        """The versioned store."""
        return self._store

    @property
    def store_key(self) -> str:
        """Storage key of the PDC runtime data."""
        return f"{PDC_STORAGE_KEY_PREFIX}.{self._entry_id}"

    @property
    def notification_prefix(self) -> str:
        """Prefix of the persistent notification ids of this manager."""
        return f"versatile_thermostat_{self._entry_id}"

    @property
    def is_running(self) -> bool:
        """True while listeners and timers are owned."""
        return self._running

    @property
    def room_keys(self) -> tuple[str, ...]:
        """The configured PDC rooms (the sensors to create)."""
        return () if self._config is None else tuple(room.room_key for room in self._config.rooms)

    @property
    def watched_entity_ids(self) -> tuple[str, ...]:
        """Entities listened to."""
        return () if self._config is None else self._config.configured_entity_ids()

    def clock(self) -> float:
        """The manager clock (Unix seconds)."""
        return self._clock()

    def state(self, room_key: str) -> tuple[str, dict[str, Any]]:
        """Last published ``(state, attributes)`` of a room."""
        if self._config is None:
            # an invalid or refused configuration (``control`` included) is an error for every room, never a silent disabled
            return STATE_ERROR, empty_pdc_diagnostics(room_key, MODE_DISABLED, last_error=self._config_error)
        return self._states.get(room_key, (STATE_DISABLED, empty_pdc_diagnostics(room_key, self.mode)))

    # --------------------------------------------------------------- publishing

    @callback
    def adopt_publishers(self, previous: FeatureHeatingPdcManager) -> None:
        """Reconnect surviving sensors after a failed platform unload.

        Share the subscription containers: existing detach callbacks close over
        the previous manager and must still remove the very same subscriptions.
        The previous manager must have stopped before this transfer.
        """
        if previous.is_running or previous.entry_id != self.entry_id:
            raise ValueError("publishers can only be adopted from the stopped manager of this entry")
        self._publishers = previous._publishers
        self._suspension_publishers = previous._suspension_publishers
        for room_key, publishers in self._publishers.items():
            for publisher in list(publishers):
                publisher(*self.state(room_key))
        for publisher in list(self._suspension_publishers):
            publisher(*self.suspension_attributes())

    @callback
    def attach(self, room_key: str, publisher: Publisher, entity_id: str | None = None) -> CALLBACK_TYPE:  # pylint: disable=unused-argument
        """Register the diagnostic sensor of a room and push its current state."""
        self._publishers.setdefault(room_key, []).append(publisher)
        publisher(*self.state(room_key))

        @callback
        def _detach() -> None:
            if publisher in self._publishers.get(room_key, []):
                self._publishers[room_key].remove(publisher)

        return _detach

    @callback
    def attach_suspension(self, publisher: Publisher) -> CALLBACK_TYPE:
        """Register the sensor of the general suspension (state ``none``/``shutting_down``/``confirmed``/``unreachable``)."""
        self._suspension_publishers.append(publisher)
        publisher(*self.suspension_attributes())

        @callback
        def _detach() -> None:
            if publisher in self._suspension_publishers:
                self._suspension_publishers.remove(publisher)

        return _detach

    def suspension_attributes(self) -> tuple[str, dict[str, Any]]:
        """``(state, attributes)`` of the general suspension for the sensor."""
        runtime = self._runtime
        if runtime is None:
            return "none", {"schema_version": 1, "active": False, "rooms": {}, "since": None, "reason": None}
        suspension = runtime.suspension
        return runtime.suspension_state(), {
            "schema_version": 1,
            "active": suspension.active,
            "since": suspension.since,
            "reason": suspension.reason,
            "hold_radiator_inhibition": suspension.hold_radiator_inhibition,
            "rooms": dict(runtime._room_states()),  # pylint: disable=protected-access
        }

    @callback
    def publish(self, room_key: str, state: str, attributes: dict[str, Any]) -> None:
        """Called by the runtime with each new room diagnostic."""
        self._states[room_key] = (state, attributes)
        now = self.clock()
        material = {key: value for key, value in attributes.items() if key not in PUBLISH_VOLATILE_KEYS}
        last = self._last_pushed.get(room_key)
        if last is not None and last[0] == state and last[1] == material and now - last[2] < PUBLISH_REFRESH_S:
            return  # nothing but the clock changed: the sensor is refreshed at most every 15 s (every power sample would flood it)
        self._last_pushed[room_key] = (state, material, now)
        for publisher in list(self._publishers.get(room_key, [])):
            publisher(state, attributes)
        for publisher in list(self._suspension_publishers):
            publisher(*self.suspension_attributes())

    def _publish_initial(self) -> None:
        if self._config is None:
            for room_key in ("pdc",):
                self._states[room_key] = (STATE_ERROR, empty_pdc_diagnostics(room_key, MODE_DISABLED, last_error=self._config_error))
            return
        state = STATE_DISABLED if self._config.mode == MODE_DISABLED else STATE_STARTING
        for room in self._config.rooms:
            self._states[room.room_key] = (state, empty_pdc_diagnostics(room.room_key, self._config.mode))

    # ---------------------------------------------------------------- lifecycle

    async def async_start(self) -> None:
        """Start. Disabled and invalid configurations own nothing; a failed load of the store never starts a control."""
        if self._running or self._stopped or self._config is None or self._config.mode == MODE_DISABLED:
            return
        self._store = Store(self.hass, PDC_STORAGE_VERSION, self.store_key)
        stored: Any = None
        try:
            stored = await self._store.async_load()
        except (HomeAssistantError, OSError, ValueError, TypeError) as err:
            _LOGGER.warning("%s - cannot load the PDC runtime data: %s", self, err)
            if self._config.mode == MODE_CONTROL:
                # An unreadable store may hold a transaction that owns an inhibition: never start a control blind.
                self._set_config_error("store_load_failed")
                return
        if self._stopped:
            return
        self._runtime = PdcRuntime(self._config, _HassPorts(self, self._inhibition), stored=stored, allow_control=PDC_CONTROL_AVAILABLE)
        self._running = True
        epoch = self._epoch
        runtime = self._runtime

        @callback
        def _on_state(event: Event) -> None:
            if epoch == self._epoch:
                runtime.on_state_changed(event.data["entity_id"], event.data.get("old_state"), event.data.get("new_state"))

        @callback
        def _on_service(event: Event) -> None:
            if epoch == self._epoch:
                runtime.on_call_service(event.data.get("domain", ""), event.data.get("service", ""), event.data.get("service_data"), event.context.user_id)

        @callback
        def _on_interval(_now: datetime) -> None:
            if epoch == self._epoch:
                runtime.request_evaluation()

        @callback
        def _on_started(_event: Event) -> None:
            self._once_unsubs.pop(EVENT_HOMEASSISTANT_STARTED, None)
            if epoch == self._epoch:
                runtime.request_evaluation()

        async def _on_stop(_event: Event) -> None:
            self._once_unsubs.pop(EVENT_HOMEASSISTANT_STOP, None)
            if epoch == self._epoch:
                await self.async_stop()

        @callback
        def _climate_only(data: Mapping[str, Any]) -> bool:
            return data.get("domain") == "climate"

        self._unsubs.append(async_track_state_change_event(self.hass, list(self._config.configured_entity_ids()), _on_state))
        if self._config.mode == MODE_CONTROL:
            self._unsubs.append(self.hass.bus.async_listen(EVENT_CALL_SERVICE, _on_service, event_filter=_climate_only))
        self._unsubs.append(async_track_time_interval(self.hass, _on_interval, EVALUATION_INTERVAL))
        if self.hass.state is not CoreState.running:
            self._once_unsubs[EVENT_HOMEASSISTANT_STARTED] = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)
        self._once_unsubs[EVENT_HOMEASSISTANT_STOP] = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)
        runtime.start()

    def _set_config_error(self, code: str) -> None:
        for room_key in self.room_keys:
            self.publish(room_key, STATE_ERROR, empty_pdc_diagnostics(room_key, MODE_DISABLED, last_error=code))

    async def async_stop(self) -> None:
        """Cancel every listener, timer and worker; nothing is released and no compensation is sent."""
        if self._stopped:
            return
        self._stopped = True
        self._running = False
        self._epoch += 1
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for unsub in self._once_unsubs.values():
            unsub()
        self._once_unsubs.clear()
        if self._runtime is not None:
            await self._runtime.async_stop()

    # -------------------------------------------------------------- entry points

    async def async_suspend(self, reason: str = "manual", *, hold_radiator_inhibition: bool = True) -> PdcSuspendResult:
        """General suspension: stop the PDC rooms and block new starts (see ``PdcRuntime.async_suspend``). Refused outside ``control``."""
        if self._runtime is None:
            return PdcSuspendResult(False, "none", "not_in_control")
        return await self._runtime.async_suspend(reason, hold_radiator_inhibition=hold_radiator_inhibition)

    async def async_resume(self) -> PdcSuspendResult:
        """Lift the general suspension (nothing is switched back on)."""
        if self._runtime is None:
            return PdcSuspendResult(False, "none", "not_in_control")
        return await self._runtime.async_resume()

    async def async_resume_automatic(self, room_key: str) -> bool:
        """Leave the manual override of a room now."""
        return False if self._runtime is None else await self._runtime.async_resume_automatic(room_key)

    @property
    def is_suspended(self) -> bool:
        """The general suspension is active (persisted)."""
        return self._runtime is not None and self._runtime.suspension.active

    def __str__(self) -> str:
        return f"HeatingPdc[{self._entry_id}]"
