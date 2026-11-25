"""Home Assistant lifecycle and services for room overrides and the heating journal."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .heating_boiler_collector import MODE_CONTROL, OrchestrationConfig
from .room_journal_policy import KIND_OVERRIDE_END, KIND_OVERRIDE_START, StateView
from .room_journal_runtime import RoomJournalConfig, RoomJournalRuntime
from .room_override_runtime import RoomOverrideConfig, RoomOverrideRuntime

_LOGGER = logging.getLogger(__name__)


class _RoomPorts:
    def __init__(self, hass: HomeAssistant, store: Store) -> None:
        self.hass = hass
        self.store = store

    def now(self) -> float:
        return dt_util.utcnow().timestamp()

    def weekday(self) -> int:
        return dt_util.now().weekday()

    def state(self, entity_id: str) -> tuple[str | None, dict[str, Any]]:
        state = self.hass.states.get(entity_id)
        return (None, {}) if state is None else (state.state, state.attributes)

    async def call_service(self, domain: str, service: str, entity_id: str) -> None:
        async with asyncio.timeout(15):
            await self.hass.services.async_call(domain, service, {"entity_id": entity_id}, blocking=True)

    def call_at(self, at, run):
        # Must be a ``@callback``: a plain function is run by Home Assistant in an executor thread, where
        # ``asyncio.get_running_loop()`` (used by the runtimes to start their tasks) raises ``RuntimeError``.
        @callback
        def _fire(_now: datetime) -> None:
            run()

        return async_track_point_in_utc_time(self.hass, _fire, datetime.fromtimestamp(at, UTC))

    async def save(self, data: dict[str, Any]) -> None:
        await self.store.async_save(data)

    def candidates(self) -> dict[str, tuple[str, str | None]]:
        return {
            state.context.id: (state.entity_id, state.name)
            for state in self.hass.states.async_all(("automation", "script", "switch"))
            if state.domain != "switch" or state.entity_id.startswith("switch.schedule_")
        }

    def notify(self, notification_id: str, title: str | None, message: str | None) -> None:
        if message:
            persistent_notification.async_create(self.hass, message, title, notification_id)
        else:
            persistent_notification.async_dismiss(self.hass, notification_id)


class HeatingRoomManager:
    """Own the room workflows for one central configuration."""

    def __init__(self, hass: HomeAssistant, entry_id: str, config: OrchestrationConfig, request_evaluation) -> None:
        self.hass = hass
        self.config = config
        self.entry_id = entry_id
        self._request_evaluation = request_evaluation
        self._override_store = Store(hass, 1, f"versatile_thermostat.room_override.{entry_id}")
        self._journal_store = Store(hass, 1, f"versatile_thermostat.room_journal.{entry_id}")
        self._unsubscribe = None
        self._stopped = False
        self.override: RoomOverrideRuntime | None = None
        self.journal: RoomJournalRuntime | None = None

    async def start(self) -> None:
        if not self.config.rooms or self.journal is not None:
            return
        # Program overrides are write operations and only exist after explicit control confirmation.
        if self.config.mode == MODE_CONTROL and self.config.control_confirmed:
            rooms = [
                RoomOverrideConfig(
                    room.room_key,
                    room.scheduler_entity_ids,
                    room.workday_entity_id,
                    room.program_owner_entity_ids,
                )
                for room in self.config.rooms if room.scheduler_entity_ids
            ]
            if rooms:
                stored = await self._read(self._override_store)
                if self._stopped:
                    return
                self.override = RoomOverrideRuntime(rooms, _RoomPorts(self.hass, self._override_store), stored=stored)
                await self.override.start()

        journal_rooms = [
            RoomJournalConfig(room.room_key, tuple(entity for entity in (room.climate_entity_id, room.split_entity_id) if entity))
            for room in self.config.rooms
        ]
        stored = await self._read(self._journal_store)
        if self._stopped:
            return
        self.journal = RoomJournalRuntime(journal_rooms, _RoomPorts(self.hass, self._journal_store), stored=stored)
        await self.journal.start()
        self._unsubscribe = async_track_state_change_event(self.hass, self.journal.entity_ids(), self._record_change)

    @staticmethod
    async def _read(store: Store):
        try:
            return await store.async_load()
        except (HomeAssistantError, OSError, ValueError):
            _LOGGER.exception("Cannot load room orchestration state")
            return {"schema_version": -1}

    async def stop(self) -> None:
        self._stopped = True
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        if self.override:
            await self.override.stop()
            self.override = None
        if self.journal:
            await self.journal.stop()
            self.journal = None

    @callback
    def _record_change(self, event: Event) -> None:
        if self.journal is None:
            return
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if old is None or new is None:
            return
        ctx = new.context
        self.journal.on_state_change(
            new.entity_id, StateView(old.state, old.attributes), StateView(new.state, new.attributes),
            context_id=ctx.id, parent_id=ctx.parent_id, user_id=ctx.user_id,
        )

    def room_data(self, key: str) -> dict[str, Any]:
        values = {}
        if self.override and key in self.override.room_keys():
            values.update(self.override.status(key))
        if self.journal and key in self.journal.room_keys():
            values.update(self.journal.status(key))
        return values

    def manual_rooms(self, *, max_age_s: float | None = None) -> frozenset[str]:
        """Rooms with a confirmed pause, within the user-requested duration."""
        if self.override is None or self.override.load_error is not None:
            return frozenset()
        now = dt_util.utcnow().timestamp()
        return frozenset(
            key for key in self.override.room_keys()
            if (state := self.override.state(key)).active and not state.pausing
            and not state.ending and state.ends_at is not None and state.ends_at > now
            and (max_age_s is None or state.started_at is not None and 0 <= now - state.started_at < max_age_s)
        )

    @property
    def has_override(self) -> bool:
        return self.override is not None and (
            self.override.load_error is not None or any(self.override.state(key).active for key in self.override.room_keys())
        )

    async def set_override(self, key: str, minutes: float | None, user_id: str | None) -> str:
        if self.override is None:
            raise HomeAssistantError("Room program control is not configured")
        room = next((room for room in self.config.rooms if room.room_key == key), None)
        if room is None:
            raise HomeAssistantError("Unknown heating room")
        result = await (self.override.cancel_override(key) if minutes is None else self.override.start_override(key, minutes))
        if self.journal and result in ("started", "restarted", "ended"):
            self.journal.record_event(
                key, KIND_OVERRIDE_END if minutes is None else KIND_OVERRIDE_START, room.climate_entity_id,
                reason=result, user_id=user_id,
            )
        self._request_evaluation()
        return result

    def history(self, key: str, **filters: Any) -> dict[str, Any]:
        if self.journal is None:
            raise HomeAssistantError("Room heating journal is not configured")
        try:
            return self.journal.query(key, **filters)
        except ValueError as error:
            raise HomeAssistantError(str(error)) from error
