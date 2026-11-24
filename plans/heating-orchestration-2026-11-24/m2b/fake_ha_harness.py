"""Harness of M2-B: runs the REAL manager module against a hand-written fake of the few Home Assistant APIs it uses.

Run from anywhere: python -I plans/heating-orchestration-2026-11-24/m2b/fake_ha_scenarios.py

No Home Assistant code is imported or executed (the stubs below replace it). It checks the asyncio logic of the manager
(eager tasks, locks, cancellation, timers) that the pure tests cannot reach. It is NOT a replacement for the HA tests.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
COMPONENT = REPO / "custom_components" / "versatile_thermostat"


# ------------------------------------------------------------------ fake homeassistant


class CoreState:
    running = "running"
    starting = "starting"


class HomeAssistantError(Exception):
    pass


class Event:
    def __init__(self, event_type, data=None):
        self.event_type, self.data = event_type, data or {}


def callback(func):
    return func


class FakeState:
    def __init__(self, state, attributes):
        self.state, self.attributes = state, attributes


class FakeStates:
    def __init__(self, hass):
        self.hass = hass
        self._d: dict[str, FakeState] = {}

    def get(self, entity_id):
        return self._d.get(entity_id)

    def async_set(self, entity_id, state, attributes=None):
        old = self._d.get(entity_id)
        new = FakeState(state, dict(attributes or {}))
        self._d[entity_id] = new
        if old is not None and old.state == new.state and old.attributes == new.attributes:
            return
        for ids, cb in list(self.hass.state_listeners):
            if entity_id in ids:
                cb(Event("state_changed", {"entity_id": entity_id}))

    def async_remove(self, entity_id):
        self._d.pop(entity_id, None)
        for ids, cb in list(self.hass.state_listeners):
            if entity_id in ids:
                cb(Event("state_changed", {"entity_id": entity_id}))


class FakeBus:
    def __init__(self, hass):
        self.hass = hass
        self.fired: list[tuple[str, dict]] = []
        self.once: dict[str, list] = {}

    def async_fire(self, event_type, data=None):
        self.fired.append((event_type, data))

    def async_listen_once(self, event_type, cb):
        entry = (event_type, cb)
        self.once.setdefault(event_type, []).append(cb)

        def unsub():
            if cb in self.once.get(event_type, []):
                self.once[event_type].remove(cb)

        return unsub


class FakeServices:
    def __init__(self):
        self.calls: list[dict] = []
        self.handler = None  # async callable(call) -> None

    async def async_call(self, domain, service, data=None, blocking=False, **kwargs):
        call = {"domain": domain, "service": service, "data": dict(data or {}), "blocking": blocking}
        self.calls.append(call)
        if self.handler is not None:
            await self.handler(call)


class FakeUnits:
    temperature_unit = "°C"


class FakeConfig:
    units = FakeUnits()


class FakeHass:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.states = FakeStates(self)
        self.bus = FakeBus(self)
        self.services = FakeServices()
        self.config = FakeConfig()
        self.state = CoreState.running
        self.is_stopping = False
        self.state_listeners: list[tuple[set, Any]] = []
        self.point_timers: list[list] = []  # [ts, cb, alive]
        self.interval_timers: list[list] = []  # [interval_s, cb, alive]
        self.tasks: list[asyncio.Task] = []
        self.stores: dict[str, Any] = {}

    def async_create_task(self, coro, name=None, eager_start=True):
        task = asyncio.get_running_loop().create_task(coro, name=name, eager_start=eager_start)
        self.tasks.append(task)
        return task


def install_fakes():
    ha = types.ModuleType("homeassistant")
    const = types.ModuleType("homeassistant.const")
    for name, value in dict(
        ATTR_DOMAIN="domain",
        ATTR_ENTITY_ID="entity_id",
        ATTR_NAME="name",
        EVENT_HOMEASSISTANT_STARTED="homeassistant_started",
        EVENT_HOMEASSISTANT_STOP="homeassistant_stop",
        EVENT_LOGBOOK_ENTRY="logbook_entry",
        STATE_UNAVAILABLE="unavailable",
        STATE_UNKNOWN="unknown",
    ).items():
        setattr(const, name, value)
    core = types.ModuleType("homeassistant.core")
    core.CALLBACK_TYPE = object
    core.CoreState = CoreState
    core.Event = Event
    core.HomeAssistant = FakeHass
    core.callback = callback
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.HomeAssistantError = HomeAssistantError
    helpers = types.ModuleType("homeassistant.helpers")
    event = types.ModuleType("homeassistant.helpers.event")

    def async_track_point_in_utc_time(hass, cb, when):
        entry = [when.timestamp(), cb, True]
        hass.point_timers.append(entry)

        def unsub():
            entry[2] = False

        return unsub

    def async_track_time_interval(hass, cb, interval):
        entry = [interval.total_seconds(), cb, True]
        hass.interval_timers.append(entry)

        def unsub():
            entry[2] = False

        return unsub

    def async_track_state_change_event(hass, ids, cb):
        entry = (set(ids), cb)
        hass.state_listeners.append(entry)

        def unsub():
            if entry in hass.state_listeners:
                hass.state_listeners.remove(entry)

        return unsub

    event.async_track_point_in_utc_time = async_track_point_in_utc_time
    event.async_track_time_interval = async_track_time_interval
    event.async_track_state_change_event = async_track_state_change_event
    storage = types.ModuleType("homeassistant.helpers.storage")

    class Store:
        def __init__(self, hass, version, key, **kwargs):
            self.hass, self.version, self.key = hass, version, key
            self.delayed = None
            self.saves: list[dict] = []
            hass.stores[key] = self

        async def async_load(self):
            stored = self.hass.stored.get(self.key)
            if stored is None:
                return None
            if stored["version"] != self.version:
                return await self._async_migrate_func(stored["version"], 1, stored["data"])
            return stored["data"]

        async def async_save(self, data):
            if self.hass.save_hook is not None:
                await self.hass.save_hook(data)
            self.saves.append(json.loads(json.dumps(data)))
            self.hass.stored[self.key] = {"version": self.version, "data": json.loads(json.dumps(data))}
            self.delayed = None

        def async_delay_save(self, data_func, delay=0):
            self.delayed = (data_func, delay)

        async def flush_delayed(self):
            if self.delayed is not None:
                func, _ = self.delayed
                self.delayed = None
                await self.async_save(func())

        async def _async_migrate_func(self, old_major, old_minor, old_data):
            raise NotImplementedError

        async def async_remove(self):
            self.hass.stored.pop(self.key, None)

    storage.Store = Store
    util = types.ModuleType("homeassistant.util")
    dt = types.ModuleType("homeassistant.util.dt")
    dt.utcnow = lambda: datetime.now(timezone.utc)
    dt.utc_from_timestamp = lambda ts: datetime.fromtimestamp(ts, timezone.utc)
    dt.get_default_time_zone = lambda: timezone.utc
    util.dt = dt
    vtherm_api = types.ModuleType("vtherm_api")
    log_collector = types.ModuleType("vtherm_api.log_collector")
    import logging

    log_collector.get_vtherm_logger = lambda name: logging.getLogger(name)
    FakeHass.stored = {}
    FakeHass.save_hook = None
    for name, module in {
        "homeassistant": ha,
        "homeassistant.const": const,
        "homeassistant.core": core,
        "homeassistant.exceptions": exceptions,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.event": event,
        "homeassistant.helpers.storage": storage,
        "homeassistant.util": util,
        "homeassistant.util.dt": dt,
        "vtherm_api": vtherm_api,
        "vtherm_api.log_collector": log_collector,
    }.items():
        sys.modules[name] = module
    pkg = types.ModuleType("vtfake")
    pkg.__path__ = [str(COMPONENT)]
    sys.modules["vtfake"] = pkg
    const_stub = types.ModuleType("vtfake.const")
    const_stub.HO_STORAGE_KEY_PREFIX = "versatile_thermostat.heating_orchestration"
    const_stub.HO_STORAGE_VERSION = 2
    sys.modules["vtfake.const"] = const_stub
    return importlib.import_module("vtfake.feature_heating_orchestration_manager")


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


async def run_loop(times=8):
    for _ in range(times):
        await asyncio.sleep(0)


class World:
    """Test control of time: fires the fake timers due and lets the loop run."""

    def __init__(self, hass, clock):
        self.hass, self.clock = hass, clock
        self.interval_next: dict[int, float] = {}

    async def advance(self, seconds):
        target = self.clock.now + seconds
        # fire in order: interval ticks and point timers, never in the past
        while True:
            candidates = []
            for index, (interval, cb, alive) in enumerate(self.hass.interval_timers):
                if alive:
                    nxt = self.interval_next.setdefault(index, self._base + interval if hasattr(self, "_base") else self.clock.now + interval)
                    candidates.append((nxt, "i", index))
            for index, (ts, cb, alive) in enumerate(self.hass.point_timers):
                if alive:
                    candidates.append((ts, "p", index))
            candidates = [c for c in candidates if c[0] <= target]
            if not candidates:
                break
            when, kind, index = min(candidates)
            self.clock.now = max(self.clock.now, when)
            if kind == "i":
                interval, cb, _ = self.hass.interval_timers[index]
                self.interval_next[index] = when + interval
                cb(datetime.fromtimestamp(when, timezone.utc))
            else:
                ts, cb, _ = self.hass.point_timers[index]
                self.hass.point_timers[index][2] = False
                cb(datetime.fromtimestamp(ts, timezone.utc))
            await run_loop()
        self.clock.now = target
        await run_loop()

    def start_intervals(self):
        self._base = self.clock.now
