"""Behavioral regressions for completion of the PDC control (offline plant)."""

import asyncio
import ast
import copy
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from .test_heating_pdc_runtime import CARLO_PDC, CARLO_RAD, CARLO_SPLIT, _PortsView, make, rt_mod, run, run_until_running, tr


@pytest.mark.parametrize("owner", ["on", "unknown", "unavailable", None])
def test_every_service_rechecks_the_owner_even_after_a_previous_command(owner):
    async def scenario():
        plant, runtime = make()
        room = runtime._rooms["carlo"]
        assert await runtime._command(room, "climate", "set_temperature", CARLO_PDC, {"temperature": 20})
        if owner is None:
            del plant.states["automation.chooser_carlo"]
        else:
            plant.set("automation.chooser_carlo", owner)
        before = len(plant.calls)
        assert not await runtime._command(room, "climate", "set_hvac_mode", CARLO_SPLIT, {"hvac_mode": "off"})
        assert len(plant.calls) == before
        assert room.blocked_by == ("automation.chooser_carlo",)

    run(scenario())


def test_mirror_retries_failed_writes_and_repairs_external_changes():
    async def scenario():
        plant, runtime = make()
        await run_until_running(plant)
        helper = "input_boolean.managed_carlo"
        plant.fail_calls.add((helper, "turn_on"))
        plant.set(helper, "off")
        await plant.settle()
        assert plant.states[helper]["state"] == "off"
        plant.fail_calls.clear()
        await plant.advance(16)
        assert plant.states[helper]["state"] == "on"
        assert runtime.transaction("carlo").inhibition_owned

    run(scenario())


def test_mirror_rechecks_interlock_in_the_scheduled_task():
    async def scenario():
        plant, runtime = make()
        room = runtime._rooms["carlo"]
        room.tx = replace(room.tx, phase=tr.PHASE_RUNNING)
        runtime._mirror_managed(room)
        plant.set("automation.chooser_carlo", "on")
        await plant.settle()
        assert plant.calls == []
        assert plant.states["input_boolean.managed_carlo"]["state"] == "off"

    run(scenario())


def test_scheduled_mirror_drops_a_value_that_is_no_longer_desired():
    async def scenario():
        plant, runtime = make()
        room = runtime._rooms["carlo"]
        room.tx = replace(room.tx, phase=tr.PHASE_RUNNING)
        runtime._mirror_managed(room)
        room.tx = replace(room.tx, phase=tr.PHASE_IDLE)
        await plant.settle()
        assert plant.calls == []
        assert plant.states["input_boolean.managed_carlo"]["state"] == "off"

    run(scenario())


@pytest.mark.parametrize("helper_state", ["unknown", "unavailable", None])
def test_unreadable_helper_is_not_acknowledged_as_a_successful_mirror(helper_state):
    async def scenario():
        plant, runtime = make()
        helper = "input_boolean.managed_carlo"
        if helper_state is None:
            del plant.states[helper]
        else:
            plant.set(helper, helper_state)
        room = runtime._rooms["carlo"]
        room.tx = replace(room.tx, phase=tr.PHASE_RUNNING)
        runtime._mirror_managed(room)
        await plant.settle()
        assert room.mirrored_managed is None
        assert room.last_error == f"mirror_unavailable:{helper}"
        assert plant.calls == []

    run(scenario())


def test_mirror_requires_observed_confirmation_and_compensates_a_late_on():
    async def scenario():
        plant, runtime = make()
        await run_until_running(plant)
        helper = "input_boolean.managed_carlo"
        entered, finish = asyncio.Event(), asyncio.Event()
        original = plant.call_service

        async def delayed(domain, service, data):
            if data["entity_id"] == helper and service == "turn_on":
                entered.set()
                await finish.wait()
            await original(domain, service, data)

        plant.call_service = delayed
        plant.set(helper, "off")
        await entered.wait()
        room = runtime._rooms["carlo"]
        room.tx = replace(room.tx, phase=tr.PHASE_IDLE)
        runtime._mirror_managed(room)
        finish.set()
        await plant.settle()
        assert plant.states[helper]["state"] == "off"

    run(scenario())


