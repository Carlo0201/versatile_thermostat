"""Tests of the Home Assistant independent PDC engine (runtime W2).

The engine runs against a small simulated plant (entities, VTherm inhibition, PDC VTherm that drives a
split, timers, store) and a controlled clock: nothing sleeps and Home Assistant is never imported. Run
with only pytest installed, from the repository root::

    python -m pytest -q --noconftest --import-mode=importlib tests/test_heating_pdc_runtime.py

The Home Assistant glue (``feature_heating_pdc_manager.py``) is covered by ``tests/test_heating_pdc_manager.py``,
written but not executed (no Home Assistant runtime is used in this workspace).
"""

from __future__ import annotations

import asyncio
import ast
import copy
import importlib
import json
import os
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_DIR = Path(os.environ["PDC_COMPONENT_DIR"]) if os.environ.get("PDC_COMPONENT_DIR") else REPO_ROOT / "custom_components" / "versatile_thermostat"
PKG = "_vtherm_pdc_pure_pkg_under_test"


def load(name: str) -> types.ModuleType:
    if PKG not in sys.modules:
        package = types.ModuleType(PKG)
        package.__path__ = [str(COMPONENT_DIR)]
        sys.modules[PKG] = package
    return importlib.import_module(f"{PKG}.{name}")


c = load("heating_pdc_collector")
rt_mod = load("heating_pdc_runtime")
tr = load("heating_pdc_transaction")
prov = load("heating_pdc_provenance")

T0 = 1_795_500_000.0
CARLO_SPLIT, CARLO_PDC, CARLO_RAD, CARLO_VALVE = "climate.split_carlo", "climate.pdc_carlo", "climate.trv_vtherm_carlo", "climate.trv_carlo"
DIEGO_SPLIT, DIEGO_PDC, DIEGO_RAD, DIEGO_VALVE = "climate.split_diego", "climate.pdc_diego", "climate.trv_vtherm_diego", "climate.trv_diego"

