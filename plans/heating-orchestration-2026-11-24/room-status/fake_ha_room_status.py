"""Scenarios of the per-room status publication of the REAL orchestration manager, against the fake Home Assistant.

No Home Assistant code is executed (``m2b/fake_ha_harness.py`` replaces the few APIs the manager uses). The sensor
entity (``sensor.py``) and the entity registry cleanup need real Home Assistant and are NOT covered here.
Run: python -I plans/heating-orchestration-2026-11-24/room-status/fake_ha_room_status.py
The last line is a JSON summary.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "m2b"))
import fake_ha_harness as h  # noqa: E402

mod = h.install_fakes()
spec = importlib.util.spec_from_file_location("tcol_rs", h.REPO / "tests" / "test_heating_boiler_collector.py")
tcol = importlib.util.module_from_spec(spec)
sys.modules["tcol_rs"] = tcol
spec.loader.exec_module(tcol)

NOW = 1_800_000_000.0
CHECKS = 0
EVENTS = {"checks": 0}


def check(condition, message):
    EVENTS["checks"] += 1
    assert condition, message
    print("PASS", message)


def section(**changes):
    value = json.loads(json.dumps(tcol.SECTION))
    value.update(mode="observe")
    value.update(changes)
    return value


def put(hass, snap):
    for entity_id, value in snap.items():
        hass.states.async_set(entity_id, value["state"], value.get("attributes") or {})


class Ctx:
    pass


async def make(snap=None, sec=None, rooms=True, start=True):
    ctx = Ctx()
    ctx.hass = h.FakeHass()
    ctx.hass.stored = {}
    ctx.hass.save_hook = None
    ctx.clock = h.Clock(NOW)
    ctx.world = h.World(ctx.hass, ctx.clock)
    ctx.world.start_intervals()
    put(ctx.hass, snap if snap is not None else tcol.base_snapshot(now=NOW))
    ctx.boiler = []
    ctx.rooms = {}
    ctx.mgr = mod.FeatureHeatingOrchestrationManager(ctx.hass, "ENTRY", sec if sec is not None else section(), clock=ctx.clock)
    ctx.mgr.attach(lambda state, attrs: ctx.boiler.append((state, attrs)))
    if rooms:
        for key in ctx.mgr.room_keys:
            ctx.rooms[key] = []
            ctx.mgr.attach_room(key, (lambda k: lambda state, attrs: ctx.rooms[k].append((state, attrs)))(key))
    if start:
        await ctx.mgr.async_start()
        await h.run_loop()
    return ctx


scenarios = []


def scenario(func):
    scenarios.append(func)
    return func


@scenario
async def one_sensor_per_configured_room_in_the_same_round():
    ctx = await make()
    check(set(ctx.mgr.room_keys) == {"bedroom", "living"}, "one status entry per configured room (also the one without PDC)")
    boiler_state, boiler_attrs = ctx.boiler[-1]
    for key in ("bedroom", "living"):
        state, attrs = ctx.rooms[key][-1]
        check(attrs["schema_version"] == 1 and attrs["room_key"] == key and attrs["mode"] == "observe", f"{key}: schema, key and mode")
        check(
            attrs["evaluated_at"] == boiler_attrs["evaluated_at"] and attrs["generation"] == boiler_attrs["generation"], f"{key}: same clock and generation as the boiler snapshot"
        )
        check(state == attrs["heat_state"], f"{key}: the state is the heat_state ({state})")
    check(ctx.rooms["bedroom"][-1][1]["pump"] is None or isinstance(ctx.rooms["bedroom"][-1][1]["pump"], dict), "pump object only with PDC data")
    check([s for s in ctx.rooms["living"][-1][1]["stages"]] != [], "stages are published")
    check(ctx.hass.services.calls == [], "observe: not one service call")
    await ctx.mgr.async_stop()


@scenario
async def unchanged_evaluations_are_not_republished_until_the_heartbeat():
    ctx = await make()
    before = {key: len(value) for key, value in ctx.rooms.items()}
    ctx.mgr.async_request_evaluation()
    await h.run_loop()
    await ctx.world.advance(5)
    check({key: len(value) for key, value in ctx.rooms.items()} == before, "nothing new is written when nothing changed")
    await ctx.world.advance(30)
    check(all(len(ctx.rooms[key]) > before[key] for key in before), "refreshed by the heartbeat")
    check(ctx.rooms["living"][-1][1]["evaluated_at"] > NOW, "the heartbeat carries the new evaluated_at")
    await ctx.mgr.async_stop()


@scenario
async def a_real_change_is_published_at_once():
    ctx = await make()
    count = len(ctx.rooms["living"])
    snap = tcol.base_snapshot(now=NOW)
    ctx.hass.states.async_set(tcol.CENTRAL, "Off", {})
    await h.run_loop()
    check(len(ctx.rooms["living"]) > count, "central mode change republishes the room")
    attrs = ctx.rooms["living"][-1][1]
    central = next(c for c in attrs["conditions"] if c["id"] == "central_mode")
    check(central["status"] == "fail" and central["reason_code"] == "central_off", "central_mode condition fails with central_off")
    check(attrs["blocker"]["id"] in {c["id"] for c in attrs["conditions"]}, "the blocker is one of the published conditions")
    await ctx.mgr.async_stop()


@scenario
async def disabled_mode_publishes_disabled_without_conditions():
    ctx = await make(sec=section(mode="disabled"))
    for key in ctx.mgr.room_keys:
        state, attrs = ctx.rooms[key][-1]
        check(state == "disabled" and attrs["conditions"] == [] and attrs["mode"] == "disabled", f"{key}: disabled, no conditions")
    check(ctx.hass.services.calls == [], "disabled: not one service call")
    await ctx.mgr.async_stop()


@scenario
async def invalid_configuration_is_an_error_for_every_room_key_asked():
    ctx = await make(sec={"mode": "bogus"}, rooms=False)
    state, attrs = ctx.mgr.room_state("anything")
    check(state == "error" and attrs["last_error"], "invalid configuration shows an error")
    check(ctx.mgr.room_keys == (), "no rooms are advertised for an invalid configuration")
    await ctx.mgr.async_stop()


@scenario
async def starting_before_the_first_valid_inputs():
    snap = tcol.base_snapshot(now=NOW)
    snap.pop(tcol.COUNT)
    ctx = await make(snap=snap)
    state, attrs = ctx.rooms["living"][-1]
    check(state == "starting" and attrs["conditions"] == [], "starting while a central entity is missing")
    await ctx.mgr.async_stop()


@scenario
async def a_failing_room_evaluation_never_blocks_the_boiler_or_the_other_rooms():
    ctx = await make(start=False)
    original = mod.evaluate_room_status

    def boom(room, *args, **kwargs):
        if room.room_key == "bedroom":
            raise RuntimeError("boom")
        return original(room, *args, **kwargs)

    mod.evaluate_room_status = boom
    try:
        await ctx.mgr.async_start()
        await h.run_loop()
    finally:
        mod.evaluate_room_status = original
    check(ctx.boiler and ctx.boiler[-1][0] not in ("error",), "the boiler decision is still published")
    state, attrs = ctx.rooms["bedroom"][-1]
    check(state == "error" and attrs["last_error"] == "evaluation_error:RuntimeError", "the failing room reports an evaluation error")
    check(ctx.rooms["living"][-1][0] != "error", "the other room is evaluated")
    await ctx.mgr.async_stop()


@scenario
async def the_pdc_view_comes_from_the_provider_and_is_forwarded():
    ctx = await make(start=False)
    seen = []

    def provider(room_key):
        seen.append(room_key)
        return "idle", {
            "schema_version": 1,
            "mode": "observe",
            "owner": "observer",
            "evaluated_at": NOW,
            "managed": False,
            "split_mode": "off",
            "split_action": "idle",
            "diagnostic_state": "idle|1100|120",
            "transaction": {"phase": "idle"},
            "conditions": [{"id": "automatic", "status": "pass", "reason_code": None, "actual": "on", "expected": "on", "deadline": None, "unit": None}],
        }

    ctx.mgr.pdc_state_provider = provider
    await ctx.mgr.async_start()
    await h.run_loop()
    check(seen and set(seen) == {"bedroom"}, "the provider is asked only for rooms that have a PDC")
    attrs = ctx.rooms["bedroom"][-1][1]
    check(any(c["stage"] == "pump" and c["id"] == "automatic" for c in attrs["conditions"]), "the PDC conditions are forwarded to the pump stage")
    check(not any(c["stage"] == "pump" for c in ctx.rooms["living"][-1][1]["conditions"]), "a room without PDC has no pump stage")
    await ctx.mgr.async_stop()


@scenario
async def the_boiler_output_is_identical_with_and_without_the_room_sensors():
    with_rooms = await make(rooms=True)
    without = await make(rooms=False)
    keys = ("proposed_action", "reason", "deadline", "rooms", "excluded_rooms", "conditions", "inputs", "valid_valve_count", "cycle")
    check({k: with_rooms.boiler[-1][1][k] for k in keys} == {k: without.boiler[-1][1][k] for k in keys}, "same boiler attributes (rooms, excluded_rooms, conditions, cycle)")
    check(with_rooms.boiler[-1][0] == without.boiler[-1][0], "same boiler state")
    await with_rooms.mgr.async_stop()
    await without.mgr.async_stop()


@scenario
async def the_new_code_sends_no_command_when_the_mode_changes_and_back():
    results = []
    for with_rooms in (True, False):
        ctx = await make(
            sec=section(mode="control", external_owner_entity_ids=["automation.owner_a"], control_confirmed=True),
            snap={
                **tcol.base_snapshot(now=NOW),
                "automation.owner_a": {"state": "off"},
                tcol.MASTER: {"state": "heat", "attributes": {"hvac_modes": ["off", "heat"]}},
                tcol.COUNT: {"state": "0", "attributes": {"active_device_ids": []}},
            },
            rooms=with_rooms,
            start=False,
        )
        await ctx.mgr.async_start()
        await ctx.world.advance(60)
        results.append([(c["data"]["entity_id"], c["data"]["hvac_mode"]) for c in ctx.hass.services.calls])
        await ctx.mgr.async_stop()
    check(len(results[0]) >= 1, "the control scenario really sends a command (the comparison is not vacuous)")
    check(results[0] == results[1], f"the service calls are the same with and without the room sensors ({len(results[0])} call(s) each)")
    ctx = await make()
    check(ctx.hass.services.calls == [], "observe again: not one service call")
    await ctx.mgr.async_stop()


@scenario
async def a_rebuilt_manager_adopts_the_room_publishers():
    ctx = await make()
    await ctx.mgr.async_stop()
    replacement = mod.FeatureHeatingOrchestrationManager(ctx.hass, "ENTRY", section(), clock=ctx.clock)
    replacement.adopt_publishers(ctx.mgr)
    check(len(ctx.rooms["living"]) >= 2, "the surviving sensor receives the state of the new manager")
    detach = ctx.mgr.attach_room("living", lambda *_: None, "sensor.x")
    detach()
    check("sensor.x" not in ctx.mgr._own_entity_ids, "detach releases the own entity id")  # noqa: SLF001
    await replacement.async_stop()


async def main():
    failed = 0
    for func in scenarios:
        try:
            await func()
            print("ok  ", func.__name__)
        except Exception:  # noqa: BLE001
            failed += 1
            print("FAIL", func.__name__)
            traceback.print_exc()
    print(json.dumps({"scenarios": len(scenarios), "failed": failed, "checks": EVENTS["checks"]}))
    return failed


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