def test_successful_helper_call_without_state_confirmation_is_retried():
    async def scenario():
        plant, runtime = make()
        room = runtime._rooms["carlo"]
        room.tx = replace(room.tx, phase=tr.PHASE_RUNNING)
        calls = []

        async def no_effect(domain, service, data):
            calls.append((domain, service, data))

        plant.call_service = no_effect
        runtime._mirror_managed(room)
        await plant.settle()
        assert room.mirrored_managed is None and room.last_error.startswith("mirror_not_confirmed")
        runtime._mirror_managed(room)
        await plant.settle()
        assert len(calls) == 2

    run(scenario())


def test_release_service_success_without_state_change_keeps_ownership():
    async def scenario():
        plant, runtime = make()
        await run_until_running(plant)

        async def no_effect(entity_id):
            plant.log.append(("unset_without_confirmation", entity_id))

        plant.unset_inhibition = no_effect
        plant.set("sensor.temp_carlo", "20")
        await plant.advance(50)
        assert runtime.transaction("carlo").phase == tr.PHASE_RELEASING
        assert runtime.transaction("carlo").inhibition_owned
        assert plant.inhibited() is True
        assert plant.states["input_boolean.managed_carlo"]["state"] == "on"
        assert sum(item[0] == "unset_without_confirmation" for item in plant.log) >= 2

    run(scenario())


def test_unknown_inhibition_at_the_last_read_is_never_acquired():
    async def scenario():
        plant, runtime = make()
        await plant.start()

        def lose_inhibition_state():
            plant.set(CARLO_RAD, specific_states={"inhibition": {"inhibited": "unknown"}})

        plant.save_hook = lose_inhibition_state
        await plant.advance(200)
        assert not any(item[0] == "inhibit" for item in plant.log)
        assert not runtime.transaction("carlo").inhibition_owned

    run(scenario())


@pytest.mark.parametrize(
    "stored",
    [
        {"schema_version": 9},
        {"schema_version": True},
        {"schema_version": 1, "rooms": {"carlo": {"transaction": {"phase": "invalid"}}}},
        {"schema_version": 1, "rooms": {}, "suspension": {"active": "unknown"}},
        {"schema_version": 1, "rooms": {"carlo": {"override": {"active": "unknown"}}}},
    ],
)
def test_corrupt_store_blocks_effects_and_is_not_overwritten(stored):
    async def scenario():
        plant, runtime = make(stored=stored)
        await plant.start()
        await plant.advance(250)
        assert runtime.attributes("carlo")[0] == "error"
        assert plant.calls == [] and plant.saves == 0
        assert not any(item[0] in ("inhibit", "unset") for item in plant.log)
        await runtime.async_stop()
        assert plant.saves == 0

    run(scenario())


@pytest.mark.parametrize(
    "phase",
    [
        tr.PHASE_WAITING_RADIATOR,
        tr.PHASE_WAITING_TARGET,
        tr.PHASE_WAITING_HEAT,
        tr.PHASE_RETURNING,
        tr.PHASE_RETURNING_DIRECT,
        tr.PHASE_RETURN_UNCONFIRMED,
        tr.PHASE_RELEASING,
        tr.PHASE_OVERRIDE_HOLD,
        tr.PHASE_RUNNING,
    ],
)
def test_restart_in_each_persisted_phase_rechecks_real_off_before_release(phase):
    async def scenario():
        plant, runtime = make()
        await run_until_running(plant)
        stored = copy.deepcopy(plant.stored)
        stored["rooms"]["carlo"]["transaction"]["phase"] = phase
        await runtime.async_stop()
        plant.states[CARLO_SPLIT]["state"] = "unavailable"
        plant.states[CARLO_PDC]["state"] = "unavailable"
        plant.fail_calls.add((CARLO_PDC, "set_hvac_mode"))
        plant.split_obeys = plant.split_follows = False
        plant.log.clear()
        before = len(plant.calls)
        plant.runtime = rt_mod.PdcRuntime(plant.config, _PortsView(plant), stored=stored, allow_control=True)
        await plant.start()
        await plant.advance(190)
        assert plant.runtime.transaction("carlo").inhibition_owned
        assert not any(item[0] == "unset" for item in plant.log)
        assert not any(data.get("hvac_mode") == "heat" for _, _, data in plant.calls[before:])
        await plant.runtime.async_stop()

    run(scenario())