ROOM_TEMPLATE = {
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


def section(mode: str, owners: dict[str, list[str]] | None = None, rooms: tuple[str, ...] = ("carlo", "diego"), **extra: Any) -> dict[str, Any]:
    owners = owners if owners is not None else {"carlo": ["automation.chooser_carlo"], "diego": []}
    out: list[dict[str, Any]] = []
    for key in rooms:
        room = {"room_key": key, **{name: value.format(k=key) for name, value in ROOM_TEMPLATE.items()}}
        room["external_owner_entity_ids"] = owners.get(key, [])
        if key == "diego":
            room["setpoint_min_c"] = 18
        else:
            room["manual_preset_bypass"] = True
        out.append(room)
    return {"mode": mode, "power_entity_id": "sensor.power", "central_mode_entity_id": "select.central_mode", "rooms": out, **extra}


class Plant:
    """Entities, a VTherm inhibition, a PDC VTherm driving a split, timers and a store, on a fake clock."""

    def __init__(
        self, mode: str = "control", *, rooms: tuple[str, ...] = ("carlo", "diego"), owners: dict[str, list[str]] | None = None, stored: dict[str, Any] | None = None
    ) -> None:
        self.clock = T0
        self.states: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.log: list[tuple[str, Any]] = []
        self.published: list[tuple[str, str, dict[str, Any]]] = []
        self.notifications: list[tuple[str, str | None]] = []
        self.timers: list[list[Any]] = []
        self.stored: dict[str, Any] | None = None
        self.saves = 0
        self.transition = 0
        # knobs
        self.split_follows = True
        self.split_obeys = True  # a direct OFF to the split works
        self.fail_calls: set[tuple[str, str]] = set()  # (entity, service) that raise
        self.fail_inhibit = False
        self.fail_unset = 0  # number of unset_inhibition calls that still raise
        self.inhibit_stops_valve = True
        self.save_fail = False
        self.inhibit_hook: Callable[[], None] | None = None
        self.save_hook: Callable[[], None] | None = None
        self.save_hook_phase = "waiting_radiator"
        self.inhibition_started = True
        self.is_ready = True
        self.config = c.parse_pdc_config(section(mode, owners, rooms), allow_control=True)
        self.runtime = rt_mod.PdcRuntime(self.config, self, stored=stored, allow_control=True)
        for key in rooms:
            self._add_room(key)
        self._add("sensor.power", "400")
        self._add("select.central_mode", "Auto")
        self._add("automation.chooser_carlo", "off")
        self.every(10.0, self._report)
        self.every(15.0, lambda: self.runtime.request_evaluation())

    # ---- the world
    def _add(self, entity_id: str, state: str, attributes: dict[str, Any] | None = None, age: float = 5000.0) -> None:
        self.states[entity_id] = {"state": state, "attributes": attributes or {}, "last_changed": self.clock - age, "last_reported": self.clock - 5.0}

    def _add_room(self, k: str) -> None:
        self._add(f"climate.trv_vtherm_{k}", "heat", {"requested_state": {"hvac_mode": "heat"}, "preset_mode": "comfort", "temperature": 19.0, "specific_states": {}})
        self._add(f"climate.trv_{k}", "heat")
        self._add(f"sensor.temp_{k}", "18.5")
        self._add(f"binary_sensor.window_{k}", "off")
        self._add(f"climate.split_{k}", "off", {"temperature": 22.0, "fan_mode": "auto", "preset_mode": "none", "swing_mode": "off"})
        self._add(f"climate.pdc_{k}", "off", {"temperature": 20.0})
        self._add(f"input_boolean.auto_{k}", "on")
        self._add(f"sensor.stop_{k}", "none")
        self._add(f"input_boolean.managed_{k}", "off")
        self._add(f"input_boolean.override_{k}", "off")
        self._add(f"input_text.cycle_{k}", json.dumps({"m": "off", "on": 1795000000, "off": int(self.clock) - 3000, "target": 19.0}))

    def get(self, entity_id: str) -> dict[str, Any] | None:
        return self.states.get(entity_id)

    def set(self, entity_id: str, state: str | None = None, **attributes: Any) -> None:
        old = copy.deepcopy(self.states.get(entity_id))
        entry = self.states.setdefault(entity_id, {"state": "unknown", "attributes": {}, "last_changed": self.clock, "last_reported": self.clock})
        if state is not None and state != entry["state"]:
            entry["state"] = state
            entry["last_changed"] = self.clock
        entry["attributes"] = {**entry["attributes"], **attributes}
        entry["last_reported"] = self.clock
        new = copy.deepcopy(entry)
        if old != new:
            self.runtime.on_state_changed(entity_id, old, new)

    def _report(self) -> None:
        for entity_id in ("sensor.power", "sensor.temp_carlo", "sensor.temp_diego"):
            if entity_id in self.states and self.states[entity_id]["state"] not in ("unknown", "unavailable"):
                self.states[entity_id]["last_reported"] = self.clock

    def inhibited(self, k: str = "carlo") -> bool | None:
        return (self.states[f"climate.trv_vtherm_{k}"]["attributes"].get("specific_states", {}).get("inhibition") or {}).get("inhibited")

    def foreign_inhibit(self, k: str = "carlo") -> None:
        self.transition += 1
        self.set(f"climate.trv_vtherm_{k}", "off", specific_states={"inhibition": {"inhibited": True, "last_transition_at": f"t{self.transition}"}})
        self.set(f"climate.trv_{k}", "off")

    # ---- PdcPorts
    def now(self) -> float:
        return self.clock

    def ready(self) -> bool:
        return self.is_ready

    def states_map(self) -> Any:
        return self

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        entity = data["entity_id"]
        self.calls.append((domain, service, dict(data)))
        self.log.append(("call", (entity, service, data.get("hvac_mode", data.get("temperature")))))
        if (entity, service) in self.fail_calls:
            raise RuntimeError("boom")
        if domain == "climate":
            self.runtime.on_call_service("climate", service, dict(data), None)
        if domain == "input_boolean":
            self.set(entity, "on" if service == "turn_on" else "off")
        elif domain == "climate" and entity.startswith("climate.pdc_"):
            self._pdc_command(entity, service, data)
        elif domain == "climate" and entity.startswith("climate.split_") and self.split_obeys:
            self._split_apply(entity, service, data)

    def _pdc_command(self, entity: str, service: str, data: dict[str, Any]) -> None:
        k = entity.removeprefix("climate.pdc_")
        if service == "set_hvac_mode":
            self.set(entity, data["hvac_mode"])
            if self.split_follows:
                self._split_apply(f"climate.split_{k}", service, {"entity_id": f"climate.split_{k}", "hvac_mode": data["hvac_mode"]}, via_vtherm=True)
        elif service == "set_temperature":
            self.set(entity, temperature=data["temperature"])

    def _split_apply(self, entity: str, service: str, data: dict[str, Any], via_vtherm: bool = False) -> None:
        if via_vtherm:
            self.runtime.on_call_service("climate", service, dict(data), None)  # the VTherm calls the split: the expected effect is registered
        if service == "set_hvac_mode":
            mode = data["hvac_mode"]
            extra = {"preset_mode": "none"} if mode == "off" else {}
            self.set(entity, mode, **extra)
        elif service == "set_temperature":
            self.set(entity, temperature=data["temperature"])

    def user_call(self, entity: str, service: str, **data: Any) -> None:
        """An authenticated user calls a climate service (the event carries a user id)."""
        payload = {"entity_id": entity, **data}
        self.runtime.on_call_service("climate", service, payload, "user-1")
        if entity.startswith("climate.split_"):
            self._split_apply(entity, service, payload)
        else:
            self._pdc_command(entity, service, payload)

    def remote_control(self, k: str, mode: str, **attributes: Any) -> None:
        """The split changes without any Home Assistant call (physical remote): no author, no expected effect."""
        self.set(f"climate.split_{k}", mode, **attributes)

    async def set_inhibition(self, entity_id: str, stop_underlyings: bool) -> None:
        self.log.append(("inhibit", entity_id))
        if self.inhibit_hook is not None:
            hook, self.inhibit_hook = self.inhibit_hook, None
            hook()
        if self.fail_inhibit:
            raise RuntimeError("inhibit failed")
        if self.inhibited(entity_id.removeprefix("climate.trv_vtherm_")):
            return  # idempotent: already inhibited, nothing changes (and the marker stays)
        k = entity_id.removeprefix("climate.trv_vtherm_")
        self.transition += 1
        self.set(entity_id, "off", specific_states={"inhibition": {"inhibited": True, "last_transition_at": f"t{self.transition}"}})
        if self.inhibit_stops_valve and stop_underlyings:
            self.set(f"climate.trv_{k}", "off")

    async def unset_inhibition(self, entity_id: str) -> None:
        self.log.append(("unset", entity_id))
        if self.fail_unset > 0:
            self.fail_unset -= 1
            raise RuntimeError("unset failed")
        k = entity_id.removeprefix("climate.trv_vtherm_")
        self.transition += 1
        self.set(entity_id, "heat", specific_states={"inhibition": {"inhibited": False, "last_transition_at": f"t{self.transition}"}})
        self.set(f"climate.trv_{k}", "heat")

    def inhibition_started_port(self, entity_id: str) -> bool | None:
        self.log.append(("started", entity_id))
        return self.inhibition_started

    async def save(self, data: dict[str, Any]) -> None:
        self.log.append(("save", {k: v["transaction"]["phase"] for k, v in data["rooms"].items()}))
        if self.save_hook is not None and any(v["transaction"]["phase"] == self.save_hook_phase for v in data["rooms"].values()):
            hook, self.save_hook = self.save_hook, None
            hook()
        if self.save_fail:
            raise OSError("disk full")
        self.saves += 1
        self.stored = json.loads(json.dumps(data))

    def call_later(self, delay_s: float, callback: Callable[[], None]) -> Callable[[], None]:
        entry = [self.clock + delay_s, callback, None, True]
        self.timers.append(entry)

        def cancel() -> None:
            entry[3] = False

        return cancel

    def every(self, period: float, callback: Callable[[], None]) -> None:
        def tick() -> None:
            callback()
            self.call_later(period, tick)

        self.call_later(period, tick)

    def publish(self, room_key: str, state: str, attributes: dict[str, Any]) -> None:
        self.published.append((room_key, state, attributes))

    def notify(self, notification_id: str, title: str | None, message: str | None) -> None:
        self.notifications.append((notification_id, title))

    def spawn(self, coro: Any, name: str) -> Any:
        return asyncio.ensure_future(coro)

    # ---- driving
    async def settle(self) -> None:
        for _ in range(30):
            await asyncio.sleep(0)
        await self.runtime.async_drain()
        for _ in range(5):
            await asyncio.sleep(0)

    async def advance(self, seconds: float, step: float = 1.0) -> None:
        end = self.clock + seconds
        while self.clock < end:
            self.clock = min(end, self.clock + step)
            due = [t for t in self.timers if t[3] and t[0] <= self.clock]
            self.timers = [t for t in self.timers if t[3] and t[0] > self.clock]
            for entry in sorted(due, key=lambda t: t[0]):
                entry[1]()
            await self.settle()

    async def start(self) -> None:
        self.runtime.start()
        await self.settle()

    def kinds(self, entity: str | None = None, service: str | None = None) -> list[tuple[str, str, Any]]:
        out = []
        for domain, svc, data in self.calls:
            if (entity is None or data["entity_id"] == entity) and (service is None or svc == service):
                out.append((data["entity_id"], svc, data.get("hvac_mode", data.get("temperature"))))
        return out


# the runtime reads the plant through ``ports.states()``: expose the plant itself as the mapping
Plant.states_method = Plant.states_map


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class _PortsView:
    """Adapter: ``PdcPorts.states()`` is a method while ``Plant.states`` is the dictionary used by the tests."""

    def __init__(self, plant: Plant) -> None:
        self._plant = plant

    def __getattr__(self, name: str) -> Any:
        return getattr(self._plant, name)

    def states(self) -> Any:
        return self._plant

    def inhibition_started(self, entity_id: str) -> bool | None:
        return self._plant.inhibition_started_port(entity_id)


def make(mode: str = "control", **kwargs: Any) -> tuple[Plant, Any]:
    stored = kwargs.get("stored")
    plant = Plant(mode, rooms=kwargs.get("rooms", ("carlo",)), owners=kwargs.get("owners"), stored=None)
    view = _PortsView(plant)
    plant.runtime = rt_mod.PdcRuntime(plant.config, view, stored=stored, allow_control=True)
    return plant, plant.runtime


def calls_of(plant: Plant, entity: str, service: str | None = None) -> list[Any]:
    return plant.kinds(entity, service)


async def run_until_running(plant: Plant, limit: float = 400.0) -> None:
    await plant.start()
    waited = 0.0
    while plant.runtime.transaction("carlo").phase != tr.PHASE_RUNNING and waited < limit:
        await plant.advance(10.0)
        waited += 10.0
    assert plant.runtime.transaction("carlo").phase == tr.PHASE_RUNNING, (plant.runtime.transaction("carlo"), plant.log[-8:])


# --- the happy path -----------------------------------------------------------------------------


def test_a_start_is_sequenced_persisted_before_the_inhibition_and_ends_with_heat_confirmed():
    async def scenario() -> None:
        plant, rt = make()
        await run_until_running(plant)
        tx = rt.transaction("carlo")
        assert tx.inhibition_owned and tx.reservation_held and tx.inhibited_before is False and rt.reservation == tr.PdcReservation("carlo", tx.token)
        # write-ahead: the ownership claim was in the store before the first inhibition call
        first_inhibit = plant.log.index(("inhibit", CARLO_RAD))
        claims = [entry for entry in plant.log[:first_inhibit] if entry[0] == "save"]
        assert claims and claims[-1][1]["carlo"] == tr.PHASE_WAITING_RADIATOR
        assert plant.stored["rooms"]["carlo"]["transaction"]["inhibition_owned"] is True
        # command order: inhibit, setpoint (already 20), HEAT; the HEAT came after the radiator stop was observed
        names = [e[0] if e[0] != "call" else e[1][1] + ":" + e[1][0] for e in plant.log if e[0] in ("inhibit", "call")]
        assert names.index("inhibit") < names.index("set_hvac_mode:" + CARLO_PDC)
        assert plant.states[CARLO_VALVE]["state"] == "off" and plant.states[CARLO_SPLIT]["state"] == "heat" and plant.states[CARLO_PDC]["state"] == "heat"
        assert plant.states["input_boolean.managed_carlo"]["state"] == "on"  # compatibility mirror of the legacy helper
        state, attrs = rt.attributes("carlo")
        assert state == "running" and attrs["owner"] == "controller" and attrs["transaction"]["inhibition_owned"] is True and attrs["managed"] is True
        # the cycle registered the confirmed HEAT
        assert plant.stored["rooms"]["carlo"]["cycle"]["m"] == "heat"

    run(scenario())


def test_a_normal_stop_returns_the_radiator_only_after_the_split_and_the_pdc_are_off():
    async def scenario() -> None:
        plant, rt = make()
        await run_until_running(plant)
        plant.set("sensor.temp_carlo", "19.6")  # target + 0.5: early stop
        await plant.settle()
        await plant.advance(5.0)
        tx = rt.transaction("carlo")
        assert tx.phase == tr.PHASE_IDLE and not tx.inhibition_owned and rt.reservation == tr.PdcReservation()
        order = [e for e in plant.log if e[0] in ("call", "unset")]
        pdc_off = next(i for i, e in enumerate(order) if e == ("call", (CARLO_PDC, "set_hvac_mode", "off")))
        unset = next(i for i, e in enumerate(order) if e[0] == "unset")
        assert pdc_off < unset
        assert plant.states[CARLO_SPLIT]["state"] == "off" and plant.states[CARLO_PDC]["state"] == "off" and plant.states[CARLO_VALVE]["state"] == "heat"
        assert plant.inhibited() is False and plant.states["input_boolean.managed_carlo"]["state"] == "off"
        cycle = plant.stored["rooms"]["carlo"]["cycle"]
        assert cycle["m"] == "off" and cycle["off"] >= cycle["on"]

    run(scenario())


# --- inhibition ownership -----------------------------------------------------------------------


def test_an_inhibition_already_present_is_never_acquired_nor_released():
    async def scenario() -> None:
        plant, rt = make()
        plant.foreign_inhibit()  # somebody else inhibits the radiator before the PDC wants it
        await plant.start()
        await plant.advance(600.0)
        assert rt.transaction("carlo").phase == tr.PHASE_IDLE and rt.transaction("carlo").token == 0
        assert ("inhibit", CARLO_RAD) not in plant.log and not any(e[0] == "unset" for e in plant.log)
        assert plant.inhibited() is True  # untouched
        assert calls_of(plant, CARLO_PDC, "set_hvac_mode") == []

    run(scenario())


def test_a_foreign_inhibition_that_appears_between_the_request_and_the_call_abandons_the_start_without_releasing_it():
    async def scenario() -> None:
        plant, rt = make()
        plant.save_hook = plant.foreign_inhibit  # runs during the write-ahead save: the claim was made while it was not inhibited
        await plant.start()
        await plant.advance(200.0)
        tx = rt.transaction("carlo")
        assert tx.phase == tr.PHASE_IDLE and tx.token >= 1 and not tx.inhibition_owned and tx.failure == tr.FAILURE_INHIBITION_NOT_OWNED
        assert not any(e[0] == "unset" for e in plant.log), "a foreign inhibition must never be released"
        assert plant.inhibited() is True and rt.reservation == tr.PdcReservation()
        assert calls_of(plant, CARLO_PDC, "set_hvac_mode") == [] and plant.states[CARLO_SPLIT]["state"] == "off"
        assert ("inhibit", CARLO_RAD) not in plant.log, "the call that would be a no-op looking like ours is not even made"

    run(scenario())


def test_a_foreign_inhibition_without_a_transition_marker_is_still_not_ours():
    async def scenario() -> None:
        plant, rt = make()

        def foreign_without_marker() -> None:
            plant.set(CARLO_RAD, "off", specific_states={"inhibition": {"inhibited": True}})
            plant.set(CARLO_VALVE, "off")

        plant.save_hook = foreign_without_marker
        await plant.start()
        await plant.advance(200.0)
        tx = rt.transaction("carlo")
        assert tx.phase == tr.PHASE_IDLE and not tx.inhibition_owned and tx.failure == tr.FAILURE_INHIBITION_NOT_OWNED
        assert not any(e[0] == "unset" for e in plant.log) and plant.inhibited() is True

    run(scenario())


def test_the_ownership_is_lost_between_the_decision_and_the_command_nothing_is_sent():
    async def scenario() -> None:
        plant, rt = make()
        plant.save_hook = lambda: plant.set("automation.chooser_carlo", "on")  # re-enabled during the write-ahead save
        await plant.start()
        await plant.advance(130.0)
        assert ("inhibit", CARLO_RAD) not in plant.log and calls_of(plant, CARLO_PDC) == []
        assert rt.transaction("carlo").phase == tr.PHASE_WAITING_RADIATOR and rt.attributes("carlo")[0] == "blocked"

    run(scenario())


def test_the_engine_never_releases_an_inhibition_the_machine_does_not_own_even_if_told_to():
    async def scenario() -> None:
        plant, rt = make()
        rt.start()
        await plant.settle()
        room = rt._rooms["carlo"]
        room.recovered = True
        plant.foreign_inhibit()
        room.tx = tr.PdcTransaction("carlo", token=2, phase=tr.PHASE_RELEASING, inhibition_owned=False, deadline=plant.clock + 30)
        await rt._execute(room, (tr.PdcCommand(tr.CMD_RELEASE_INHIBITION, 2, "carlo"),))
        assert not any(e[0] == "unset" for e in plant.log) and plant.inhibited() is True

    run(scenario())


def test_the_release_is_dropped_if_the_pdc_comes_back_while_the_state_is_being_saved():
    async def scenario() -> None:
        plant, rt = make()
        await run_until_running(plant)
        plant.save_hook_phase = "releasing"

        def pdc_back() -> None:
            plant.set(CARLO_PDC, "heat")
            plant.set(CARLO_SPLIT, "heat")

        plant.save_hook = pdc_back
        plant.set("sensor.temp_carlo", "19.6")
        await plant.settle()
        await plant.advance(3.0)
        assert not any(e[0] == "unset" for e in plant.log) and plant.inhibited() is True
        assert rt.transaction("carlo").inhibition_owned and rt.transaction("carlo").phase != tr.PHASE_IDLE

    run(scenario())


def test_every_phase_is_in_the_store_before_the_command_it_leads_to():
    async def scenario() -> None:
        plant, rt = make()
        await run_until_running(plant)
        plant.set("sensor.temp_carlo", "19.6")
        await plant.settle()
        await plant.advance(5.0)

        def saved_before(call: tuple, phase: str) -> bool:
            at = plant.log.index(call)
            return any(e[0] == "save" and e[1]["carlo"] == phase for e in plant.log[:at])

        assert saved_before(("call", (CARLO_PDC, "set_temperature", 20.0)), tr.PHASE_WAITING_TARGET)
        assert saved_before(("call", (CARLO_PDC, "set_hvac_mode", "heat")), tr.PHASE_WAITING_HEAT)
        assert saved_before(("call", (CARLO_PDC, "set_hvac_mode", "off")), tr.PHASE_RETURNING)
        first_unset = next(i for i, e in enumerate(plant.log) if e[0] == "unset")
        assert any(e[0] == "save" and e[1]["carlo"] == tr.PHASE_RELEASING for e in plant.log[:first_unset])

    run(scenario())


def test_a_hook_that_inhibits_before_the_machine_asks_does_not_create_ownership_either():
    async def scenario() -> None:
        plant, rt = make()
        await plant.start()
        # the policy sees heat; the VTherm reports the inhibition as already placed at the very moment of the request
        plant.states[CARLO_RAD]["attributes"]["specific_states"] = {"inhibition": {"inhibited": True, "last_transition_at": "t0"}}
        await plant.advance(300.0)
        assert not any(e[0] == "inhibit" for e in plant.log) and rt.transaction("carlo").token == 0

    run(scenario())


def test_a_failed_inhibition_call_leaves_nothing_to_release():
    async def scenario() -> None:
        plant, rt = make()
        plant.fail_inhibit = True
        await plant.start()
        await plant.advance(200.0)
        tx = rt.transaction("carlo")
        assert tx.phase == tr.PHASE_IDLE and tx.failure == tr.FAILURE_INHIBITION_FAILED and not tx.inhibition_owned
        assert not any(e[0] == "unset" for e in plant.log) and calls_of(plant, CARLO_PDC, "set_hvac_mode") == []
        assert rt.reservation == tr.PdcReservation()

    run(scenario())


def test_the_runtime_does_not_start_while_the_vtherm_inhibition_is_not_started():
    async def scenario() -> None:
        plant, rt = make()
        plant.inhibition_started = False
        await plant.start()
        await plant.advance(300.0)
        assert rt.transaction("carlo").token == 0 and not any(e[0] == "inhibit" for e in plant.log)
        assert rt.attributes("carlo")[1]["last_error"] == "inhibition_not_started"

    run(scenario())


def test_without_a_durable_claim_there_is_no_session():
    async def scenario() -> None:
        plant, rt = make()
        plant.save_fail = True
        await plant.start()
        await plant.advance(300.0)
        assert rt.transaction("carlo").token == 0 and not any(e[0] == "inhibit" for e in plant.log)
        assert rt.reservation == tr.PdcReservation() and rt.attributes("carlo")[1]["last_error"].startswith("store_save_failed")

    run(scenario())


# --- strict confirmations ----------------------------------------------------------------------


def test_the_pdc_is_never_started_without_a_confirmed_radiator_stop():
    async def scenario() -> None:
        plant, rt = make()
        plant.inhibit_stops_valve = False  # the inhibition is placed but the valve never closes
        await plant.start()
        await plant.advance(200.0)
        assert calls_of(plant, CARLO_PDC, "set_hvac_mode") == []  # no HEAT; the PDC was already off, so not even an OFF was needed
        assert (CARLO_PDC, "set_hvac_mode", "heat") not in plant.kinds()
        tx = rt.transaction("carlo")
        assert tx.phase == tr.PHASE_IDLE and tx.failure == tr.FAILURE_TRV_UNCONFIRMED
        # the return happened (PDC OFF confirmed) and only then was the owned inhibition released
        assert any(e[0] == "unset" for e in plant.log) and plant.inhibited() is False

    run(scenario())


def test_the_radiator_is_never_released_while_the_split_does_not_confirm_off():
    async def scenario() -> None:
        plant, rt = make()
        await run_until_running(plant)
        plant.split_obeys = False  # the split ignores the direct OFF
        plant.split_follows = False  # and the PDC VTherm does not drive it either
        plant.set("sensor.temp_carlo", "19.6")
        await plant.settle()
        await plant.advance(200.0)
        tx = rt.transaction("carlo")
        assert tx.phase == tr.PHASE_RETURN_UNCONFIRMED and tx.inhibition_owned
        assert not any(e[0] == "unset" for e in plant.log) and plant.inhibited() is True and plant.states[CARLO_VALVE]["state"] == "off"
        assert ("climate", "set_hvac_mode") in {(d, s) for d, s, _ in plant.calls}
        assert plant.notifications[-1][0] == "carlo_pdc_return_unconfirmed" and plant.notifications[-1][1] is not None
        assert (CARLO_SPLIT, "set_hvac_mode", "off") in plant.kinds()  # the direct fallback was tried
        # the retry sends OFF again; a late confirmation completes the return and only then releases
        before = len(plant.kinds(CARLO_PDC, "set_hvac_mode"))
        await plant.advance(130.0)
        assert len(plant.kinds(CARLO_PDC, "set_hvac_mode")) > before and not any(e[0] == "unset" for e in plant.log)
        plant.set(CARLO_SPLIT, "off")
        plant.set(CARLO_PDC, "off")
        await plant.settle()
        await plant.advance(35.0)
        assert rt.transaction("carlo").phase == tr.PHASE_IDLE and any(e[0] == "unset" for e in plant.log) and plant.inhibited() is False
        assert plant.notifications[-1] == ("carlo_pdc_return_unconfirmed", None)

    run(scenario())


def test_a_split_that_disconnects_while_running_never_gives_the_radiator_back_until_it_confirms_off():
    async def scenario() -> None:
        plant, rt = make()
        await run_until_running(plant)
        plant.split_obeys = False
        plant.split_follows = False
        plant.set(CARLO_SPLIT, "unavailable")  # SmartThings drops the split
        await plant.settle()
        await plant.advance(300.0, step=5.0)
        tx = rt.transaction("carlo")
        assert tx.phase in (tr.PHASE_RETURNING, tr.PHASE_RETURNING_DIRECT, tr.PHASE_RETURN_UNCONFIRMED) and tx.inhibition_owned
        assert not any(e[0] == "unset" for e in plant.log) and plant.inhibited() is True and plant.states[CARLO_VALVE]["state"] == "off"
        # the unknown state is not an OFF, it is not a reason for an on/off cycle either: the PDC OFF is repeated, nothing is started
        assert (CARLO_PDC, "set_hvac_mode", "heat") not in plant.kinds()[plant.kinds().index((CARLO_PDC, "set_hvac_mode", "off")) :]
        plant.set(CARLO_SPLIT, "off")  # it comes back, off
        plant.set(CARLO_PDC, "off")
        await plant.settle()
        await plant.advance(130.0)
        assert rt.transaction("carlo").phase == tr.PHASE_IDLE and plant.inhibited() is False

    run(scenario())


def test_a_failed_release_keeps_the_ownership_and_is_retried():
    async def scenario() -> None:
        plant, rt = make()
        await run_until_running(plant)
        plant.fail_unset = 2
        plant.set("sensor.temp_carlo", "19.6")
        await plant.settle()
        await plant.advance(5.0)
        assert rt.transaction("carlo").phase == tr.PHASE_RELEASING and rt.transaction("carlo").inhibition_owned and rt.reservation.holder == "carlo"
        assert rt.attributes("carlo")[1]["transaction"]["release_attempts"] >= 1 and rt.attributes("carlo")[1]["managed"] is True
        await plant.advance(100.0)
        tx = rt.transaction("carlo")
        assert tx.phase == tr.PHASE_IDLE and plant.inhibited() is False and len([e for e in plant.log if e[0] == "unset"]) == 3 and rt.reservation == tr.PdcReservation()

    run(scenario())


def test_the_release_is_re_validated_against_a_fresh_read():
    async def scenario() -> None:
        plant, rt = make()
        await run_until_running(plant)
        plant.set("sensor.temp_carlo", "19.6")
        await plant.settle()
        # ... but the release call failed: the machine waits in ``releasing`` and the split is started again meanwhile
        plant.fail_unset = 1
        await plant.advance(2.0)
        assert rt.transaction("carlo").phase == tr.PHASE_RELEASING
        unsets = len([e for e in plant.log if e[0] == "unset"])
        plant.set(CARLO_PDC, "heat")
        plant.set(CARLO_SPLIT, "heat")  # somebody starts the split again while the release is pending (no call: a remote)
        await plant.settle()
        await plant.advance(120.0)
        assert len([e for e in plant.log if e[0] == "unset"]) == unsets, "the radiator must not be released while the PDC is on"
        assert plant.inhibited() is True and rt.transaction("carlo").inhibition_owned
        assert rt.override("carlo").active and rt.transaction("carlo").phase == tr.PHASE_OVERRIDE_HOLD

    run(scenario())


def test_protections_interrupt_a_start_in_progress():
    async def scenario() -> None:
        plant, rt = make()
        plant.inhibit_stops_valve = False  # the radiator never confirms its stop: the sequence stays in the first wait
        await plant.start()
        for _ in range(200):
            await plant.advance(1.0)
            if rt.transaction("carlo").phase == tr.PHASE_WAITING_RADIATOR:
                break
        assert rt.transaction("carlo").phase == tr.PHASE_WAITING_RADIATOR
        plant.set("binary_sensor.window_carlo", "on")  # a protection arrives during the wait (the baseline would drop it)
        await plant.settle()
        await plant.advance(2.0)
        tx = rt.transaction("carlo")
        assert tx.phase in (tr.PHASE_IDLE, tr.PHASE_RETURNING, tr.PHASE_RELEASING) and tx.failure == tr.FAILURE_CONDITIONS_CHANGED
        assert (CARLO_PDC, "set_hvac_mode", "heat") not in plant.kinds()  # the PDC was never started
        await plant.advance(30.0)
        assert rt.transaction("carlo").phase == tr.PHASE_IDLE and plant.inhibited() is False and rt.reservation == tr.PdcReservation()

    run(scenario())


def test_a_power_peak_during_the_hand_over_returns_to_the_radiator_without_heat():
    async def scenario() -> None:
        plant, rt = make()
        plant.split_follows = False  # the HEAT never reaches the split: the sequence stays in its waits
        await plant.start()
        for _ in range(200):
            await plant.advance(1.0)
            if rt.transaction("carlo").phase == tr.PHASE_WAITING_HEAT:
                break
        assert rt.transaction("carlo").phase == tr.PHASE_WAITING_HEAT
        plant.set("sensor.power", "2800")
        await plant.settle()
        await plant.advance(3.0)
        tx = rt.transaction("carlo")
        assert tx.phase in (tr.PHASE_RETURNING, tr.PHASE_RELEASING, tr.PHASE_IDLE) and tx.failure == tr.FAILURE_CONDITIONS_CHANGED
        assert (CARLO_PDC, "set_hvac_mode", "off") in plant.kinds()

    run(scenario())


def test_heat_is_revalidated_with_a_fresh_read_right_before_it_is_sent():
    async def scenario() -> None:
        plant, rt = make()
        room = rt._rooms["carlo"]
        room.recovered = True
        # the machine just decided HEAT, but the radiator valve is reopened exactly at that moment (a stale decision)
        plant.foreign_inhibit()
        plant.set(CARLO_VALVE, "heat")
        now = plant.clock
        room.tx = tr.PdcTransaction(
            "carlo",
            token=3,
            phase=tr.PHASE_WAITING_HEAT,
            started_at=now - 5,
            phase_started_at=now,
            deadline=now + 20,
            inhibition_owned=True,
            inhibited_before=False,
            reservation_held=True,
            heat_sent_at=now,
        )
        rt._reservation = tr.PdcReservation("carlo", 3)
        await rt._execute(room, (tr.PdcCommand(tr.CMD_PDC_HEAT, 3, "carlo"),))
        assert (CARLO_PDC, "set_hvac_mode", "heat") not in plant.kinds()
        assert room.last_error == "heat_revalidation_failed" and room.tx.phase in (tr.PHASE_RETURNING, tr.PHASE_RELEASING, tr.PHASE_IDLE)
        assert room.tx.failure == tr.FAILURE_CONDITIONS_CHANGED
        # with everything confirmed the same command goes out
        plant2, rt2 = make()
        room2 = rt2._rooms["carlo"]
        room2.recovered = True
        await plant2.set_inhibition(CARLO_RAD, True)
        room2.tx = tr.PdcTransaction(
            "carlo",
            token=3,
            phase=tr.PHASE_WAITING_HEAT,
            started_at=plant2.clock - 5,
            phase_started_at=plant2.clock,
            deadline=plant2.clock + 20,
            inhibition_owned=True,
            inhibited_before=False,
            reservation_held=True,
            heat_sent_at=plant2.clock,
        )
        rt2._reservation = tr.PdcReservation("carlo", 3)
        await rt2._execute(room2, (tr.PdcCommand(tr.CMD_PDC_HEAT, 3, "carlo"),))
        assert (CARLO_PDC, "set_hvac_mode", "heat") in plant2.kinds()

    run(scenario())


def test_a_command_of_an_old_token_is_never_executed():
    async def scenario() -> None:
        plant, rt = make()
        await run_until_running(plant)
        room = rt._rooms["carlo"]
        n = len(plant.calls)
        await rt._execute(
            room, (tr.PdcCommand(tr.CMD_PDC_OFF, room.tx.token - 1, "carlo"), tr.PdcCommand(tr.CMD_RELEASE_INHIBITION, 0, "carlo"), tr.PdcCommand(tr.CMD_PDC_HEAT, 99, "carlo"))
        )
        assert len(plant.calls) == n and not any(e[0] == "unset" for e in plant.log)

    run(scenario())


def test_the_radiator_released_by_somebody_else_while_running_stops_the_pdc():
    async def scenario() -> None:
        plant, rt = make()
        await run_until_running(plant)
        plant.set(CARLO_RAD, "heat", specific_states={"inhibition": {"inhibited": False, "last_transition_at": "tx"}})
        await plant.settle()
        await plant.advance(5.0)
        tx = rt.transaction("carlo")
        assert tx.phase == tr.PHASE_IDLE and tx.failure == tr.FAILURE_RADIATOR_RELEASED and not tx.inhibition_owned
        assert plant.states[CARLO_SPLIT]["state"] == "off" and (CARLO_PDC, "set_hvac_mode", "off") in plant.kinds()
        assert rt.reservation == tr.PdcReservation()

    run(scenario())


# --- two rooms, one PDC ------------------------------------------------------------------------


def test_only_one_pdc_runs_at_a_time_across_the_rooms():
    async def scenario() -> None:
        plant, rt = make(rooms=("carlo", "diego"))
        await plant.start()
        await plant.advance(300.0)
        phases = {key: rt.transaction(key).phase for key in ("carlo", "diego")}
        busy = [k for k, p in phases.items() if p != tr.PHASE_IDLE]
        assert len(busy) == 1, phases
        assert sum(1 for e in plant.log if e[0] == "inhibit") == 1
        other = "diego" if busy[0] == "carlo" else "carlo"
        assert rt.attributes(other)[1]["reason"] in ("not_ready", "managed_unknown", "stability_wait", "edge_consumed", "start_blocked", "start", "running")

    run(scenario())


# --- observe: nothing is sent --------------------------------------------------------------------


def test_observe_sends_no_command_creates_no_transaction_and_persists_nothing():
    async def scenario() -> None:
        plant, rt = make("observe")
        await plant.start()
        await plant.advance(400.0)
        assert plant.calls == [] and not any(e[0] in ("inhibit", "unset", "save", "started") for e in plant.log) and plant.stored is None
        assert rt.transaction("carlo").token == 0 and rt.reservation == tr.PdcReservation()
        state, attrs = rt.attributes("carlo")
        assert attrs["mode"] == "observe" and attrs["owner"] == "observer" and attrs["proposed_action"] in ("none", "heat") and attrs["transaction"]["phase"] == "idle"
        assert any(attrs["proposed_action"] == "heat" for _, _, attrs in plant.published)  # it would start, and says so
        # observe also ignores everything that could start a session
        plant.user_call(CARLO_SPLIT, "set_hvac_mode", hvac_mode="heat")
        await plant.advance(50.0)
        assert plant.calls == [] and rt.override("carlo") == prov.OverrideState()
        assert (await rt.async_suspend("x")).accepted is False

    run(scenario())


def test_the_control_engine_cannot_be_built_unless_the_host_opts_in():
    config = c.parse_pdc_config(section("control"), allow_control=True)
    plant = Plant("observe", rooms=("carlo",))
    with pytest.raises(rt_mod.PdcControlUnavailableError):
        rt_mod.PdcRuntime(config, _PortsView(plant))
    with pytest.raises(rt_mod.PdcControlUnavailableError):
        rt_mod.PdcRuntime(config, _PortsView(plant), stored={"schema_version": 1}, allow_control=False)
    assert rt_mod.PdcRuntime(plant.config, _PortsView(plant)).mode == "observe"  # observe needs no opt-in
    with pytest.raises(c.PdcConfigError):
        c.parse_pdc_config(section("control"))


def test_disabled_does_nothing_at_all():
    async def scenario() -> None:
        plant, rt = make("disabled")
        rt.start()
        await plant.advance(60.0)
        assert plant.calls == [] and plant.published == [] and not plant.log

    run(scenario())


def test_the_first_pass_waits_for_the_inputs_and_for_home_assistant():
    async def scenario() -> None:
        plant, rt = make("observe")
        del plant.states["climate.split_carlo"]
        await plant.start()
        state, attrs = rt.attributes("carlo")
        assert state == "starting" and attrs["last_error"].startswith("waiting_for_entities:")
        plant._add("climate.split_carlo", "off")
        plant.is_ready = False
        await plant.advance(20.0)
        assert rt.attributes("carlo")[0] == "starting" and rt.attributes("carlo")[1]["last_error"] == "waiting_for_home_assistant"
        plant.is_ready = True
        await plant.advance(20.0)
        assert rt.attributes("carlo")[0] != "starting"

    run(scenario())


# --- the interlock -----------------------------------------------------------------------------


def test_control_is_blocked_while_an_external_owner_is_not_confirmed_off():
    async def scenario() -> None:
        for owner_state in ("on", "unavailable", "unknown"):
            plant, rt = make(rooms=("carlo", "diego"))
            plant.set("automation.chooser_carlo", owner_state)
            await plant.start()
            await plant.advance(400.0)
            assert not [call for call in plant.calls if "carlo" in call[2]["entity_id"]] and ("inhibit", CARLO_RAD) not in plant.log, owner_state
            state, attrs = rt.attributes("carlo")
            assert state == "blocked" and attrs["owner"] is None and attrs["blocked_by"] == ["automation.chooser_carlo"] and attrs["last_error"].startswith("external_owner_active")
            # the other room has no owner declared and is not affected
            assert rt.attributes("diego")[0] != "blocked"
            assert (await rt.async_suspend("x")).reason == rt_mod.REFUSED_BLOCKED
        plant, rt = make()
        del plant.states["automation.chooser_carlo"]  # a missing owner is not a confirmed OFF either
        await plant.start()
        await plant.advance(60.0)
        assert rt.attributes("carlo")[0] == "blocked"

    run(scenario())


def test_losing_the_ownership_during_a_transaction_freezes_it_without_compensation():
    async def scenario() -> None:
        plant, rt = make()
        plant.split_follows = False
        await plant.start()
        for _ in range(200):
            await plant.advance(1.0)
            if rt.transaction("carlo").phase == tr.PHASE_WAITING_HEAT:
                break
        assert rt.transaction("carlo").phase == tr.PHASE_WAITING_HEAT
        n = len(plant.calls)
        plant.set("automation.chooser_carlo", "on")  # somebody re-enabled the legacy chooser
        await plant.advance(120.0)
        assert len(plant.calls) == n and not any(e[0] == "unset" for e in plant.log)
        assert rt.transaction("carlo").phase == tr.PHASE_WAITING_HEAT and plant.inhibited() is True
        assert rt.attributes("carlo")[0] == "blocked"
        # when the owner is gone again the runtime resumes through the safe path (no stale generation completes)
        plant.set("automation.chooser_carlo", "off")
        await plant.advance(120.0)
        assert rt.transaction("carlo").phase == tr.PHASE_IDLE and plant.inhibited() is False and plant.states[CARLO_SPLIT]["state"] == "off"

    run(scenario())


# --- restart -----------------------------------------------------------------------------------


def test_a_restart_in_the_middle_of_a_hand_over_returns_through_the_safe_path_and_releases_the_owned_inhibition():
    async def scenario() -> None:
        plant, rt = make()
        plant.split_follows = False
        await plant.start()
        for _ in range(200):
            await plant.advance(1.0)
            if rt.transaction("carlo").phase == tr.PHASE_WAITING_HEAT:
                break
        stored = copy.deepcopy(plant.stored)
        assert stored["rooms"]["carlo"]["transaction"]["inhibition_owned"] is True
        old_token = rt.transaction("carlo").token
        await rt.async_stop()
        # a new process: same plant, the runtime is rebuilt from the store
        plant2, _ = make()
        plant2.states = plant.states
        plant2.clock = plant.clock + 30
        plant2.runtime = rt_mod.PdcRuntime(plant2.config, _PortsView(plant2), stored=stored, allow_control=True)
        await plant2.start()
        await plant2.advance(120.0)
        tx = plant2.runtime.transaction("carlo")
        assert tx.token > old_token and tx.phase == tr.PHASE_IDLE and plant2.inhibited() is False
        assert any(e[0] == "unset" for e in plant2.log) and plant2.states[CARLO_SPLIT]["state"] == "off"
        assert (CARLO_PDC, "set_hvac_mode", "heat") not in plant2.kinds()

    run(scenario())


def test_a_restart_with_a_running_pdc_keeps_it_and_keeps_the_ownership():
    async def scenario() -> None:
        plant, rt = make()
        await run_until_running(plant)
        stored = copy.deepcopy(plant.stored)
        await rt.async_stop()
        plant2, _ = make()
        plant2.states = plant.states
        plant2.clock = plant.clock + 20
        plant2.runtime = rt_mod.PdcRuntime(plant2.config, _PortsView(plant2), stored=stored, allow_control=True)
        await plant2.start()
        await plant2.advance(60.0)
        tx = plant2.runtime.transaction("carlo")
        assert tx.phase == tr.PHASE_RUNNING and tx.inhibition_owned and tx.token == 1
        assert [call for call in plant2.calls if call[2]["entity_id"] != "input_boolean.managed_carlo"] == []  # nothing is sent: the PDC keeps running

    run(scenario())


def test_recovery_waits_for_readable_devices_then_goes_on_even_if_they_never_come_back():
    async def scenario() -> None:
        plant, rt = make()
        plant.split_follows = False
        await plant.start()
        for _ in range(200):
            await plant.advance(1.0)
            if rt.transaction("carlo").phase == tr.PHASE_WAITING_HEAT:
                break
        stored = copy.deepcopy(plant.stored)
        await rt.async_stop()
        plant2, _ = make()
        plant2.states = copy.deepcopy(plant.states)
        plant2.clock = plant.clock + 30
        plant2.set(CARLO_SPLIT, "unavailable")
        plant2.set(CARLO_PDC, "unavailable")
        plant2.runtime = rt_mod.PdcRuntime(plant2.config, _PortsView(plant2), stored=stored, allow_control=True)
        await plant2.start()
        await plant2.advance(60.0)
        assert plant2.calls == [] and plant2.runtime.transaction("carlo").token == stored["rooms"]["carlo"]["transaction"]["token"]
        assert plant2.runtime.attributes("carlo")[1]["transaction"]["phase"] == "waiting_heat"  # not recovered yet, nothing released
        plant2.set(CARLO_SPLIT, "off")
        plant2.set(CARLO_PDC, "off")
        await plant2.advance(30.0)
        assert plant2.runtime.transaction("carlo").phase == tr.PHASE_IDLE and plant2.inhibited() is False
        # a device that never comes back: after the bounded wait the return is attempted anyway (and stays unconfirmed, never released)
        plant3, _ = make()
        plant3.states = copy.deepcopy(plant.states)
        plant3.clock = plant.clock + 30
        plant3.set(CARLO_SPLIT, "unavailable")
        plant3.set(CARLO_PDC, "unavailable")
        plant3.fail_calls.add((CARLO_PDC, "set_hvac_mode"))
        plant3.split_obeys = False
        plant3.split_follows = False
        plant3.runtime = rt_mod.PdcRuntime(plant3.config, _PortsView(plant3), stored=stored, allow_control=True)
        await plant3.start()
        await plant3.advance(rt_mod.RECOVERY_WAIT_S + 60.0)
        assert plant3.runtime.transaction("carlo").phase in (tr.PHASE_RETURNING, tr.PHASE_RETURNING_DIRECT, tr.PHASE_RETURN_UNCONFIRMED)
        assert plant3.runtime.transaction("carlo").inhibition_owned and not any(e[0] == "unset" for e in plant3.log)

    run(scenario())


def test_an_orphan_reservation_is_dropped_and_a_live_one_is_kept():
    async def scenario() -> None:
        base = {"schema_version": 1, "rooms": {}, "suspension": {"active": False}}
        _, orphan = make(rooms=("carlo", "diego"), stored={**base, "reservation": {"holder": "diego", "token": 4}})
        assert orphan.reservation == tr.PdcReservation()
        plant, rt = make(rooms=("carlo", "diego"))
        await plant.start()
        await plant.advance(300.0)
        stored = copy.deepcopy(plant.stored)
        busy = stored["reservation"]["holder"]
        _, kept = make(rooms=("carlo", "diego"), stored=stored)
        assert kept.reservation == tr.PdcReservation(busy, stored["reservation"]["token"])
        other = "diego" if busy == "carlo" else "carlo"
        stale = copy.deepcopy(stored)
        stale["reservation"] = {"holder": other, "token": 1}
        _, dropped = make(rooms=("carlo", "diego"), stored=stale)
        assert dropped.reservation == tr.PdcReservation()

    run(scenario())


def test_the_diagnostics_describe_the_hand_over_in_flight_not_the_decision_that_started_it():
    async def scenario() -> None:
        plant, rt = make()
        plant.split_follows = False
        await plant.start()
        for _ in range(200):
            await plant.advance(1.0)
            if rt.transaction("carlo").phase == tr.PHASE_WAITING_HEAT:
                break
        state, attrs = rt.attributes("carlo")
        assert state == "waiting_heat" and attrs["reason"] == "waiting_heat" and attrs["proposed_action"] == "heat" and attrs["conditions"] == []
        assert attrs["transaction"]["inhibition_owned"] is True and attrs["managed"] is True and attrs["owner"] == "controller"
        plant.set("sensor.power", "2800")
        await plant.settle()
        await plant.advance(1.0)
        state, attrs = rt.attributes("carlo")
        assert attrs["proposed_action"] in ("off", "none") and attrs["transaction"]["failure"] == tr.FAILURE_CONDITIONS_CHANGED

    run(scenario())


def test_an_override_and_a_suspension_publish_the_policy_view_for_diagnostics_only():
    async def scenario() -> None:
        plant, rt = make()
        await plant.start()
        await plant.advance(5.0)
        plant.user_call(CARLO_SPLIT, "set_hvac_mode", hvac_mode="heat")
        await plant.settle()
        await plant.advance(5.0)
        state, attrs = rt.attributes("carlo")
        assert attrs["override"]["active"] is True and attrs["reason"] == "override_hold" and attrs["proposed_action"] == "none"
        await rt.async_suspend("general")
        await plant.settle()
        await plant.advance(5.0)
        state, attrs = rt.attributes("carlo")
        assert state == "suspended" and attrs["reason"] == "suspended" and attrs["suspension"] in ("shutting_down", "confirmed")

    run(scenario())


def test_invalid_persisted_data_is_dropped_and_reported_not_guessed():
    async def scenario() -> None:
        plant, rt = make(stored={"schema_version": 9})
        assert rt.load_errors == ("store_invalid",)
        _, rt2 = make(stored={"schema_version": 1, "reservation": {"holder": "x", "token": None}, "rooms": {"carlo": {"transaction": {"schema_version": 1, "room_key": "other"}}}})
        assert "reservation_invalid" in rt2.load_errors and any(e.startswith("transaction_invalid") for e in rt2.load_errors)
        assert rt2.transaction("carlo").phase == tr.PHASE_IDLE and rt2.reservation == tr.PdcReservation()

    run(scenario())


# --- manual override ---------------------------------------------------------------------------


def test_an_authenticated_user_command_on_the_split_starts_a_120_minute_override_and_adopts_a_session():
    async def scenario() -> None:
        plant, rt = make()
        await plant.start()
        await plant.advance(5.0)
        plant.user_call(CARLO_SPLIT, "set_hvac_mode", hvac_mode="cool")
        await plant.settle()
        await plant.advance(5.0)
        ov = rt.override("carlo")
        assert ov.active and ov.provenance == "user" and ov.source == "user_command" and ov.until == pytest.approx(plant.clock - 5.0 + 7200.0, abs=10)
        tx = rt.transaction("carlo")
        assert tx.phase == tr.PHASE_OVERRIDE_HOLD and tx.manual and tx.inhibition_owned and plant.inhibited() is True
        assert plant.states["input_boolean.override_carlo"]["state"] == "on" and plant.states["input_boolean.managed_carlo"]["state"] == "on"
        # the manual state is copied to the PDC VTherm (baseline "sincronizza")
        assert plant.states[CARLO_PDC]["state"] == "cool"
        # the automatic start does not run during the override
        await plant.advance(300.0)
        assert (CARLO_PDC, "set_hvac_mode", "heat") not in plant.kinds()

    run(scenario())


def test_the_override_ends_by_itself_after_120_minutes_and_the_pdc_goes_back_through_the_safe_return():
    async def scenario() -> None:
        plant, rt = make()
        await plant.start()
        await plant.advance(5.0)
        plant.user_call(CARLO_SPLIT, "set_hvac_mode", hvac_mode="heat")
        await plant.settle()
        await plant.advance(30.0)
        assert rt.override("carlo").active and rt.transaction("carlo").phase == tr.PHASE_OVERRIDE_HOLD
        await plant.advance(7200.0, step=30.0)
        assert not rt.override("carlo").active and rt.transaction("carlo").phase == tr.PHASE_IDLE
        assert plant.states[CARLO_SPLIT]["state"] == "off" and plant.inhibited() is False
        assert plant.states["input_boolean.override_carlo"]["state"] == "off"
        order = [e for e in plant.log if e[0] in ("call", "unset")]
        assert max(i for i, e in enumerate(order) if e[0] == "unset") > max(i for i, e in enumerate(order) if e == ("call", (CARLO_PDC, "set_hvac_mode", "off")))

    run(scenario())


def test_the_user_can_resume_the_automatic_mode_and_the_override_ends_only_when_off_is_confirmed():
    async def scenario() -> None:
        plant, rt = make()
        await plant.start()
        plant.user_call(CARLO_SPLIT, "set_hvac_mode", hvac_mode="heat")
        await plant.settle()
        await plant.advance(20.0)
        assert await rt.async_resume_automatic("carlo") is True
        plant.split_obeys = False
        plant.split_follows = False
        await plant.advance(60.0)
        assert rt.override("carlo").active, "the override stays while the split does not confirm OFF"
        assert plant.inhibited() is True
        plant.split_obeys = True
        plant.set(CARLO_SPLIT, "off")
        plant.set(CARLO_PDC, "off")
        await plant.advance(200.0)
        assert not rt.override("carlo").active and rt.transaction("carlo").phase == tr.PHASE_IDLE and plant.inhibited() is False
        assert await rt.async_resume_automatic("carlo") is False  # nothing left to resume
        assert await rt.async_resume_automatic("nobody") is False

    run(scenario())


def test_power_protection_under_override_stops_the_split_at_once_and_keeps_the_override():
    async def scenario() -> None:
        plant, rt = make()
        await plant.start()
        plant.user_call(CARLO_SPLIT, "set_hvac_mode", hvac_mode="heat")
        await plant.settle()
        await plant.advance(20.0)
        assert rt.transaction("carlo").phase == tr.PHASE_OVERRIDE_HOLD
        plant.split_follows = False
        plant.set("sensor.power", "2800")
        await plant.settle()
        await plant.advance(5.0)
        assert plant.states[CARLO_SPLIT]["state"] == "off", "protections switch the split off without waiting for the PDC VTherm"
        calls = plant.kinds()
        assert calls.index((CARLO_PDC, "set_hvac_mode", "off")) < calls.index((CARLO_SPLIT, "set_hvac_mode", "off"))
        assert rt.override("carlo").active  # kept: no automatic restart
        assert rt.transaction("carlo").phase == tr.PHASE_IDLE and plant.inhibited() is False

    run(scenario())


def test_the_split_switched_off_by_the_user_during_the_override_gives_the_radiator_back():
    async def scenario() -> None:
        plant, rt = make()
        await plant.start()
        plant.user_call(CARLO_SPLIT, "set_hvac_mode", hvac_mode="heat")
        await plant.settle()
        await plant.advance(20.0)
        assert plant.inhibited() is True
        plant.user_call(CARLO_SPLIT, "set_hvac_mode", hvac_mode="off")
        await plant.settle()
        await plant.advance(60.0)
        assert plant.inhibited() is False and rt.transaction("carlo").phase == tr.PHASE_IDLE and rt.override("carlo").active

    run(scenario())


def test_own_commands_are_expected_effects_and_do_not_start_an_override_but_a_remote_does_with_an_unknown_author():
    async def scenario() -> None:
        plant, rt = make()
        await run_until_running(plant)
        await plant.advance(5.0)
        assert not rt.override("carlo").active  # the HEAT it sent itself was registered as an expected effect
        plant.remote_control("carlo", "cool")  # a change without any Home Assistant call
        await plant.settle()
        await plant.advance(5.0)
        ov = rt.override("carlo")
        assert ov.active and ov.provenance == prov.PROVENANCE_UNKNOWN and ov.source == prov.SOURCE_DEVICE_CHANGE
        assert rt.transaction("carlo").phase == tr.PHASE_RUNNING  # the running session keeps the radiator suspended
        assert plant.states[CARLO_PDC]["state"] == "cool"  # and the PDC VTherm follows the manual mode

    run(scenario())


def test_a_split_found_running_after_a_reconnection_starts_an_override_with_unknown_author():
    async def scenario() -> None:
        plant, rt = make()
        plant.set(CARLO_SPLIT, "unavailable")
        await plant.start()
        await plant.advance(20.0)
        plant.set(CARLO_SPLIT, "heat")  # reconnects already running
        await plant.settle()
        await plant.advance(5.0)
        assert rt.override("carlo").active and rt.override("carlo").source == prov.SOURCE_DEVICE_FOUND_RUNNING and rt.override("carlo").provenance == prov.PROVENANCE_UNKNOWN

    run(scenario())


def test_an_override_survives_a_restart_without_renewal_and_expires_when_its_time_has_passed():
    async def scenario() -> None:
        plant, rt = make()
        await plant.start()
        plant.user_call(CARLO_SPLIT, "set_hvac_mode", hvac_mode="heat")
        await plant.settle()
        await plant.advance(30.0)
        stored = copy.deepcopy(plant.stored)
        until = rt.override("carlo").until
        await rt.async_stop()
        plant2, _ = make()
        plant2.states = plant.states
        plant2.clock = plant.clock + 10
        plant2.runtime = rt_mod.PdcRuntime(plant2.config, _PortsView(plant2), stored=stored, allow_control=True)
        await plant2.start()
        await plant2.advance(20.0)
        assert plant2.runtime.override("carlo").active and plant2.runtime.override("carlo").until == until
        stored2 = copy.deepcopy(plant2.stored)
        await plant2.runtime.async_stop()
        plant3, _ = make()
        plant3.states = plant2.states
        plant3.clock = until + 100
        plant3.runtime = rt_mod.PdcRuntime(plant3.config, _PortsView(plant3), stored=stored2, allow_control=True)
        await plant3.start()
        await plant3.advance(60.0)
        assert not plant3.runtime.override("carlo").active

    run(scenario())


# --- general suspension ------------------------------------------------------------------------


def test_suspension_stops_the_pdc_cancels_overrides_blocks_starts_and_holds_the_radiator_inhibition():
    async def scenario() -> None:
        plant, rt = make()
        await run_until_running(plant)
        result = await rt.async_suspend("general", hold_radiator_inhibition=True)
        assert result.accepted and result.state == rt_mod.SUSPENSION_SHUTTING_DOWN
        assert plant.stored["suspension"]["active"] is True  # persisted before anything else
        await plant.settle()
        await plant.advance(10.0)
        assert plant.states[CARLO_SPLIT]["state"] == "off" and plant.states[CARLO_PDC]["state"] == "off"
        assert rt.suspension_state() == rt_mod.SUSPENSION_CONFIRMED and dict(rt._room_states())["carlo"] == rt_mod.SUSPENSION_CONFIRMED
        # the owned inhibition is held, so the radiator valve cannot reopen during the shutdown
        assert rt.transaction("carlo").phase == tr.PHASE_RELEASING and rt.transaction("carlo").inhibition_owned and plant.inhibited() is True
        assert not any(e[0] == "unset" for e in plant.log) and plant.states[CARLO_VALVE]["state"] == "off"
        assert rt.attributes("carlo")[0] == "suspended" and rt.attributes("carlo")[1]["transaction"]["release_held"] is True
        # no new automatic start, however long we wait
        await plant.advance(1200.0, step=30.0)
        assert (CARLO_PDC, "set_hvac_mode", "heat") not in plant.kinds()[plant.kinds().index((CARLO_PDC, "set_hvac_mode", "off")) :]
        # a manual start is undone, an override is not adopted
        plant.user_call(CARLO_SPLIT, "set_hvac_mode", hvac_mode="heat")
        await plant.settle()
        await plant.advance(40.0)
        assert plant.states[CARLO_SPLIT]["state"] == "off" and not rt.override("carlo").active
        # the resume releases the radiator and switches nothing on
        resumed = await rt.async_resume()
        assert resumed.accepted and rt.suspension_state() == rt_mod.SUSPENSION_NONE
        await plant.settle()
        await plant.advance(60.0)
        assert plant.inhibited() is False and rt.transaction("carlo").phase == tr.PHASE_IDLE and plant.states[CARLO_SPLIT]["state"] == "off"
        assert (await rt.async_resume()).reason == rt_mod.REFUSED_NOT_SUSPENDED

    run(scenario())


def test_suspension_without_holding_releases_the_radiator_after_the_confirmed_off():
    async def scenario() -> None:
        plant, rt = make()
        await run_until_running(plant)
        await rt.async_suspend("general", hold_radiator_inhibition=False)
        await plant.settle()
        await plant.advance(40.0)
        assert plant.inhibited() is False and rt.transaction("carlo").phase == tr.PHASE_IDLE and rt.suspension_state() == rt_mod.SUSPENSION_CONFIRMED

    run(scenario())


def test_suspension_with_an_unreachable_device_is_reported_per_room_and_never_as_off():
    async def scenario() -> None:
        plant, rt = make()
        await run_until_running(plant)
        plant.set(CARLO_SPLIT, "unavailable")
        plant.fail_calls.add((CARLO_PDC, "set_hvac_mode"))
        plant.split_obeys = False
        plant.split_follows = False
        await rt.async_suspend("general")
        await plant.settle()
        await plant.advance(20.0)
        assert rt.suspension_state() == rt_mod.SUSPENSION_SHUTTING_DOWN  # not confirmed, not yet declared unreachable
        await plant.advance(120.0)
        assert rt.suspension_state() == rt_mod.SUSPENSION_UNREACHABLE and dict(rt._room_states())["carlo"] == rt_mod.SUSPENSION_UNREACHABLE
        assert not any(e[0] == "unset" for e in plant.log) and plant.inhibited() is True
        assert rt.attributes("carlo")[1]["suspension"] == rt_mod.SUSPENSION_UNREACHABLE

    run(scenario())


def test_suspension_survives_a_restart_and_keeps_enforcing():
    async def scenario() -> None:
        plant, rt = make()
        await plant.start()
        await plant.advance(5.0)
        await rt.async_suspend("general")
        await plant.settle()
        stored = copy.deepcopy(plant.stored)
        await rt.async_stop()
        plant2, _ = make()
        plant2.states = plant.states
        plant2.clock = plant.clock + 600
        plant2.set(CARLO_SPLIT, "heat")  # the split came on while Home Assistant was down
        plant2.runtime = rt_mod.PdcRuntime(plant2.config, _PortsView(plant2), stored=stored, allow_control=True)
        await plant2.start()
        await plant2.advance(120.0)
        assert plant2.runtime.suspension.active and plant2.states[CARLO_SPLIT]["state"] == "off"
        assert (CARLO_PDC, "set_hvac_mode", "heat") not in plant2.kinds()

    run(scenario())


def test_suspension_is_refused_outside_control_and_sends_nothing():
    async def scenario() -> None:
        for mode in ("observe", "disabled"):
            plant, rt = make(mode)
            rt.start()
            result = await rt.async_suspend("general")
            assert result == rt_mod.PdcSuspendResult(False, rt_mod.SUSPENSION_NONE, rt_mod.REFUSED_NOT_CONTROL)
            assert plant.calls == [] and plant.stored is None and not rt.suspension.active
        plant, rt = make()
        assert (await rt.async_suspend("general")).reason == rt_mod.REFUSED_NOT_CONTROL  # not started yet

    run(scenario())


def test_a_suspend_request_while_suspended_is_idempotent():
    async def scenario() -> None:
        plant, rt = make()
        await plant.start()
        first = await rt.async_suspend("a")
        second = await rt.async_suspend("b")
        assert first.accepted and second.accepted and second.reason == rt_mod.REFUSED_ALREADY and rt.suspension.reason == "a"

    run(scenario())


# --- robustness --------------------------------------------------------------------------------


def test_a_failed_persistence_never_blocks_a_stop():
    async def scenario() -> None:
        plant, rt = make()
        await run_until_running(plant)
        plant.save_fail = True
        plant.set("sensor.temp_carlo", "19.6")
        await plant.settle()
        await plant.advance(10.0)
        assert plant.states[CARLO_SPLIT]["state"] == "off" and plant.inhibited() is False and rt.transaction("carlo").phase == tr.PHASE_IDLE

    run(scenario())


def test_a_service_failure_is_reported_and_the_state_is_read_again():
    async def scenario() -> None:
        plant, rt = make()
        plant.fail_calls.add((CARLO_PDC, "set_hvac_mode"))
        await plant.start()
        await plant.advance(200.0)
        tx = rt.transaction("carlo")
        assert (CARLO_PDC, "set_hvac_mode", "heat") in plant.kinds()  # it tried
        assert plant.states[CARLO_SPLIT]["state"] == "off"  # the failed call did not heat anything
        # the HEAT was never confirmed: back to the radiator through the return path, which confirmed off before releasing
        assert tx.failure == tr.FAILURE_SPLIT_UNCONFIRMED and tx.phase == tr.PHASE_IDLE and plant.inhibited() is False
        assert plant.log.index(("call", (CARLO_PDC, "set_hvac_mode", "heat"))) < next(i for i, e in enumerate(plant.log) if e[0] == "unset")

    run(scenario())


def test_stop_cancels_the_timers_and_workers_and_is_idempotent():
    async def scenario() -> None:
        plant, rt = make()
        await plant.start()
        await plant.advance(30.0)
        await rt.async_stop()
        await rt.async_stop()
        n = len(plant.calls)
        rt.request_evaluation()
        await plant.advance(600.0)
        assert len(plant.calls) == n and all(room.worker is None or room.worker.done() for room in rt._rooms.values())
        assert all(room.wake_cancel is None for room in rt._rooms.values())

    run(scenario())


def test_an_event_during_a_pass_is_not_lost():
    async def scenario() -> None:
        plant, rt = make("observe")
        await plant.start()
        await plant.advance(1.0)
        generation = rt._rooms["carlo"].generation
        rt.request_evaluation("carlo")
        rt.request_evaluation("carlo")
        rt.request_evaluation("carlo")
        await plant.settle()
        assert rt._rooms["carlo"].generation >= generation + 1
        rt._rooms["carlo"].rerun = False
        # an event requested while the worker is awaiting runs one more pass
        done = rt._rooms["carlo"].generation
        rt.request_evaluation("carlo")
        rt._rooms["carlo"].rerun = True
        await plant.settle()
        assert rt._rooms["carlo"].generation >= done

    run(scenario())


def test_the_diagnostics_are_published_for_every_room_with_the_contract_keys():
    async def scenario() -> None:
        plant, rt = make(rooms=("carlo", "diego"))
        await plant.start()
        await plant.advance(30.0)
        for key in ("carlo", "diego"):
            state, attrs = rt.attributes(key)
            assert {
                "schema_version",
                "mode",
                "owner",
                "proposed_action",
                "reason",
                "conditions",
                "power_limit_w",
                "stability_s",
                "transaction",
                "reservation",
                "override",
                "last_error",
            } <= set(attrs)
            assert attrs["room_key"] == key and json.loads(json.dumps(attrs, allow_nan=False)) == attrs
        assert {room for room, _, _ in plant.published} == {"carlo", "diego"}

    run(scenario())


def test_the_runtime_module_is_independent_of_home_assistant():
    tree = ast.parse((COMPONENT_DIR / "heating_pdc_runtime.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or "").split(".")[0])
    assert not {name for name in imported if name.startswith("homeassistant") or name.startswith("vtherm_api")}, imported
