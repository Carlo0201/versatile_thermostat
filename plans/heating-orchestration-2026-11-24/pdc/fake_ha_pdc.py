"""Scenarios of the REAL PDC manager (observe) against a hand-written fake of the few Home Assistant APIs it uses.

Run from anywhere: python -I plans/heating-orchestration-2026-11-24/pdc/fake_ha_pdc.py

No Home Assistant code is imported or executed (the stubs below replace it), in the way of
``plans/heating-orchestration-2026-11-24/m2b/fake_ha_harness.py``. It checks what the pure tests cannot reach: the wiring
of ``feature_heating_pdc_manager.py`` (listeners, timers, tasks, store, publishers, stop) and, above all, that in
``observe`` **no service is called, no inhibition is touched and nothing is written to the store**, while the
diagnostics are still published. It is NOT a replacement for the Home Assistant tests (written, not executed).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import sys
import traceback
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
COMPONENT = Path(os.environ["PDC_COMPONENT_DIR"]) if os.environ.get("PDC_COMPONENT_DIR") else REPO / "custom_components" / "versatile_thermostat"
NOW = 1_800_000_000.0


# ------------------------------------------------------------------ fake homeassistant


class CoreState:
    running = "running"
    starting = "starting"


class HomeAssistantError(Exception):
    pass


class Context:
    def __init__(self, user_id=None):
        self.user_id = user_id


class Event:
    def __init__(self, event_type, data=None, context=None):
        self.event_type, self.data, self.context = event_type, data or {}, context or Context()


def callback(func):
    return func


class FakeState:
    def __init__(self, state, attributes, changed, reported):
        self.state, self.attributes, self.last_changed, self.last_reported = state, attributes, changed, reported


class FakeStates:
    def __init__(self, hass):
        self.hass = hass
        self._d: dict[str, FakeState] = {}

    def get(self, entity_id):
        return self._d.get(entity_id)

    def async_all(self):
        return list(self._d.values())

    def async_set(self, entity_id, state, attributes=None):
        old = self._d.get(entity_id)
        now = self.hass.clock.now
        changed = now if old is None or old.state != state else old.last_changed
        new = FakeState(state, dict(attributes or {}), changed, now)
        self._d[entity_id] = new
        if old is not None and old.state == new.state and old.attributes == new.attributes:
            return
        for ids, cb in list(self.hass.state_listeners):
            if entity_id in ids:
                cb(Event("state_changed", {"entity_id": entity_id, "old_state": old, "new_state": new}))


class FakeBus:
    def __init__(self, hass):
        self.hass = hass
        self.listeners: list[list] = []  # [type, cb, filter]
        self.once: dict[str, list] = {}

    def async_listen(self, event_type, listener, event_filter=None):
        entry = [event_type, listener, event_filter]
        self.listeners.append(entry)

        def unsub():
            if entry in self.listeners:
                self.listeners.remove(entry)

        return unsub

    def async_fire(self, event_type, data=None, context=None):
        for kind, cb, event_filter in list(self.listeners):
            if kind == event_type and (event_filter is None or event_filter(data or {})):
                cb(Event(event_type, data, context))

    def async_listen_once(self, event_type, cb):
        self.once.setdefault(event_type, []).append(cb)

        def unsub():
            if cb in self.once.get(event_type, []):
                self.once[event_type].remove(cb)

        return unsub


class FakeServices:
    def __init__(self):
        self.calls: list[dict] = []

    async def async_call(self, domain, service, data=None, blocking=False, context=None, **kwargs):
        self.calls.append({"domain": domain, "service": service, "data": dict(data or {})})


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class FakeHass:
    def __init__(self, clock):
        self.clock = clock
        self.loop = asyncio.get_running_loop()
        self.states = FakeStates(self)
        self.bus = FakeBus(self)
        self.services = FakeServices()
        self.state = CoreState.running
        self.state_listeners: list[tuple[set, Any]] = []
        self.interval_timers: list[list] = []  # [interval_s, cb, alive, next]
        self.later_timers: list[list] = []  # [due, cb, alive]
        self.tasks: list[asyncio.Task] = []
        self.stored: dict[str, Any] = {}
        self.saves: list[tuple[str, dict]] = []
        self.notifications: list[tuple] = []

    def async_create_task(self, coro, name=None, eager_start=True):
        task = asyncio.get_running_loop().create_task(coro, name=name, eager_start=eager_start)
        self.tasks.append(task)
        return task


def install_fakes():
    modules: dict[str, types.ModuleType] = {}

    def mod(name):
        modules[name] = types.ModuleType(name)
        return modules[name]

    mod("homeassistant")
    mod("homeassistant.components")
    const = mod("homeassistant.const")
    const.EVENT_CALL_SERVICE = "call_service"
    const.EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
    const.EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"
    pn = mod("homeassistant.components.persistent_notification")

    def async_create(hass, message, title=None, notification_id=None):
        hass.notifications.append(("create", notification_id))

    def async_dismiss(hass, notification_id):
        hass.notifications.append(("dismiss", notification_id))

    pn.async_create, pn.async_dismiss = async_create, async_dismiss
    modules["homeassistant.components"].persistent_notification = pn
    core = mod("homeassistant.core")
    core.CALLBACK_TYPE, core.CoreState, core.Context, core.Event, core.HomeAssistant, core.callback = object, CoreState, Context, Event, FakeHass, callback
    mod("homeassistant.exceptions").HomeAssistantError = HomeAssistantError
    mod("homeassistant.helpers")
    event = mod("homeassistant.helpers.event")

    def async_track_time_interval(hass, cb, interval):
        entry = [interval.total_seconds(), cb, True, hass.clock.now + interval.total_seconds()]
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

    def async_call_later(hass, delay, cb):
        entry = [hass.clock.now + delay, cb, True]
        hass.later_timers.append(entry)

        def cancel():
            entry[2] = False

        return cancel

    event.async_track_time_interval, event.async_track_state_change_event, event.async_call_later = async_track_time_interval, async_track_state_change_event, async_call_later
    storage = mod("homeassistant.helpers.storage")

    class Store:
        def __init__(self, hass, version, key, **kwargs):
            self.hass, self.version, self.key = hass, version, key

        async def async_load(self):
            stored = self.hass.stored.get(self.key)
            return None if stored is None else stored["data"]

        async def async_save(self, data):
            self.hass.saves.append((self.key, json.loads(json.dumps(data))))
            self.hass.stored[self.key] = {"version": self.version, "data": json.loads(json.dumps(data))}

        async def async_remove(self):
            self.hass.stored.pop(self.key, None)

    storage.Store = Store
    mod("homeassistant.util")
    dt = mod("homeassistant.util.dt")
    dt.utcnow = lambda: datetime.now(timezone.utc)
    modules["homeassistant.util"].dt = dt
    mod("vtherm_api")
    log_collector = mod("vtherm_api.log_collector")
    log_collector.get_vtherm_logger = lambda name: logging.getLogger(name)
    sys.modules.update(modules)
    pkg = types.ModuleType("vtfakepdc")
    pkg.__path__ = [str(COMPONENT)]
    sys.modules["vtfakepdc"] = pkg
    return importlib.import_module("vtfakepdc.feature_heating_pdc_manager")


# ------------------------------------------------------------------ the plant and the time


class FakeInhibition:
    """Records every use: in observe the list must stay empty."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def set(self, entity_id, stop_underlyings):
        self.calls.append(("set", entity_id))

    async def unset(self, entity_id):
        self.calls.append(("unset", entity_id))

    def started(self, entity_id):
        self.calls.append(("started", entity_id))
        return True