def test_stop_cancels_pending_classification_and_mirror_tasks():
    async def scenario():
        plant, runtime = make()
        await plant.start()
        plant.remote_control("carlo", "heat")
        assert runtime._rooms["carlo"].classify_cancels
        await runtime.async_stop()
        assert runtime._rooms["carlo"].classify_cancels == []
        assert runtime._rooms["carlo"].mirror_workers == {}
        before = len(plant.calls)
        await plant.advance(5)
        assert len(plant.calls) == before

    run(scenario())


def test_other_room_still_owned_by_legacy_is_not_assumed_idle_from_local_state():
    async def scenario():
        plant, runtime = make(rooms=("carlo", "diego"), owners={"carlo": [], "diego": ["automation.chooser_diego"]})
        plant._add("automation.chooser_diego", "on")
        plant.set("input_boolean.managed_diego", "on")
        await plant.start()
        await plant.advance(250)
        assert runtime.transaction("carlo").phase == tr.PHASE_IDLE
        assert runtime.attributes("diego")[0] == "blocked"
        assert not any(item[0] == "inhibit" for item in plant.log)
        assert runtime._collect(runtime._rooms["carlo"], plant.clock).snapshot.other_managed == "on"

    run(scenario())


def test_blocked_room_with_native_transaction_still_counts_as_busy_before_its_helper_is_written():
    plant, runtime = make(rooms=("carlo", "diego"), owners={"carlo": [], "diego": ["automation.chooser_diego"]})
    plant._add("automation.chooser_diego", "on")
    diego = runtime._rooms["diego"]
    diego.tx = replace(diego.tx, phase=tr.PHASE_RUNNING)
    assert plant.states["input_boolean.managed_diego"]["state"] == "off"
    assert runtime._collect(runtime._rooms["carlo"], plant.clock).snapshot.other_managed == "on"


def test_split_telemetry_does_not_delay_protections_for_a_running_room():
    async def scenario():
        plant, runtime = make()
        await run_until_running(plant)
        await plant.advance(2)
        plant.set(CARLO_SPLIT, current_temperature=21.5)
        assert runtime._rooms["carlo"].classify_pending == 0
        plant.set("select.central_mode", "Stopped")
        await plant.settle()
        assert any(call[0] == "climate" and call[2].get("hvac_mode") == "off" for call in plant.calls)

    run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("select.central_mode", "Stopped"),
        ("input_boolean.auto_carlo", "off"),
        ("sensor.power", "unavailable"),
        ("sensor.power", "2800"),
        ("sensor.temp_carlo", "unavailable"),
        ("binary_sensor.window_carlo", "on"),
    ],
)
def test_running_protections_return_safely_before_the_ordinary_minimum(field, value):
    async def scenario():
        plant, runtime = make()
        await run_until_running(plant)
        plant.set(field, value)
        await plant.advance(180)
        assert plant.states[CARLO_SPLIT]["state"] == "off"
        assert plant.states[CARLO_PDC]["state"] == "off"
        assert not runtime.transaction("carlo").inhibition_owned

    run(scenario())


def test_program_off_stops_a_running_session_even_before_its_ordinary_minimum():
    async def scenario():
        plant, runtime = make()
        await run_until_running(plant)
        plant.set(CARLO_RAD, requested_state={"hvac_mode": "off"})
        await plant.advance(40)
        assert plant.states[CARLO_SPLIT]["state"] == "off"
        assert not runtime.transaction("carlo").inhibition_owned

    run(scenario())


