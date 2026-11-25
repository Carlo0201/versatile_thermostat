"""Scenarios of the real boiler manager (M2-B) against the fake Home Assistant of fake_ha_harness.py.

No Home Assistant code is executed. This does not replace the Home Assistant tests of tests/test_heating_orchestration.py."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fake_ha_harness as h  # noqa: E402

mod = h.install_fakes()

spec = importlib.util.spec_from_file_location("tcol", h.REPO / "tests" / "test_heating_boiler_collector.py")
tcol = importlib.util.module_from_spec(spec)
sys.modules["tcol"] = tcol
spec.loader.exec_module(tcol)

NOW = 1_800_000_000.0
MASTER, COUNT, LEGACY = tcol.MASTER, tcol.COUNT, tcol.LEGACY
OWNERS = ["automation.caldaia_controllo_stabile_delle_richieste", "automation.caldaia_filtro_richiesta_vtherm"]
MODES = {"hvac_modes": ["off", "heat"]}


def section(**changes):
    value = json.loads(json.dumps(tcol.SECTION))
    value.update(mode="control", external_owner_entity_ids=list(OWNERS), control_confirmed=True)
    value.update(changes)
    return value


def world(**changes):
    snap = tcol.base_snapshot(now=NOW)
    snap.setdefault(LEGACY, {"state": json.dumps(tcol.collector.cycle_to_dict(None))})
    for owner in OWNERS:
        snap[owner] = {"state": "off"}
    snap.update(changes)
    return snap


def off_world(**changes):
    base = {MASTER: {"state": "heat", "attributes": MODES}, COUNT: {"state": "0", "attributes": {"active_device_ids": []}}}
    base.update(changes)
    return world(**base)


def heat_world(**changes):
    base = {MASTER: {"state": "off", "attributes": MODES}, tcol.MANUAL: {"state": "on"}}
    base.update(changes)
    return world(**base)


def put(hass, snap):
    for entity_id, value in snap.items():
        hass.states.async_set(entity_id, value["state"], value.get("attributes") or {})


class Ctx:
    pass


async def make(snap, sec=None, stored=None, **kw):
    ctx = Ctx()
    ctx.hass = h.FakeHass()
    ctx.hass.stored = dict(stored or {})
    ctx.hass.save_hook = None
    ctx.clock = h.Clock(kw.get("now", NOW))
    ctx.world = h.World(ctx.hass, ctx.clock)
    ctx.world.start_intervals()
    put(ctx.hass, snap)
    ctx.pub = []
    ctx.mgr = mod.FeatureHeatingOrchestrationManager(ctx.hass, "ENTRY", sec if sec is not None else section(), clock=ctx.clock)
    ctx.mgr.attach(lambda state, attrs: ctx.pub.append((state, attrs)))
    await ctx.mgr.async_start()
    await h.run_loop()
    return ctx


def attrs(ctx):
    return ctx.mgr.attributes


def calls(ctx):
    return [(c["data"]["entity_id"], c["data"]["hvac_mode"]) for c in ctx.hass.services.calls]


def logbook(ctx):
    return [d["message"] for t, d in ctx.hass.bus.fired if t == "logbook_entry"]


results = []


def scenario(func):
    results.append(func)
    return func


@scenario
async def s01_settling_then_one_off():
    ctx = await make(off_world())
    a = attrs(ctx)
    assert a["mode"] == "control" and a["owner"] == "controller" and a["control_status"] == "settling", a
    assert a["proposed_action"] == "off" and a["reason"] == "no_valid_radiator", a
    await ctx.world.advance(15)
    assert calls(ctx) == [], "before settling"
    await ctx.world.advance(15)
    assert calls(ctx) == [(MASTER, "off")], calls(ctx)
    a = attrs(ctx)
    assert a["command_status"] == "pending" and a["commanded_mode"] == "off" and a["command_attempts"] == 1, a
    assert a["last_confirmation_at"] is None and a["service_completed_at"] == ctx.clock.now and a["control_status"] == "pending", a
    await ctx.mgr.async_stop()


@scenario
async def s02_confirmation_and_logbook():
    ctx = await make(off_world())
    await ctx.world.advance(30)
    ctx.hass.states.async_set(MASTER, "off", MODES)
    await h.run_loop()
    a = attrs(ctx)
    assert a["command_status"] == "confirmed" and a["control_status"] == "confirmed" and a["last_confirmation_at"] == ctx.clock.now, a
    for _ in range(6):
        await ctx.world.advance(15)
    assert len(calls(ctx)) == 1
    msgs = logbook(ctx)
    assert len(msgs) == 2 and msgs[0].startswith("OIKOS_HEATING_V1|boiler|sent|off|attempt=1/4") and msgs[1].startswith("OIKOS_HEATING_V1|boiler|confirmed|off|attempt=1/4"), msgs
    await ctx.mgr.async_stop()


@scenario
async def s03_four_sends_then_command_failed():
    ctx = await make(off_world())
    await ctx.world.advance(30)
    assert len(calls(ctx)) == 1
    await ctx.world.advance(59)
    assert len(calls(ctx)) == 1 and attrs(ctx)["command_status"] == "pending", attrs(ctx)["command_status"]
    await ctx.world.advance(2)  # 61 s after the completion: timeout, retry in 60 s
    a = attrs(ctx)
    assert a["command_status"] == "failed" and a["last_error"] == "confirmation_timeout", a
    await ctx.world.advance(60)
    assert len(calls(ctx)) == 2, len(calls(ctx))
    await ctx.world.advance(62)
    await ctx.world.advance(121)
    assert len(calls(ctx)) == 3, len(calls(ctx))
    await ctx.world.advance(62)
    await ctx.world.advance(301)
    assert len(calls(ctx)) == 4, len(calls(ctx))
    await ctx.world.advance(62)
    a = attrs(ctx)
    assert a["control_status"] == "command_failed" and a["command_attempts"] == 4, a
    for _ in range(20):
        await ctx.world.advance(600)
    assert len(calls(ctx)) == 4
    kinds = [m.split("|")[2] for m in logbook(ctx)]
    assert kinds.count("sent") == 4 and kinds.count("failed") == 4 and kinds.count("exhausted") == 1, kinds
    await ctx.mgr.async_stop()


@scenario
async def s04_service_error_is_retried():
    ctx = await make(off_world())

    async def boom(call):
        raise mod.HomeAssistantError("boom") if hasattr(mod, "HomeAssistantError") else RuntimeError("boom")

    ctx.hass.services.handler = boom
    await ctx.world.advance(30)
    assert len(calls(ctx)) == 1
    a = attrs(ctx)
    assert a["command_status"] == "failed" and a["last_error"].startswith("service_error:") and a["command_retry_at"] == ctx.clock.now + 60, a
    await ctx.world.advance(59)
    assert len(calls(ctx)) == 1
    await ctx.world.advance(2)
    assert len(calls(ctx)) == 2
    await ctx.mgr.async_stop()


@scenario
async def s05_blocked_by_external_owner_and_cancel_on_reactivation():
    ctx = await make(off_world(**{OWNERS[0]: {"state": "on"}}))
    for _ in range(8):
        await ctx.world.advance(15)
    a = attrs(ctx)
    assert calls(ctx) == [] and a["owner"] == "observer" and a["control_status"] == "blocked_external_owner", a
    ctx.hass.states.async_set(OWNERS[0], "off")
    await h.run_loop()
    await ctx.world.advance(30)  # settling window is anchored at the first valid evaluation: already latched
    assert len(calls(ctx)) == 1, calls(ctx)
    ctx.hass.states.async_set(OWNERS[0], "on")
    await h.run_loop()
    a = attrs(ctx)
    assert a["owner"] == "observer" and a["command_status"] == "cancelled" and a["control_status"] == "blocked_external_owner", a
    for _ in range(10):
        await ctx.world.advance(120)
    assert len(calls(ctx)) == 1, calls(ctx)
    ctx.hass.states.async_set(OWNERS[0], "off")
    await h.run_loop()
    await ctx.world.advance(1)
    assert len(calls(ctx)) == 2 and attrs(ctx)["command_attempts"] == 1
    await ctx.mgr.async_stop()


@scenario
async def s06_missing_or_unknown_owner_blocks_and_unknown_master_blocks():
    for state in ("unknown", "unavailable", None):
        snap = off_world()
        if state is not None:
            snap[OWNERS[1]] = {"state": state}
        ctx = await make(snap)
        if state is None:
            ctx.hass.states.async_remove(OWNERS[1])
        for _ in range(8):
            await ctx.world.advance(15)
        assert calls(ctx) == [] and attrs(ctx)["control_status"] == "blocked_external_owner", (state, attrs(ctx)["control_status"])
        await ctx.mgr.async_stop()
    ctx = await make(off_world(**{MASTER: {"state": "unavailable"}}))
    for _ in range(8):
        await ctx.world.advance(15)
    assert calls(ctx) == [] and attrs(ctx)["control_status"] == "blocked_master_unknown", attrs(ctx)["control_status"]
    await ctx.mgr.async_stop()


@scenario
async def s07_unload_mid_wait_then_restart_reads_the_master():
    ctx = await make(off_world())
    await ctx.world.advance(30)
    assert len(calls(ctx)) == 1
    await ctx.mgr.async_stop()
    for _ in range(6):
        await ctx.world.advance(600)
    assert len(calls(ctx)) == 1
    stored = ctx.hass.stored["versatile_thermostat.heating_orchestration.ENTRY"]
    assert stored["version"] == 2 and stored["data"]["command"]["status"] == "pending" and stored["data"]["command"]["mode"] == "off", stored
    assert not ctx.hass.point_timers or all(not t[2] for t in ctx.hass.point_timers)
    ctx2 = await make(off_world(), stored=ctx.hass.stored, now=ctx.clock.now)
    a = attrs(ctx2)
    assert a["command_status"] == "unconfirmed" and a["last_confirmation_at"] is None and a["control_status"] == "settling", a
    assert calls(ctx2) == []
    await ctx2.mgr.async_stop()


@scenario
async def s08_off_replaces_a_heat_still_in_flight():
    ctx = await make(heat_world())
    gate = asyncio.Event()
    started = []

    async def slow(call):
        started.append(call["data"]["hvac_mode"])
        if call["data"]["hvac_mode"] == "heat":
            await gate.wait()

    ctx.hass.services.handler = slow
    await ctx.world.advance(30)
    assert started == ["heat"], started
    assert ctx.mgr.command_record.status == "sending"
    # while the heat call is in flight the master turns heat by itself and no radiator is valid: M1 wants off
    ctx.hass.states.async_set(COUNT, "0", {"active_device_ids": []})
    ctx.hass.states.async_set(MASTER, "heat", MODES)
    await h.run_loop(20)
    assert started == ["heat", "off"], started
    rec = ctx.mgr.command_record
    assert rec.mode == "off" and rec.status in ("sending", "pending") and rec.attempts == 1, rec
    gate.set()
    await h.run_loop(20)
    assert ctx.mgr.command_record.mode == "off"
    kinds = [m.split("|")[2] + ":" + m.split("|")[3] for m in logbook(ctx)]
    assert kinds[:3] == ["sent:heat", "superseded:heat", "sent:off"], kinds
    await ctx.mgr.async_stop()


@scenario
async def s09_off_never_waits_for_a_slow_save_heat_waits_five_seconds():
    ctx = await make(off_world())

    async def slow_save(data):
        await asyncio.sleep(3)

    ctx.hass.save_hook = slow_save
    await ctx.world.advance(30)
    await h.run_loop(5)
    assert calls(ctx) == [(MASTER, "off")], "the OFF did not wait for the save"
    await ctx.mgr.async_stop()

    mod.HEAT_PERSIST_WAIT_S = 0.2
    try:
        ctx = await make(heat_world())
        ctx.hass.save_hook = slow_save
        await ctx.world.advance(30)
        assert calls(ctx) == []  # heat is waiting for the save
        await asyncio.sleep(0.4)
        await h.run_loop(10)
        assert calls(ctx) == [(MASTER, "heat")], calls(ctx)
        assert ctx.mgr.attributes["last_error"] in ("command_save_timeout", None)
        await ctx.mgr.async_stop()
    finally:
        mod.HEAT_PERSIST_WAIT_S = 5.0


@scenario
async def s10_heat_goes_on_when_the_save_fails():
    ctx = await make(heat_world())

    async def broken(data):
        raise mod.HomeAssistantError("disk full")

    ctx.hass.save_hook = broken
    await ctx.world.advance(30)
    assert calls(ctx) == [(MASTER, "heat")], calls(ctx)
    assert attrs(ctx)["last_error"] == "command_save_failed", attrs(ctx)["last_error"]
    await ctx.mgr.async_stop()


@scenario
async def s11_revalidation_abandons_a_stale_action():
    ctx = await make(off_world())
    real = mod.evaluate_boiler

    def changing(snapshot, cycle, now, *a, **k):
        decision = real(snapshot, cycle, now, *a, **k)
        task = asyncio.current_task()
        if task is not None and task.get_name().startswith("vtherm boiler command"):
            return dataclasses.replace(decision, action="none", reason="no_demand")
        return decision

    counter = []

    def counting(*a, **k):
        counter.append(1)
        return changing(*a, **k)

    mod.evaluate_boiler = counting
    try:
        await ctx.world.advance(30)
        before = len(counter)
        await h.run_loop(300)  # a hot loop would evaluate at every turn (review M2-B I-3)
        assert len(counter) - before <= 4, len(counter) - before
    finally:
        mod.evaluate_boiler = real
    assert calls(ctx) == [] and attrs(ctx)["command_attempts"] == 0 and ctx.mgr.command_record is None
    # the next poll decides again, with the real M1
    await ctx.world.advance(15)
    assert calls(ctx) == [(MASTER, "off")], calls(ctx)
    await ctx.mgr.async_stop()


@scenario
async def s12_publication_only_on_significant_change_or_heartbeat():
    ctx = await make(world())
    start = len(ctx.pub)
    gen0 = ctx.mgr.generation
    await ctx.world.advance(15)
    assert ctx.mgr.generation == gen0, "unchanged evaluation younger than the heartbeat is not published"
    await ctx.world.advance(10)  # 25 s: exact heartbeat
    assert ctx.mgr.generation == gen0 + 1 and attrs(ctx)["evaluated_at"] == ctx.clock.now
    # a new sample alone does not republish
    a = dict(ctx.hass.states.get("climate.vt_living").attributes)
    a["current_temperature"] = 18.01
    ctx.hass.states.async_set("climate.vt_living", "heat", a)
    await h.run_loop()
    assert ctx.mgr.generation == gen0 + 1
    # a significant change does
    ctx.hass.states.async_set(MASTER, "heat", MODES)
    await h.run_loop()
    assert ctx.mgr.generation == gen0 + 2 and attrs(ctx)["master_mode"] == "heat"
    await ctx.mgr.async_stop()


@scenario
async def s13_startup_error_ttl_and_error_priority():
    ctx = await make(world(**{LEGACY: {"state": "garbage"}}))
    assert attrs(ctx)["last_error"] == "legacy_seed_invalid", attrs(ctx)["last_error"]
    await ctx.world.advance(61)
    assert attrs(ctx)["last_error"] is None
    await ctx.mgr.async_stop()
    ctx = await make(world(**{LEGACY: {"state": "garbage"}}))
    ctx.hass.config.units.temperature_unit = "°F"
    await ctx.world.advance(31)
    assert attrs(ctx)["last_error"] == "temperature_unit_unsupported", attrs(ctx)["last_error"]
    ctx.hass.config.units.temperature_unit = "°C"
    await ctx.mgr.async_stop()


@scenario
async def s14_store_v1_migration_and_future_version():
    v1 = {"version": 1, "data": {"cycle": {"m": "off", "off": 1, "r": None, "s": []}, "legacy_seeded": True, "first_origin": "legacy_seed"}}
    ctx = await make(world(), stored={"versatile_thermostat.heating_orchestration.ENTRY": v1})
    assert ctx.mgr.cycle_origin == "local_restore" and ctx.mgr.command_record is None and attrs(ctx)["last_error"] is None
    await ctx.mgr.async_stop()
    v3 = {"version": 3, "data": {"cycle": {}}}
    ctx = await make(world(), stored={"versatile_thermostat.heating_orchestration.ENTRY": v3})
    assert attrs(ctx)["last_error"] == "store_load_failed" and ctx.mgr.is_running
    await ctx.mgr.async_stop()


@scenario
async def s15_unsupported_hvac_mode_is_a_failure_without_a_call():
    ctx = await make(off_world(**{MASTER: {"state": "heat", "attributes": {"hvac_modes": ["heat"]}}}))
    await ctx.world.advance(30)
    assert calls(ctx) == []
    a = attrs(ctx)
    assert a["command_status"] == "failed" and a["last_error"] == "hvac_mode_unsupported", a
    await ctx.mgr.async_stop()


@scenario
async def s16_a_failing_publisher_and_a_failing_compute_do_not_stop_the_manager():
    ctx = await make(world())

    armed = []

    def broken(state, attributes):
        if armed:
            raise RuntimeError("bug")

    ctx.mgr.attach(broken)
    armed.append(1)
    ctx.hass.states.async_set(MASTER, "heat", MODES)
    await h.run_loop()
    assert ctx.mgr.is_running and MASTER in ctx.mgr.watched_entity_ids and attrs(ctx)["master_mode"] == "heat"
    real = mod.collect_boiler_inputs

    def failing(*a, **k):
        raise AssertionError("boom")

    mod.collect_boiler_inputs = failing
    try:
        await ctx.world.advance(15)
    finally:
        mod.collect_boiler_inputs = real
    assert ctx.mgr.state == "error" and attrs(ctx)["last_error"] == "evaluation_error:AssertionError", (ctx.mgr.state, attrs(ctx)["last_error"])
    await ctx.world.advance(15)
    assert ctx.mgr.state != "error"
    await ctx.mgr.async_stop()


@scenario
async def s17_shutdown_sends_nothing_and_reload_settles_again():
    ctx = await make(off_world())
    ctx.hass.is_stopping = True
    await ctx.world.advance(60)
    assert calls(ctx) == []
    await ctx.mgr.async_stop()
    # a reload: new manager, the settling starts again
    ctx = await make(off_world())
    assert not ctx.mgr.is_settled
    await ctx.world.advance(15)
    assert calls(ctx) == []
    await ctx.world.advance(15)
    assert calls(ctx) == [(MASTER, "off")]
    await ctx.mgr.async_stop()


@scenario
async def s18_observe_never_calls_a_service():
    sec = tcol.collector.parse_orchestration_config  # just to keep the import used
    obs = json.loads(json.dumps(tcol.SECTION))
    ctx = await make(off_world(), sec=obs)
    for _ in range(10):
        await ctx.world.advance(15)
    assert calls(ctx) == [] and attrs(ctx)["mode"] == "observe" and attrs(ctx)["owner"] == "observer" and attrs(ctx)["control_status"] == "inactive"
    await ctx.mgr.async_stop()


@scenario
async def s19_stop_cancels_timers_and_ignores_late_callbacks():
    ctx = await make(off_world())
    await ctx.world.advance(30)
    await ctx.mgr.async_stop()
    assert not ctx.mgr.is_running and ctx.mgr._send_task is None
    gen = ctx.mgr.generation
    ctx.hass.states.async_set(MASTER, "off", MODES)
    await h.run_loop()
    ctx.mgr.async_request_evaluation()
    await h.run_loop()
    assert ctx.mgr.generation == gen
    live = [t for t in ctx.hass.point_timers if t[2]] + [t for t in ctx.hass.interval_timers if t[2]]
    assert live == [], live
    assert ctx.hass.state_listeners == []


@scenario
async def s20_exhausted_off_is_released_when_the_master_goes_unavailable():
    ctx = await make(off_world())

    async def boom(call):
        raise RuntimeError("boom")

    ctx.hass.services.handler = boom
    await ctx.world.advance(30)
    for wait in (61, 121, 301):
        await ctx.world.advance(wait)
    assert len(calls(ctx)) == 4 and attrs(ctx)["control_status"] == "command_failed", (len(calls(ctx)), attrs(ctx)["control_status"])
    await ctx.world.advance(600)
    assert len(calls(ctx)) == 4
    ctx.hass.states.async_set(MASTER, "unavailable")
    await h.run_loop()
    await ctx.world.advance(40)
    ctx.hass.states.async_set(MASTER, "heat", MODES)
    await h.run_loop()
    assert len(calls(ctx)) == 4
    await ctx.world.advance(61)
    assert len(calls(ctx)) == 5, len(calls(ctx))  # a fresh budget after the short wait
    await ctx.mgr.async_stop()


@scenario
async def s21_flapping_action_keeps_the_budget_per_mode():
    ctx = await make(off_world())

    async def boom(call):
        raise RuntimeError("boom")

    ctx.hass.services.handler = boom
    await ctx.world.advance(30)
    for index in range(100):  # the valve count flaps: off is wanted, then nothing, every 2 s
        valid = index % 2 == 0
        ids = [] if valid else ["climate.v_bed", "climate.v_liv_1", "climate.v_liv_2"]
        ctx.hass.states.async_set(COUNT, str(len(ids)), {"active_device_ids": ids})
        await ctx.world.advance(2)
    assert len(calls(ctx)) <= 4, len(calls(ctx))
    await ctx.mgr.async_stop()


@scenario
async def s22_a_service_in_flight_is_not_cancelled_when_the_action_becomes_none():
    ctx = await make(off_world())
    gate = asyncio.Event()
    outcome = []

    async def slow(call):
        try:
            await gate.wait()
            outcome.append("done")
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise

    ctx.hass.services.handler = slow
    await ctx.world.advance(30)
    assert ctx.mgr.command_record.status == "sending"
    ids = ["climate.v_bed", "climate.v_liv_1", "climate.v_liv_2"]
    ctx.hass.states.async_set(COUNT, "3", {"active_device_ids": ids})  # radiators valid again: action none
    await h.run_loop(20)
    assert ctx.mgr.command_record.status == "superseded" and outcome == []
    gate.set()
    await h.run_loop(20)
    assert outcome == ["done"] and ctx.mgr.command_record.status == "superseded"
    await ctx.mgr.async_stop()


@scenario
async def s23_release_gate_refuses_a_stored_control_section():
    """With the shipped gate (control unavailable) a confirmed control section never commands."""
    # Exercise the closed gate explicitly, also when a reviewed release opens it.
    mod.BOILER_CONTROL_AVAILABLE = False
    try:
        ctx = await make(off_world())
        for _ in range(10):
            await ctx.world.advance(15)
        assert calls(ctx) == [], calls(ctx)
        assert ctx.mgr.mode == "disabled", ctx.mgr.mode
        assert "control_not_available" in str(attrs(ctx).get("last_error")), attrs(ctx)
        await ctx.mgr.async_stop()
    finally:
        mod.BOILER_CONTROL_AVAILABLE = True


@scenario
async def s24_restart_preserves_the_spent_budget():
    ctx = await make(off_world())
    await ctx.world.advance(30)
    assert len(calls(ctx)) == 1
    await ctx.mgr.async_stop()
    stored = ctx.hass.stored["versatile_thermostat.heating_orchestration.ENTRY"]
    assert stored["data"]["command"]["attempts"] == 1
    ctx2 = await make(off_world(), stored=ctx.hass.stored, now=ctx.clock.now)
    assert ctx2.mgr._budget and ctx2.mgr._budget[0].attempts == 1
    await ctx2.world.advance(30)
    assert calls(ctx2) == [], "restart must retain the backoff of the first send"
    await ctx2.mgr.async_stop()


@scenario
async def s25_surviving_sensor_reattaches_after_failed_unload():
    ctx = await make(off_world())
    pushes = []
    detach = ctx.mgr.attach(lambda state, attrs: pushes.append(state), "sensor.boiler")
    await ctx.mgr.async_stop()
    replacement = mod.FeatureHeatingOrchestrationManager(ctx.hass, "ENTRY", section(), clock=ctx.clock)
    replacement.adopt_publishers(ctx.mgr)
    replacement._publish("reattached", {})
    assert pushes[-1] == "reattached"
    detach()
    replacement._publish("detached", {})
    assert pushes[-1] == "reattached"
    await replacement.async_stop()


@scenario
async def s26_master_lost_after_persistence_is_never_commanded():
    ctx = await make(heat_world())
    persist = ctx.mgr._async_persist_before_send

    async def lose_master(mode):
        await persist(mode)
        if mode == "heat":
            previous = ctx.hass.states.get(MASTER)
            ctx.hass.states.async_set(MASTER, "unavailable", previous.attributes)

    ctx.mgr._async_persist_before_send = lose_master
    await ctx.world.advance(30)
    await h.run_loop(16)
    assert ctx.hass.states.get(MASTER).state == "unavailable"
    assert calls(ctx) == []
    await ctx.mgr.async_stop()


async def main():
    failed = 0
    for func in results:
        try:
            await func()
            print("ok  ", func.__name__)
        except Exception:  # noqa: BLE001
            failed += 1
            print("FAIL", func.__name__)
            traceback.print_exc()
    print(f"{len(results) - failed}/{len(results)} scenarios passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