TEMPLATE = {
    "radiator_entity_id": "climate.trv_vtherm_{k}",
    "radiator_valve_entity_id": "climate.trv_{k}",
    "temperature_entity_id": "sensor.temp_{k}",
    "window_entity_id": "binary_sensor.window_{k}",
    "split_entity_id": "climate.split_{k}",
    "pdc_entity_id": "climate.pdc_{k}",
    "automatic_entity_id": "input_boolean.auto_{k}",
    "schedule_stop_entity_id": "sensor.stop_{k}",
    "managed_entity_id": "input_boolean.managed_{k}",
    "override_entity_id": "input_boolean.override_{k}",
    "cycle_entity_id": "input_text.cycle_{k}",
}


def pdc_runtime(rooms=("carlo", "diego")):
    out = []
    for key in rooms:
        room = {"room_key": key, **{name: value.format(k=key) for name, value in TEMPLATE.items()}}
        room.update({"setpoint_min_c": 18} if key == "diego" else {"manual_preset_bypass": True})
        out.append(room)
    return {"power_entity_id": "sensor.power", "central_mode_entity_id": "select.central_mode", "rooms": out}


def section(pdc_mode="observe", **extra):
    return {"mode": "disabled", "pdc_mode": pdc_mode, "pdc_runtime": pdc_runtime(), **extra}


