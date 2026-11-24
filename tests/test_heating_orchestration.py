# pylint: disable=wildcard-import, unused-wildcard-import, protected-access, unused-argument, line-too-long, redefined-outer-name

"""Home Assistant lifecycle tests of the boiler orchestration observer (M2-A).

Real Home Assistant (config entries, registry, ``Store``, timers); no service of
any other integration is executed and the observer must not call any. The
clock is controlled through the manager clock hook and
``async_fire_time_changed``: nothing sleeps for real.
"""

import dataclasses
import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.versatile_thermostat import feature_heating_orchestration_manager as manager_module
from custom_components.versatile_thermostat.heating_boiler_collector import SCHEMA_VERSION, cycle_to_dict
from custom_components.versatile_thermostat.sensor import HeatingOrchestrationBoilerSensor
from custom_components.versatile_thermostat.vtherm_central_api import VersatileThermostatAPI

from .commons import *  # pylint: disable=wildcard-import, unused-wildcard-import
from .test_heating_boiler_collector import CENTRAL, COUNT, LEGACY, MASTER, SECTION, base_snapshot, vtherm

SENSOR_SUFFIX = "_heating_orchestration_boiler"
EXPECTED_KEYS = {
    "schema_version",
    "mode",
    "owner",
    "evaluated_at",
    "generation",
    "proposed_action",
    "reason",
    "deadline",
    "master_mode",
    "count_raw",
    "count_raw_valid",
    "valid_valve_count",
    "inputs_ok",
    "inputs",
    "rooms",
    "excluded_rooms",
    "conditions",
    "cycle",
    "cycle_origin",
    "commanded_mode",
    "last_command_at",
    "last_confirmation_at",
    "last_error",
}


class Clock:
    """Manager clock plus the matching ``async_fire_time_changed`` instants."""

    def __init__(self) -> None:
        self.real0 = dt_util.utcnow()
        self.now = self.real0.timestamp()
        self.elapsed = 0.0

    def __call__(self) -> float:
        return self.now

    async def advance(self, hass: HomeAssistant, seconds: float) -> None:
        """Move the observer clock and fire the timers due in the meantime."""
        self.now += seconds
        self.elapsed += seconds
        async_fire_time_changed(hass, self.real0 + timedelta(seconds=self.elapsed))
        await hass.async_block_till_done()


@pytest.fixture(name="clock")
def clock_fixture():
    clock = Clock()
    with patch.object(manager_module, "_utc_clock", clock):
        yield clock


@pytest.fixture(name="service_calls")
def service_calls_fixture(hass):
    """Every attempt to call a service, recorded and still executed."""
    calls: list = []
    original = hass.services.async_call

    async def spy(domain, service, *args, **kwargs):
        calls.append((domain, service, args, kwargs))
        return await original(domain, service, *args, **kwargs)

    with patch.object(hass.services, "async_call", spy):
        yield calls


def put_states(hass: HomeAssistant, snapshot: dict) -> None:
    for entity_id, value in snapshot.items():
        hass.states.async_set(entity_id, value["state"], value.get("attributes") or {})


def world(now: float, **changes) -> dict:
    return base_snapshot(now=now, **changes)


def observe_section(**changes) -> dict:
    section = json.loads(json.dumps(SECTION))
    section.update(changes)
    return section


async def settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done()
    await hass.async_block_till_done()


async def setup_central(hass: HomeAssistant, section, *, title="TheCentralConfigMockName") -> MockConfigEntry:
    data = dict(FULL_CENTRAL_CONFIG)
    if section is not None:
        data[CONF_HEATING_ORCHESTRATION] = section
    entry = MockConfigEntry(domain=DOMAIN, title=title, unique_id="centralConfigUniqueId", data=data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    return entry


def sensor_entity_id(hass: HomeAssistant, entry: MockConfigEntry) -> str | None:
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}{SENSOR_SUFFIX}")


def diagnostic(hass: HomeAssistant, entry: MockConfigEntry):
    entity_id = sensor_entity_id(hass, entry)
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state


def manager_of(hass: HomeAssistant):
    return VersatileThermostatAPI.get_vtherm_api(hass).heating_orchestration_manager


def count_evaluations():
    """Wrap the collector to count the evaluations of the manager."""
    return patch.object(manager_module, "collect_boiler_inputs", wraps=manager_module.collect_boiler_inputs)


# ---------------------------------------------------------------------------
# Existing entries and disabled
# ---------------------------------------------------------------------------