@pytest.mark.parametrize("phase", tr.ALL_PHASES)
def test_user_override_prevents_automatic_heat_in_every_phase(phase):
    async def scenario():
        plant, runtime = make()
        await run_until_running(plant)
        room = runtime._rooms["carlo"]
        room.tx = replace(room.tx, phase=phase)
        before = len(plant.calls)
        plant.user_call(CARLO_SPLIT, "set_temperature", temperature=24)
        await plant.settle()
        await plant.advance(3)
        assert runtime.override("carlo").active
        assert not any(data.get("hvac_mode") == "heat" for _, _, data in plant.calls[before:])
        assert plant.inhibited() is True

    run(scenario())


def test_future_transaction_clock_blocks_recovery_instead_of_extending_the_session():
    async def scenario():
        plant, runtime = make()
        await run_until_running(plant)
        stored = copy.deepcopy(plant.stored)
        stored["rooms"]["carlo"]["transaction"]["started_at"] = plant.clock + 100
        other, recovered = make(stored=stored)
        await other.start()
        assert "transaction_clock_invalid:carlo" in recovered.load_errors
        assert recovered.attributes("carlo")[0] == "error"
        assert other.calls == [] and other.saves == 0

    run(scenario())


def test_a_stored_transaction_in_the_future_recovers_once_the_clock_is_consistent():
    async def scenario():
        plant, runtime = make()
        await run_until_running(plant)
        stored = copy.deepcopy(plant.stored)
        other, recovered = make(stored=stored)
        stored["rooms"]["carlo"]["transaction"]["started_at"] = other.clock + 100
        other, recovered = make(stored=stored)
        await other.start()
        await other.advance(20)
        assert "transaction_clock_invalid:carlo" in recovered.load_errors
        assert other.calls == [] and other.saves == 0
        assert ("pdc_store_unreadable", "Pompa di calore: stato salvato non utilizzabile") in other.notifications
        # the host clock catches up: the error clears by itself and the safe recovery path runs
        await other.advance(150)
        assert recovered.load_errors == ()
        assert ("pdc_store_unreadable", None) in other.notifications
        assert recovered.attributes("carlo")[0] != "error"

    run(scenario())


def test_an_unreadable_store_notifies_and_is_never_overwritten():
    async def scenario():
        plant, runtime = make()
        await run_until_running(plant)
        stored = copy.deepcopy(plant.stored)
        stored["schema_version"] = 999
        other, recovered = make(stored=stored)
        await other.start()
        await other.advance(120)
        assert recovered.load_errors == ("store_invalid",)
        assert sum(1 for item in other.notifications if item[0] == "pdc_store_unreadable") == 1  # notified once, not every pass
        assert other.calls == [] and other.saves == 0

    run(scenario())


def test_a_radiator_that_cannot_be_released_keeps_its_error_and_notifies_the_operator():
    async def scenario():
        plant, runtime = make()
        await run_until_running(plant)
        plant.fail_unset = 50  # every release attempt raises
        plant.set(OWNER, "off")
        plant.set("binary_sensor.window_carlo", "on")  # forces the return, then the release
        waited = 0.0
        while runtime.transaction("carlo").phase != tr.PHASE_RELEASING and waited < 400:
            await plant.advance(5)
            waited += 5
        assert runtime.transaction("carlo").phase == tr.PHASE_RELEASING
        await plant.advance(200)
        tx = runtime.transaction("carlo")
        assert tx.phase == tr.PHASE_RELEASING and tx.inhibition_owned  # never released blindly
        state, attributes = runtime.attributes("carlo")
        assert "release_failed" in str(attributes.get("last_error") or attributes), "the cause must stay visible on every pass"
        assert any(item[0] == "carlo_pdc_release_stuck" and item[1] for item in plant.notifications)
        # once the radiator answers, the session ends and the notification is dismissed
        plant.fail_unset = 0
        await plant.advance(120)
        assert runtime.transaction("carlo").phase == tr.PHASE_IDLE and plant.inhibited() is False
        assert ("carlo_pdc_release_stuck", None) in plant.notifications

    run(scenario())