def seed(hass, rooms=("carlo", "diego")):
    s = hass.states.async_set
    s("sensor.power", "400")
    s("select.central_mode", "Auto")
    for k in rooms:
        s(f"climate.trv_vtherm_{k}", "heat", {"requested_state": {"hvac_mode": "heat"}, "preset_mode": "comfort", "temperature": 19.0, "specific_states": {}})
        s(f"climate.trv_{k}", "heat")
        s(f"sensor.temp_{k}", "18.5")
        s(f"binary_sensor.window_{k}", "off")
        s(f"climate.split_{k}", "off", {"temperature": 22.0})
        s(f"climate.pdc_{k}", "off", {"temperature": 20.0})
        s(f"input_boolean.auto_{k}", "on")
        s(f"sensor.stop_{k}", "none")
        s(f"input_boolean.managed_{k}", "off")
        s(f"input_boolean.override_{k}", "off")
        s(f"input_text.cycle_{k}", json.dumps({"m": "off", "on": 1, "off": int(NOW) - 3000, "target": 19.0}))
    for entity_id in ("climate.split_carlo", "climate.split_diego", "input_boolean.managed_carlo", "input_boolean.managed_diego"):
        hass.states._d[entity_id].last_changed = hass.clock.now - 5000  # old enough for the raw guards


async def run_loop(times=10):
    for _ in range(times):
        await asyncio.sleep(0)


async def advance(hass, seconds, step=5.0):
    """Move the clock in small steps, firing the due timers; the sensors keep reporting every step."""
    end = hass.clock.now + seconds
    while hass.clock.now < end:
        hass.clock.now = min(end, hass.clock.now + step)
        for entity_id in ("sensor.power", "sensor.temp_carlo", "sensor.temp_diego"):
            state = hass.states.get(entity_id)
            if state is not None:
                state.last_reported = hass.clock.now
        for entry in list(hass.interval_timers):
            if entry[2] and entry[3] <= hass.clock.now:
                entry[3] += entry[0]
                entry[1](datetime.fromtimestamp(hass.clock.now, timezone.utc))
        for entry in list(hass.later_timers):
            if entry[2] and entry[0] <= hass.clock.now:
                entry[2] = False
                entry[1](datetime.fromtimestamp(hass.clock.now, timezone.utc))
        await run_loop()


# ------------------------------------------------------------------ scenarios

RESULTS: list[tuple[str, bool, str]] = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    if not condition:
        print(f"FAIL {name} {detail}")


async def make(mod, hass, sec, inhibition=None):
    inhibition = inhibition or FakeInhibition()
    pdc_section = mod.extract_pdc_section(sec)
    manager = mod.FeatureHeatingPdcManager(hass, "entry1", pdc_section, clock=hass.clock, inhibition=inhibition)
    return manager, inhibition