async def test_entry_without_the_section_is_unchanged(hass: HomeAssistant, init_vtherm_api, hass_storage, service_calls, clock):
    entry = await setup_central(hass, None)
    assert manager_of(hass) is None
    assert sensor_entity_id(hass, entry) is None
    assert not [key for key in hass_storage if "heating_orchestration" in key]
    assert not [call for call in service_calls if call[0] != "persistent_notification"]
    assert CONF_HEATING_ORCHESTRATION not in entry.data


async def test_disabled_section_adds_only_the_inert_diagnostic(hass: HomeAssistant, init_vtherm_api, hass_storage, service_calls, clock):
    without = await setup_central(hass, None)
    registry = er.async_get(hass)
    before = {entity.unique_id for entity in er.async_entries_for_config_entry(registry, without.entry_id)}
    await hass.config_entries.async_unload(without.entry_id)
    await hass.config_entries.async_remove(without.entry_id)
    VersatileThermostatAPI.reset_vtherm_api()

    entry = await setup_central(hass, {"mode": "disabled", "master_entity_id": "garbage"})
    manager = manager_of(hass)
    assert manager is not None and manager.mode == "disabled" and not manager.is_running
    assert manager.watched_entity_ids == ()
    state = diagnostic(hass, entry)
    assert state.state == "disabled"
    assert state.attributes["schema_version"] == SCHEMA_VERSION and state.attributes["mode"] == "disabled" and state.attributes["owner"] is None
    assert state.attributes["last_error"] is None
    after = {entity.unique_id for entity in er.async_entries_for_config_entry(registry, entry.entry_id)}
    assert len(after) == len(before) + 1
    assert f"{entry.entry_id}{SENSOR_SUFFIX}" in after
    assert not [key for key in hass_storage if "heating_orchestration" in key]
    assert not [call for call in service_calls if call[0] != "persistent_notification"]


@pytest.mark.parametrize(
    "section, error",
    [
        ({"mode": "control", **{key: value for key, value in SECTION.items() if key != "mode"}}, "config_control_not_available:mode"),
        ({"mode": "Observe"}, "config_unsupported_mode:mode"),
        ({"mode": "observe", "master_entity_id": "switch.not_a_climate"}, "config_invalid_domain:master_entity_id"),
        ({"mode": "observe"}, "config_required:master_entity_id"),
        ("observe", "config_invalid_section"),
    ],
)
async def test_unsupported_or_invalid_section_stays_inactive_with_an_explicit_error(hass: HomeAssistant, init_vtherm_api, hass_storage, service_calls, clock, section, error):
    put_states(hass, world(clock.now))
    entry = await setup_central(hass, section)
    manager = manager_of(hass)
    assert manager is not None and manager.mode == "disabled" and not manager.is_running and manager.watched_entity_ids == ()
    state = diagnostic(hass, entry)
    assert state.state == "error"
    assert state.attributes["mode"] == "disabled" and state.attributes["owner"] is None and state.attributes["last_error"] == error
    assert not [key for key in hass_storage if "heating_orchestration" in key]
    assert not [call for call in service_calls if call[0] != "persistent_notification"]


# ---------------------------------------------------------------------------
# Observe: publication, identity, no actuation
# ---------------------------------------------------------------------------