def test_manual_sync_never_copies_stale_heat_after_a_new_split_off():
    async def scenario():
        plant, runtime = make()
        room = runtime._rooms["carlo"]
        room.override = rt_mod.start_override(room.override, plant.clock, rt_mod.SOURCE_USER_COMMAND, rt_mod.PROVENANCE_USER)
        plant.set(CARLO_SPLIT, "heat", temperature=24)
        collection = runtime._collect(room, plant.clock)
        original = plant.call_service

        async def change_during_target(domain, service, data):
            await original(domain, service, data)
            plant.set(CARLO_SPLIT, "off")

        plant.call_service = change_during_target
        await runtime._sync_pdc(room, collection, plant.clock)
        assert not any(data.get("hvac_mode") == "heat" for _, _, data in plant.calls)
        assert plant.states[CARLO_PDC]["state"] == "off"

    run(scenario())


def _cleanup_function(registry):
    """Execute the actual registry cleanup function without importing HA's platforms."""
    source = Path(__file__).resolve().parents[1] / "custom_components/versatile_thermostat/sensor.py"
    node = next(node for node in ast.parse(source.read_text(encoding="utf-8")).body if isinstance(node, ast.FunctionDef) and node.name == "_cleanup_removed_pdc_sensors")
    node.decorator_list = []
    node.returns = None
    for arg in node.args.args:
        arg.annotation = None
    namespace = {
        "CONF_HEATING_ORCHESTRATION": "heating_orchestration",
        "CONF_HO_PDC_RUNTIME": "pdc_runtime",
        "HO_PDC_SENSOR_SUFFIX": "_heating_orchestration_pdc_",
        "DOMAIN": "versatile_thermostat",
        "PDC_ROOM_KEY_PATTERN": re.compile(r"^[a-z][a-z0-9_]*$"),
        "er": SimpleNamespace(async_get=lambda hass: registry, async_entries_for_config_entry=lambda reg, key: reg.entries),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
    return namespace[node.name]


@pytest.mark.parametrize("rooms", [[], [{"room_key": "carlo"}], "malformed", [{"room_key": 12}]])
def test_registry_cleanup_removes_only_obsolete_pdc_sensors_of_this_entry(rooms):
    prefix = "central_heating_orchestration_pdc_"
    entries = [
        SimpleNamespace(entity_id="sensor.old", domain="sensor", platform="versatile_thermostat", unique_id=prefix + "old"),
        SimpleNamespace(entity_id="sensor.carlo", domain="sensor", platform="versatile_thermostat", unique_id=prefix + "carlo"),
        SimpleNamespace(entity_id="climate.keep", domain="climate", platform="versatile_thermostat", unique_id=prefix + "keep"),
        SimpleNamespace(entity_id="sensor.other", domain="sensor", platform="versatile_thermostat", unique_id="other" + prefix),
    ]
    removed = []
    registry = SimpleNamespace(entries=entries, async_remove=removed.append)
    entry = SimpleNamespace(entry_id="central", data={"heating_orchestration": {"pdc_runtime": {"rooms": rooms}}})
    _cleanup_function(registry)(None, entry)
    expected = ["sensor.old", "sensor.carlo"] if rooms == [] else (["sensor.old"] if rooms == [{"room_key": "carlo"}] else [])
    assert removed == expected


# --- T3: tolerance for an unavailable owner, then safety OFF only ------------------------------

OWNER = "automation.chooser_carlo"


def _offs(plant):
    return [c for c in plant.calls if c[1] == "set_hvac_mode" and c[2].get("hvac_mode") == "off"]


@pytest.mark.parametrize("state", ["unavailable", "unknown", None])
def test_unavailable_owner_is_tolerated_for_30_seconds_then_only_the_safety_off_is_sent(state):
    async def scenario():
        plant, runtime = make()
        await run_until_running(plant)
        before = len(plant.calls)
        if state is None:
            del plant.states[OWNER]
        else:
            plant.set(OWNER, state)
        await plant.advance(29)
        assert len(plant.calls) == before  # inside the tolerance nothing is sent
        assert runtime.transaction("carlo").phase == tr.PHASE_RUNNING
        await plant.advance(8)
        sent = plant.calls[before:]
        assert sent and all(c[0] == "climate" and c[1] == "set_hvac_mode" and c[2]["hvac_mode"] == "off" for c in sent)
        assert {c[2]["entity_id"] for c in sent} <= {CARLO_PDC, CARLO_SPLIT}
        assert plant.states[CARLO_PDC]["state"] == "off" and plant.states[CARLO_SPLIT]["state"] == "off"
        assert not any(entry[0] == "unset" for entry in plant.log)  # the radiator is never released while blocked
        assert plant.inhibited() is True
        assert ("carlo_pdc_owner_unavailable", "Pompa di calore: proprietario esterno non disponibile") in plant.notifications
        # the owner comes back: the notification goes away and the session returns through the safe path
        plant.set(OWNER, "off")
        await plant.advance(120)
        assert ("carlo_pdc_owner_unavailable", None) in plant.notifications
        assert runtime.transaction("carlo").phase == tr.PHASE_IDLE and plant.inhibited() is False

    run(scenario())


def test_an_owner_that_is_on_is_never_overridden_by_the_tolerance():
    async def scenario():
        plant, runtime = make()
        await run_until_running(plant)
        before = len(plant.calls)
        plant.set(OWNER, "on")
        await plant.advance(300)
        assert len(plant.calls) == before and runtime.attributes("carlo")[0] == "blocked"

    run(scenario())


def test_an_unavailable_owner_that_turns_on_before_the_end_of_the_tolerance_stays_frozen():
    async def scenario():
        plant, runtime = make()
        await run_until_running(plant)
        before = len(plant.calls)
        plant.set(OWNER, "unavailable")
        await plant.advance(20)
        plant.set(OWNER, "on")
        await plant.advance(120)
        assert len(plant.calls) == before

    run(scenario())


def test_a_flapping_owner_restarts_the_tolerance_clock():
    async def scenario():
        plant, runtime = make()
        await run_until_running(plant)
        before = len(plant.calls)
        for _ in range(3):
            plant.set(OWNER, "unavailable")
            await plant.advance(20)
            plant.set(OWNER, "off")
            await plant.advance(2)
        assert len(plant.calls) == before and _offs(plant) == []
        assert runtime.transaction("carlo").phase == tr.PHASE_RUNNING
        assert plant.states[CARLO_PDC]["state"] == "heat"

    run(scenario())


def test_the_tolerance_never_applies_without_a_session_of_this_room():
    async def scenario():
        plant, runtime = make()
        await plant.start()
        plant.set(OWNER, "unavailable")
        plant.set(CARLO_SPLIT, "heat")  # somebody else's heating
        await plant.advance(120)
        assert _offs(plant) == [] and plant.states[CARLO_SPLIT]["state"] == "heat"

    run(scenario())


def test_after_the_tolerance_only_off_commands_pass_the_interlock():
    async def scenario():
        plant, runtime = make()
        await run_until_running(plant)
        room = runtime._rooms["carlo"]
        plant.set(OWNER, "unavailable")
        await plant.advance(40)
        before = len(plant.calls)
        assert not await runtime._command(room, "climate", "set_hvac_mode", CARLO_PDC, {"hvac_mode": "heat"})
        assert not await runtime._command(room, "climate", "set_temperature", CARLO_PDC, {"temperature": 22})
        assert not await runtime._command(room, "climate", "set_hvac_mode", "climate.other_split", {"hvac_mode": "off"})
        assert len(plant.calls) == before
        assert await runtime._command(room, "climate", "set_hvac_mode", CARLO_SPLIT, {"hvac_mode": "off"})

    run(scenario())