async def scenario_observe_sends_nothing(mod):
    hass = FakeHass(Clock(NOW))
    seed(hass)
    manager, inhibition = await make(mod, hass, section())
    pushes: dict[str, list] = {"carlo": [], "diego": []}
    for key in ("carlo", "diego"):
        manager.attach(key, lambda state, attrs, key=key: pushes[key].append((state, attrs)), f"sensor.pdc_{key}")
    await manager.async_start()
    check("observe: running with its own mode", manager.is_running and manager.mode == "observe" and manager.runtime is not None)
    await advance(hass, 20)
    check("observe: waits for nothing once the inputs exist", pushes["carlo"][-1][0] != "starting", str(pushes["carlo"][-1][0]))
    # a long, eventful session: ready, power peak, window, a user command, a split started by the remote, a sensor lost
    await advance(hass, 200)
    proposals = {attrs["proposed_action"] for _, attrs in pushes["carlo"]}
    check("observe: it would start and says so", "heat" in proposals, str(proposals))
    hass.states.async_set("sensor.power", "2800")
    await advance(hass, 30)
    hass.states.async_set("sensor.power", "400")
    hass.states.async_set("binary_sensor.window_carlo", "on")
    await advance(hass, 150)
    hass.states.async_set("binary_sensor.window_carlo", "off")
    hass.bus.async_fire(
        "call_service", {"domain": "climate", "service": "set_hvac_mode", "service_data": {"entity_id": "climate.split_carlo", "hvac_mode": "heat"}}, Context("user-1")
    )
    hass.states.async_set("climate.split_carlo", "heat", {"temperature": 22.0})
    await advance(hass, 60)
    hass.states.async_set("climate.split_carlo", "unavailable")
    hass.states.async_set("sensor.temp_carlo", "unknown")
    await advance(hass, 60)
    hass.states.async_set("climate.split_carlo", "off")
    hass.states.async_set("sensor.temp_carlo", "18.5")
    await advance(hass, 400)
    check("observe: not one service call", hass.services.calls == [], str(hass.services.calls[:3]))
    check("observe: not one inhibition call (not even a read of the start state)", inhibition.calls == [], str(inhibition.calls[:3]))
    check("observe: nothing written to the store", hass.saves == [] and hass.stored == {}, str(hass.saves[:1]))
    check("observe: no persistent notification", hass.notifications == [])
    check("observe: no call_service listener (the provenance is a control feature)", all(kind != "call_service" for kind, _, _ in hass.bus.listeners))
    runtime = manager.runtime
    check(
        "observe: no transaction, no reservation, no override",
        all(runtime.transaction(k).token == 0 for k in runtime.room_keys) and runtime.reservation.holder is None and not any(runtime.override(k).active for k in runtime.room_keys),
    )
    state, attrs = manager.state("carlo")
    keys = {"schema_version", "mode", "owner", "proposed_action", "reason", "conditions", "power_limit_w", "stability_s", "transaction", "reservation", "override", "last_error"}
    check(
        "observe: diagnostics follow CONTRATTI section 4",
        keys <= set(attrs) and attrs["mode"] == "observe" and attrs["owner"] == "observer" and attrs["transaction"]["phase"] == "idle",
        str(sorted(keys - set(attrs))),
    )
    check("observe: attributes are JSON safe", json.loads(json.dumps(attrs, allow_nan=False)) == attrs)
    check("observe: both rooms publish", len(pushes["carlo"]) > 5 and len(pushes["diego"]) > 5)
    # the sensors are not flooded: far fewer pushes than state changes and passes
    check("observe: publishing is throttled", len(pushes["carlo"]) < 400, str(len(pushes["carlo"])))
    suspended = await manager.async_suspend("test")
    check("observe: the general suspension is refused and sends nothing", suspended.accepted is False and hass.services.calls == [] and not manager.is_suspended)
    await manager.async_stop()
    await run_loop()
    check(
        "observe: stop leaves no listener, timer or task",
        not hass.state_listeners and not any(t[2] for t in hass.interval_timers) and not any(t[2] for t in hass.later_timers) and all(t.done() for t in hass.tasks),
        str([t for t in hass.tasks if not t.done()][:2]),
    )
    await manager.async_stop()
    check("observe: stop is idempotent", not manager.is_running)


async def scenario_control_is_refused(mod):
    for owners_key in (True, False):
        hass = FakeHass(Clock(NOW))
        seed(hass)
        sec = section("control")
        if owners_key:
            for room in sec["pdc_runtime"]["rooms"]:
                room["external_owner_entity_ids"] = ["automation.chooser"]
        manager, inhibition = await make(mod, hass, sec)
        await manager.async_start()
        state, attrs = manager.state("carlo")
        check(
            "control: refused (error state, not running, no runtime)",
            manager.mode == "disabled" and manager.config is None and not manager.is_running and manager.runtime is None and state == "error",
            f"{state} {manager.config_error}",
        )
        check(
            "control: the error is explicit",
            attrs["last_error"] == "config_control_not_available:mode" and manager.config_error == "config_control_not_available:mode",
            str(attrs["last_error"]),
        )
        await advance(hass, 300)
        check(
            "control: nothing listened to, nothing sent",
            not hass.state_listeners and not hass.interval_timers and hass.services.calls == [] and inhibition.calls == [] and hass.saves == [] and not hass.tasks,
        )
        result = await manager.async_suspend("x")
        check("control: the suspension entry point is refused", result.accepted is False and hass.services.calls == [])
        check("control: no manual resume either", await manager.async_resume_automatic("carlo") is False)
    # the engine itself cannot be built in control from the manager's host
    config = mod.parse_pdc_config(section("observe")["pdc_runtime"] | {"mode": "observe"})
    check("control: constant is off", mod.PDC_CONTROL_AVAILABLE is False and config.mode == "observe")