async def test_observe_publishes_the_schema_v1_diagnostic(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    put_states(hass, world(clock.now))
    entry = await setup_central(hass, observe_section())
    manager = manager_of(hass)
    assert manager.is_running and manager.mode == "observe"

    state = diagnostic(hass, entry)
    attributes = dict(state.attributes)
    assert state.state == attributes["reason"] and state.state not in ("starting", "error", "disabled", "unknown", "unavailable")
    assert set(attributes) >= EXPECTED_KEYS
    assert attributes["schema_version"] == 1 and attributes["mode"] == "observe" and attributes["owner"] == "observer"
    assert attributes["evaluated_at"] == clock.now and attributes["generation"] >= 1
    assert attributes["proposed_action"] in ("none", "heat", "off")
    assert attributes["master_mode"] == "off" and attributes["count_raw"] == 3 and attributes["count_raw_valid"] is True
    assert attributes["valid_valve_count"] == 3 and attributes["inputs_ok"] is True
    assert attributes["inputs"] == {"central_mode": "Heat only", "manual": "off", "pilot": "off", "pilot_ready": True, "consent": True}
    assert [room["room_key"] for room in attributes["rooms"]] == ["bedroom", "living"]
    assert attributes["excluded_rooms"] == []
    assert attributes["commanded_mode"] is None and attributes["last_command_at"] is None and attributes["last_confirmation_at"] is None
    assert attributes["last_error"] is None and attributes["cycle_origin"] in ("legacy_seed", "new_observation")
    assert all(set(condition) == {"id", "status", "reason_code", "actual", "expected", "deadline", "unit"} for condition in attributes["conditions"])
    json.dumps(attributes, allow_nan=False)
    assert len(json.dumps(attributes)) < 16_000


async def test_unique_id_entity_id_and_category(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    entry = await setup_central(hass, observe_section())
    registry = er.async_get(hass)
    entity_id = sensor_entity_id(hass, entry)
    assert entity_id is not None
    registered = registry.async_get(entity_id)
    assert registered.unique_id == f"{entry.entry_id}_heating_orchestration_boiler"
    assert registered.entity_category == EntityCategory.DIAGNOSTIC
    assert registered.translation_key == "heating_orchestration_boiler"
    assert registered.device_id is not None and registered.config_entry_id == entry.entry_id
    # Documented in M2-A-ESITO.md: <central device name>_<translated name>.
    assert entity_id == "sensor.centralconfiguration_boiler_orchestration" or entity_id.endswith("_boiler_orchestration"), entity_id
    assert hass.states.get(entity_id).attributes.get("friendly_name", "").endswith("Boiler orchestration")


async def test_recorder_exclusions_cover_the_bulky_attributes():
    excluded = HeatingOrchestrationBoilerSensor._unrecorded_attributes
    assert {"conditions", "rooms", "excluded_rooms", "evaluated_at", "generation", "cycle", "inputs"} <= set(excluded)
    # state (reason) and the short attributes stay recorded
    assert not {"schema_version", "mode", "owner", "proposed_action", "reason", "last_error", "cycle_origin"} & set(excluded)


async def test_observe_never_calls_a_service_nor_writes_the_legacy_entities(hass: HomeAssistant, init_vtherm_api, hass_storage, service_calls, clock):
    put_states(hass, world(clock.now))
    hass.states.async_set(LEGACY, json.dumps(cycle_to_dict(None)))
    legacy_before = hass.states.get(LEGACY)
    seen: list = []
    hass.bus.async_listen("call_service", lambda event: seen.append(event.data))
    entry = await setup_central(hass, observe_section())
    service_calls.clear()
    seen.clear()

    # inputs changing, heating requested, polling, a deadline: still no actuation
    for state in ("on", "off", "unknown"):
        hass.states.async_set(MASTER, state)
        await settle(hass)
    put_states(hass, world(clock.now, **{"climate.vt_living": vtherm(["climate.v_liv_1"], now=clock.now, current=10, target=22)}))
    await settle(hass)
    for _ in range(5):
        await clock.advance(hass, 16)
    await hass.config_entries.async_unload(entry.entry_id)
    await settle(hass)

    assert service_calls == [] and seen == []
    assert hass.states.get(LEGACY) == legacy_before
    assert hass.states.get(MASTER).state == "unknown"  # never commanded


async def test_waits_for_the_real_inputs_before_declaring_observations(hass: HomeAssistant, init_vtherm_api, clock):
    entry = await setup_central(hass, observe_section())
    state = diagnostic(hass, entry)
    assert state.state == "starting"
    assert state.attributes["last_error"].startswith("waiting_for_entities:")
    assert state.attributes["proposed_action"] is None and state.attributes["rooms"] == []

    put_states(hass, world(clock.now))
    await settle(hass)
    state = diagnostic(hass, entry)
    assert state.state not in ("starting", "error") and state.attributes["last_error"] is None
    assert state.attributes["generation"] >= 1


async def test_central_input_unavailable_is_invalid_not_a_zero(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now, **{COUNT: {"state": "unavailable", "attributes": {}}}))
    entry = await setup_central(hass, observe_section())
    attributes = diagnostic(hass, entry).attributes
    assert attributes["count_raw"] is None and attributes["count_raw_valid"] is False and attributes["inputs_ok"] is False
    assert {room["reasons"][0] for room in attributes["excluded_rooms"]} == {"count_invalid"}


async def test_unit_other_than_celsius_invalidates_the_inputs(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry = await setup_central(hass, observe_section())
    attributes = diagnostic(hass, entry).attributes
    assert attributes["inputs_ok"] is False and attributes["last_error"] == "temperature_unit_unsupported"
    assert all("unit_unsupported" in room["reasons"] for room in attributes["excluded_rooms"])


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


async def test_simultaneous_events_are_coalesced_into_one_evaluation(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    entry = await setup_central(hass, observe_section())
    before = diagnostic(hass, entry).attributes["generation"]
    with count_evaluations() as collected:
        for index in range(10):
            hass.states.async_set("climate.v_bed", "heat", {"hvac_action": "heating", "n": index})
            hass.states.async_set(MASTER, "heat" if index % 2 else "off")
            hass.states.async_set("binary_sensor.window_bed", "off", {"n": index})
        await settle(hass)
    assert collected.call_count == 1
    assert manager_of(hass).generation == before + 1


async def test_attribute_only_changes_trigger_an_evaluation(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    await setup_central(hass, observe_section())
    with count_evaluations() as collected:
        attributes = dict(hass.states.get("climate.vt_living").attributes)
        attributes["temperature"] = 21.0
        hass.states.async_set("climate.vt_living", "heat", attributes)
        await settle(hass)
    assert collected.call_count == 1


async def test_unrelated_entities_do_not_trigger_evaluations(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    await setup_central(hass, observe_section())
    with count_evaluations() as collected:
        hass.states.async_set("sensor.unrelated", "1")
        hass.states.async_set("climate.other_room", "heat")
        await settle(hass)
    assert collected.call_count == 0


async def test_recovery_polling_every_15_seconds(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    entry = await setup_central(hass, observe_section())
    start = diagnostic(hass, entry).attributes
    with count_evaluations() as collected:
        await clock.advance(hass, 10)
        assert collected.call_count == 0
        await clock.advance(hass, 6)
        assert collected.call_count == 1
        await clock.advance(hass, 15)
        assert collected.call_count == 2
    attributes = diagnostic(hass, entry).attributes
    assert attributes["generation"] == start["generation"] + 2 and attributes["evaluated_at"] == clock.now
    assert clock.now == start["evaluated_at"] + 31


async def test_one_clock_per_evaluation(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    entry = await setup_central(hass, observe_section())
    calls = []
    real = clock.__call__

    def counting() -> float:
        calls.append(1)
        return real()

    with patch.object(manager_module, "_utc_clock", counting):
        manager_of(hass)._clock = counting
        manager_of(hass).async_request_evaluation()
        await settle(hass)
    # one reading for the evaluation (the save payload reads it again only when persisting)
    assert len(calls) == 1
    attributes = diagnostic(hass, entry).attributes
    assert attributes["evaluated_at"] == clock.now


def _with_deadline(deadline_of_now):
    real = manager_module.evaluate_boiler

    def wrapper(snapshot, cycle, now, *args, **kwargs):
        decision = real(snapshot, cycle, now, *args, **kwargs)
        return dataclasses.replace(decision, deadline=deadline_of_now(now))

    return patch.object(manager_module, "evaluate_boiler", wrapper)


async def test_deadline_already_passed_never_loops(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    with _with_deadline(lambda now: now - 30):
        entry = await setup_central(hass, observe_section())
        manager = manager_of(hass)
        generation = manager.generation
        for _ in range(20):
            await settle(hass)
        assert manager.generation == generation  # no zero-delay re-evaluation
        assert manager._deadline_unsub is None and manager._pending is None
        assert diagnostic(hass, entry).attributes["deadline"] == clock.now - 30
        await clock.advance(hass, 16)
        assert manager.generation == generation + 1  # recovered by the poll only


async def test_future_deadline_schedules_one_timer_and_recomputes(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    with _with_deadline(lambda now: now + 5):
        await setup_central(hass, observe_section())
        manager = manager_of(hass)
        generation = manager.generation
        assert manager._deadline_unsub is not None and manager._deadline_at == clock.now + 5
        clock.now += 6
        async_fire_time_changed(hass, clock.real0 + timedelta(seconds=6))
        await settle(hass)
        assert manager.generation == generation + 1
        # the evaluation moved the deadline to the new now + 5: still a single timer
        assert manager._deadline_unsub is not None and manager._deadline_at == clock.now + 5


async def test_duplicate_updates_with_no_consent_do_not_spin(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now, **{"input_boolean.consent_a": {"state": "off"}}))
    entry = await setup_central(hass, observe_section())
    manager = manager_of(hass)
    generation = manager.generation
    for _ in range(10):
        hass.states.async_set("input_boolean.consent_a", "off")  # identical: no event
        await settle(hass)
    assert manager.generation == generation
    assert diagnostic(hass, entry).attributes["inputs"]["consent"] is False


async def test_own_diagnostic_is_not_listened_to(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    entry = await setup_central(hass, observe_section())
    manager = manager_of(hass)
    entity_id = sensor_entity_id(hass, entry)
    assert entity_id not in manager.watched_entity_ids
    generation = manager.generation
    for _ in range(10):
        await settle(hass)
    assert manager.generation == generation


# ---------------------------------------------------------------------------
# Cycle context: seed, restore, storage
# ---------------------------------------------------------------------------

STORE_KEY = "versatile_thermostat.heating_orchestration."


def storage_key(entry: MockConfigEntry) -> str:
    return f"{STORE_KEY}{entry.entry_id}"


def legacy_cycle(now: float) -> str:
    return json.dumps({"m": "normal", "off": now - 600, "r": now - 120, "s": []})


async def test_legacy_cycle_seeds_once_and_is_never_written(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    put_states(hass, world(clock.now))
    hass.states.async_set(LEGACY, legacy_cycle(clock.now))
    entry = await setup_central(hass, observe_section())
    manager = manager_of(hass)
    assert manager.cycle_origin == "legacy_seed"
    attributes = diagnostic(hass, entry).attributes
    assert attributes["cycle_origin"] == "legacy_seed"
    assert attributes["cycle"]["off"] == clock.now - 600

    # the external cycle is not followed afterwards
    hass.states.async_set(LEGACY, json.dumps({"m": "off", "off": clock.now, "r": None, "s": []}))
    await settle(hass)
    await clock.advance(hass, 16)
    assert manager.cycle_origin == "legacy_seed"
    assert diagnostic(hass, entry).attributes["cycle_origin"] == "legacy_seed"
    assert json.loads(hass.states.get(LEGACY).state)["m"] == "off"  # untouched by the observer

    # deferred write: not at every evaluation
    assert storage_key(entry) not in hass_storage
    await clock.advance(hass, 31)
    saved = hass_storage[storage_key(entry)]
    assert saved["version"] == 1 and saved["data"]["legacy_seeded"] is True
    assert set(saved["data"]["cycle"]) == {"m", "off", "r", "s"}


async def test_local_restore_wins_and_the_seed_is_not_repeated(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    put_states(hass, world(clock.now))
    hass.states.async_set(LEGACY, legacy_cycle(clock.now))
    key = f"{STORE_KEY}ENTRY"
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="TheCentralConfigMockName",
        unique_id="centralConfigUniqueId",
        data={**FULL_CENTRAL_CONFIG, CONF_HEATING_ORCHESTRATION: observe_section()},
        entry_id="ENTRY",
    )
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"cycle": {"m": "off", "off": clock.now - 50, "r": None, "s": []}, "legacy_seeded": True, "first_origin": "legacy_seed"},
    }
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    manager = manager_of(hass)
    assert manager.cycle_origin == "local_restore"
    assert diagnostic(hass, entry).attributes["cycle_origin"] == "local_restore"
    assert manager._legacy_seeded is True


async def test_invalid_stored_context_falls_back_to_a_new_observation(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    put_states(hass, world(clock.now))
    key = f"{STORE_KEY}ENTRY"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": {"cycle": "garbage"}}
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="TheCentralConfigMockName",
        unique_id="centralConfigUniqueId",
        data={**FULL_CENTRAL_CONFIG, CONF_HEATING_ORCHESTRATION: observe_section()},
        entry_id="ENTRY",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    attributes = diagnostic(hass, entry).attributes
    assert attributes["cycle_origin"] == "new_observation" and attributes["last_error"] == "store_invalid"


@pytest.mark.parametrize("value", ["garbage", "unknown", "unavailable", "[1, 2]", "null"])
async def test_invalid_legacy_seed_starts_a_new_observation(hass: HomeAssistant, init_vtherm_api, clock, value):
    put_states(hass, world(clock.now))
    hass.states.async_set(LEGACY, value)
    entry = await setup_central(hass, observe_section())
    attributes = diagnostic(hass, entry).attributes
    assert attributes["cycle_origin"] == "new_observation" and attributes["last_error"] == "legacy_seed_invalid"
    assert attributes["inputs_ok"] is True  # the observation itself is not affected


async def test_no_legacy_binding_is_a_new_observation_without_error(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    section = observe_section()
    del section["legacy_cycle_entity_id"]
    entry = await setup_central(hass, section)
    attributes = diagnostic(hass, entry).attributes
    assert attributes["cycle_origin"] == "new_observation" and attributes["last_error"] is None


async def test_cycle_is_saved_on_unload_without_waiting_for_the_delay(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    put_states(hass, world(clock.now))
    hass.states.async_set(LEGACY, legacy_cycle(clock.now))
    entry = await setup_central(hass, observe_section())
    assert storage_key(entry) not in hass_storage
    await hass.config_entries.async_unload(entry.entry_id)
    await settle(hass)
    assert hass_storage[storage_key(entry)]["data"]["legacy_seeded"] is True


async def test_reload_restores_the_saved_cycle_without_a_second_seed(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    put_states(hass, world(clock.now))
    hass.states.async_set(LEGACY, legacy_cycle(clock.now))
    entry = await setup_central(hass, observe_section())
    assert manager_of(hass).cycle_origin == "legacy_seed"
    hass.states.async_set(LEGACY, json.dumps({"m": "off", "off": clock.now, "r": None, "s": []}))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)
    assert manager_of(hass).cycle_origin == "local_restore"
    assert diagnostic(hass, entry).attributes["cycle_origin"] == "local_restore"


# ---------------------------------------------------------------------------
# Unload, reload and late callbacks
# ---------------------------------------------------------------------------


async def test_unload_cancels_listeners_timers_and_publications(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    put_states(hass, world(clock.now))
    with _with_deadline(lambda now: now + 5):
        entry = await setup_central(hass, observe_section())
        manager = manager_of(hass)
        old_epoch = manager._epoch
        published: list = []
        manager.attach(lambda state, attributes: published.append(state))
        published.clear()
        generation = manager.generation
        await hass.config_entries.async_unload(entry.entry_id)
        await settle(hass)

    assert manager_of(hass) is None
    assert not manager.is_running and manager._deadline_unsub is None and manager._state_unsub is None and not manager._unsubs and manager._pending is None
    with count_evaluations() as collected:
        hass.states.async_set(MASTER, "heat")
        await settle(hass)
        async_fire_time_changed(hass, clock.real0 + timedelta(seconds=60))
        await settle(hass)
        # late callbacks of the cancelled generation
        manager.async_request_evaluation()
        manager._run_pending(old_epoch)
        await settle(hass)
    assert collected.call_count == 0 and published == [] and manager.generation == generation
    state = hass.states.get(sensor_entity_id(hass, entry) or "sensor.missing")
    assert state is None or state.state == "unavailable"


async def test_manager_cannot_restart_after_stop(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    await setup_central(hass, observe_section())
    manager = manager_of(hass)
    await manager.async_stop()
    await manager.async_start()
    assert not manager.is_running and manager._state_unsub is None
    await manager.async_stop()  # idempotent


async def test_reload_never_duplicates_observers(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    entry = await setup_central(hass, observe_section())
    first = manager_of(hass)
    for _ in range(3):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await settle(hass)
    second = manager_of(hass)
    assert second is not first and not first.is_running and second.is_running
    with count_evaluations() as collected:
        hass.states.async_set(MASTER, "heat")
        await settle(hass)
        assert collected.call_count == 1
        await clock.advance(hass, 16)
        assert collected.call_count == 2  # one poll, not one per reload
    assert hass.states.get(sensor_entity_id(hass, entry)).state == second.state


async def test_configuration_change_to_disabled_stops_observing(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    put_states(hass, world(clock.now))
    entry = await setup_central(hass, observe_section())
    first = manager_of(hass)
    assert first.is_running
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HEATING_ORCHESTRATION: {"mode": "disabled"}})
    await settle(hass)
    assert not first.is_running
    second = manager_of(hass)
    assert second is not first and not second.is_running
    assert diagnostic(hass, entry).state == "disabled"
    with count_evaluations() as collected:
        hass.states.async_set(MASTER, "heat")
        await clock.advance(hass, 16)
    assert collected.call_count == 0


async def test_removing_the_section_removes_the_manager(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    entry = await setup_central(hass, observe_section())
    data = {key: value for key, value in entry.data.items() if key != CONF_HEATING_ORCHESTRATION}
    hass.config_entries.async_update_entry(entry, data=data)
    await settle(hass)
    assert manager_of(hass) is None


async def test_restart_resumes_observing_only_when_the_entry_says_observe(hass: HomeAssistant, init_vtherm_api, clock):
    """No hidden state re-enables a mode: after a reload the entry data decide."""
    put_states(hass, world(clock.now))
    entry = await setup_central(hass, {"mode": "disabled"})
    assert not manager_of(hass).is_running
    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)
    assert not manager_of(hass).is_running and diagnostic(hass, entry).state == "disabled"