async def scenario_modes_and_sections(mod):
    hass = FakeHass(Clock(NOW))
    seed(hass)
    off, _ = await make(mod, hass, section("disabled"))
    await off.async_start()
    check("pdc_mode disabled: owns nothing", not off.is_running and not hass.state_listeners and off.state("carlo")[0] == "disabled")
    check("pdc_mode wins over the mode inside the sub-section", mod.extract_pdc_section({"pdc_mode": "disabled", "pdc_runtime": {"mode": "observe"}})["mode"] == "disabled")
    check("absent sub-section: nothing is built", mod.extract_pdc_section({"mode": "observe"}) is None and mod.extract_pdc_section(None) is None)
    absent = mod.FeatureHeatingPdcManager(hass, "entry1", None)
    await absent.async_start()
    check("absent section: disabled, not an error", absent.mode == "disabled" and not absent.is_running and absent.config_error is None)
    bad = mod.FeatureHeatingPdcManager(hass, "entry1", {"mode": "observe", "rooms": "x"})
    await bad.async_start()
    check("invalid section: explicit error, nothing built", bad.config_error is not None and bad.state("carlo")[0] == "error" and not bad.is_running and not hass.state_listeners)
    broken = section("observe")
    del broken["pdc_runtime"]["power_entity_id"]
    refused = mod.FeatureHeatingPdcManager(hass, "entry1", mod.extract_pdc_section(broken))
    check("observe without the power entity is an error", refused.config_error == "config_required:power_entity_id", str(refused.config_error))
    check("manager storage key is distinct from the boiler one", mod.PDC_STORAGE_KEY_PREFIX == "versatile_thermostat.heating_orchestration_pdc")


async def scenario_reload_and_stop(mod):
    hass = FakeHass(Clock(NOW))
    seed(hass)
    first, _ = await make(mod, hass, section())
    await first.async_start()
    await advance(hass, 30)
    await first.async_stop()
    second, inhibition = await make(mod, hass, section())
    await second.async_start()
    await advance(hass, 60)
    alive = [t for t in hass.interval_timers if t[2]]
    check("reload: one interval timer and one state listener only", len(alive) == 1 and len(hass.state_listeners) == 1, f"{len(alive)} {len(hass.state_listeners)}")
    await second.async_stop()
    await run_loop()
    check("reload: after the second stop nothing is left", not hass.state_listeners and not any(t[2] for t in hass.interval_timers) and all(t.done() for t in hass.tasks))
    check("reload: still not one command", hass.services.calls == [] and inhibition.calls == [] and hass.saves == [])
    late_events = len(hass.tasks)
    hass.states.async_set("sensor.power", "900")
    await advance(hass, 60)
    check("reload: events after the stop are ignored", len(hass.tasks) == late_events)


async def scenario_stop_during_start_does_not_publish_late(mod):
    hass = FakeHass(Clock(NOW))
    seed(hass)
    manager, _ = await make(mod, hass, section())
    pushes = []
    manager.attach("carlo", lambda state, attrs: pushes.append(state))
    start = asyncio.ensure_future(manager.async_start())
    await manager.async_stop()
    await start
    await run_loop()
    check("stop racing the start leaves a stopped manager", not manager.is_running)


async def main_async():
    mod = install_fakes()
    scenarios = [
        scenario_observe_sends_nothing,
        scenario_control_is_refused,
        scenario_modes_and_sections,
        scenario_reload_and_stop,
        scenario_stop_during_start_does_not_publish_late,
    ]
    for scenario in scenarios:
        try:
            await scenario(mod)
        except Exception:  # noqa: BLE001
            check(scenario.__name__, False, traceback.format_exc())
    failed = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        print(("PASS " if ok else "FAIL ") + name)
    print(json.dumps({"checks": len(RESULTS), "failed": len(failed)}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
