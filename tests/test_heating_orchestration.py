# pylint: disable=wildcard-import, unused-wildcard-import, protected-access, unused-argument, line-too-long, redefined-outer-name

"""Home Assistant lifecycle tests of the boiler orchestration (M2-A observe, M2-B control).

WRITTEN AND NEVER EXECUTED: Carlo forbade running Home Assistant (and the
pytest-homeassistant fixtures) for this work. They may contain errors and must
be run once, in the isolated test environment, before any activation.

Real Home Assistant (config entries, registry, ``Store``, timers). In ``observe``
no service of any other integration is executed. In ``control`` the only service
is ``climate.set_hvac_mode`` on the master, replaced by a recording mock. The
clock is controlled through the manager clock hook and ``async_fire_time_changed``:
nothing sleeps for real.
"""

import asyncio
import dataclasses
import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events, async_fire_time_changed, async_mock_service

from custom_components.versatile_thermostat import feature_heating_orchestration_manager as manager_module
from custom_components.versatile_thermostat.heating_boiler_collector import SCHEMA_VERSION, cycle_to_dict
from custom_components.versatile_thermostat.sensor import HeatingOrchestrationBoilerSensor
from custom_components.versatile_thermostat.vtherm_central_api import VersatileThermostatAPI

from .commons import *  # pylint: disable=wildcard-import, unused-wildcard-import
from .test_heating_boiler_collector import CENTRAL, COUNT, LEGACY, MASTER, SECTION, base_snapshot, vtherm

SENSOR_SUFFIX = "_heating_orchestration_boiler"
EXPECTED_KEYS = {
    "schema_version",
    "schema_minor",
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
    "control_status",
    "command_status",
    "service_completed_at",
    "command_attempts",
    "command_retry_at",
    "external_owners",
}
OWNERS = ["automation.caldaia_controllo_stabile_delle_richieste", "automation.caldaia_filtro_richiesta_vtherm"]


class Clock:
    """Manager clock plus the matching ``async_fire_time_changed`` instants.

    The timers of Home Assistant are armed on the real clock; the manager clock
    starts at the real time of the fixture and only moves with ``advance``. The
    firing instants are cumulative from the first ``advance`` (taken right after
    the setup), so the 15 s interval timers and the one-shot timers set at clock
    instants both become due exactly when the elapsed test time says so.
    """

    def __init__(self) -> None:
        self.real0 = dt_util.utcnow()
        self.now = self.real0.timestamp()
        self.elapsed = 0.0
        self.fire_base = None

    def __call__(self) -> float:
        return self.now

    async def advance(self, hass: HomeAssistant, seconds: float) -> None:
        """Move the manager clock and fire the timers due in the meantime."""
        if self.fire_base is None:
            self.fire_base = dt_util.utcnow()
        self.now += seconds
        self.elapsed += seconds
        async_fire_time_changed(hass, max(self.fire_base, self.real0) + timedelta(seconds=self.elapsed))
        await hass.async_block_till_done()


@pytest.fixture(autouse=True)
def open_boiler_control_gate(request):
    """The shipped release keeps boiler control closed (``BOILER_CONTROL_AVAILABLE = False``).

    These tests exercise the control code that a later release will enable, so the gate is opened
    for each test, except the ``test_release_gate_*`` tests that check the shipped behaviour.
    """
    if request.node.name.startswith("test_release_gate"):
        yield
        return
    from custom_components.versatile_thermostat import config_flow as flow_module  # pylint: disable=import-outside-toplevel

    with patch.object(manager_module, "BOILER_CONTROL_AVAILABLE", True), patch.object(flow_module.VersatileThermostatOptionsFlowHandler, "_ho_allow_control", True):
        yield


def test_release_gate_offers_explicit_boiler_control():
    """This release offers control through the options flow and still requires confirmation."""
    from custom_components.versatile_thermostat import config_flow as flow_module  # pylint: disable=import-outside-toplevel

    assert manager_module.BOILER_CONTROL_AVAILABLE is True
    assert flow_module.VersatileThermostatOptionsFlowHandler._ho_allow_control is True  # pylint: disable=protected-access


@pytest.fixture(name="clock")
def clock_fixture():
    clock = Clock()
    with patch.object(manager_module, "_utc_clock", clock):
        yield clock


@pytest.fixture(name="service_calls")
def service_calls_fixture(hass):
    """Every service call, recorded from the ``call_service`` events (``ServiceRegistry`` has ``__slots__``: it cannot be patched).

    Items are ``(domain, service, (), service_data)``.
    """
    calls: list = []

    @callback
    def _record(event) -> None:
        calls.append((event.data.get("domain"), event.data.get("service"), (), event.data.get("service_data")))

    unsub = hass.bus.async_listen("call_service", _record)
    yield calls
    unsub()


def put_states(hass: HomeAssistant, snapshot: dict) -> None:
    for entity_id, value in snapshot.items():
        hass.states.async_set(entity_id, value["state"], value.get("attributes") or {})


def world(now: float, **changes) -> dict:
    """A healthy world. The legacy cycle helper holds a valid empty cycle: no startup error."""
    snapshot = base_snapshot(now=now, **changes)
    snapshot.setdefault(LEGACY, {"state": json.dumps(cycle_to_dict(None))})
    return snapshot


def observe_section(**changes) -> dict:
    section = json.loads(json.dumps(SECTION))
    section.update(changes)
    return section


def control_section(**changes) -> dict:
    return observe_section(mode="control", external_owner_entity_ids=list(OWNERS), control_confirmed=True, **changes)


def control_world(now: float, *, owners: str = "off", master: str = "heat", **changes) -> dict:
    """Master in heat and no valid radiator: M1 proposes ``off`` at once (``no_valid_radiator``)."""
    states = {
        MASTER: {"state": master, "attributes": {"hvac_modes": ["off", "heat"]}},
        COUNT: {"state": "0", "attributes": {"active_device_ids": []}},
    }
    states.update({owner: {"state": owners} for owner in OWNERS})
    states.update(changes)
    return world(now, **states)


def heat_world(now: float, **changes) -> dict:
    """Master off and a manual request with valid radiators: M1 proposes ``heat`` at once (``manual``)."""
    states = {MASTER: {"state": "off", "attributes": {"hvac_modes": ["off", "heat"]}}, "binary_sensor.manual_request": {"state": "on"}}
    states.update({owner: {"state": "off"} for owner in OWNERS})
    states.update(changes)
    return world(now, **states)


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
    assert state.attributes["last_error"] is None and state.attributes["control_status"] == "inactive"
    after = {entity.unique_id for entity in er.async_entries_for_config_entry(registry, entry.entry_id)}
    assert len(after) == len(before) + 1
    assert f"{entry.entry_id}{SENSOR_SUFFIX}" in after
    assert not [key for key in hass_storage if "heating_orchestration" in key]
    assert not [call for call in service_calls if call[0] != "persistent_notification"]


@pytest.mark.parametrize(
    "section, error",
    [
        ({"mode": "control", **{key: value for key, value in SECTION.items() if key != "mode"}}, "config_required:external_owner_entity_ids"),
        (
            {"mode": "control", "external_owner_entity_ids": list(OWNERS), **{key: value for key, value in SECTION.items() if key != "mode"}},
            "config_control_not_confirmed:control_confirmed",
        ),
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


async def test_observe_publishes_the_schema_v1_1_diagnostic(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    put_states(hass, world(clock.now))
    entry = await setup_central(hass, observe_section())
    manager = manager_of(hass)
    assert manager.is_running and manager.mode == "observe"

    state = diagnostic(hass, entry)
    attributes = dict(state.attributes)
    assert state.state == attributes["reason"] and state.state not in ("starting", "error", "disabled", "unknown", "unavailable")
    assert set(attributes) >= EXPECTED_KEYS
    assert attributes["schema_version"] == 1 and attributes["schema_minor"] == 1 and attributes["mode"] == "observe" and attributes["owner"] == "observer"
    assert attributes["evaluated_at"] == clock.now and attributes["generation"] >= 1
    assert attributes["proposed_action"] in ("none", "heat", "off")
    assert attributes["master_mode"] == "off" and attributes["count_raw"] == 3 and attributes["count_raw_valid"] is True
    assert attributes["valid_valve_count"] == 3 and attributes["inputs_ok"] is True
    assert attributes["inputs"] == {"central_mode": "Heat only", "manual": "off", "pilot": "off", "pilot_ready": True, "consent": True}
    assert [room["room_key"] for room in attributes["rooms"]] == ["bedroom", "living"]
    assert attributes["excluded_rooms"] == []
    assert attributes["commanded_mode"] is None and attributes["last_command_at"] is None and attributes["last_confirmation_at"] is None
    assert attributes["control_status"] == "inactive" and attributes["command_status"] is None and attributes["external_owners"] == []
    assert attributes["last_error"] is None and attributes["cycle_origin"] == "legacy_seed"
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
    # <central device name>_<translated name>: the language of the instance decides the suffix.
    assert entity_id.endswith("_boiler_orchestration") or entity_id.endswith("_orchestrazione_caldaia"), entity_id
    assert hass.states.get(entity_id).attributes.get("friendly_name", "").endswith(("Boiler orchestration", "Orchestrazione caldaia"))


async def test_recorder_exclusions_cover_the_bulky_attributes():
    excluded = HeatingOrchestrationBoilerSensor._unrecorded_attributes
    assert {"conditions", "rooms", "excluded_rooms", "evaluated_at", "generation", "cycle", "inputs"} <= set(excluded)
    # state (reason) and the short attributes stay recorded
    assert not {"schema_version", "mode", "owner", "proposed_action", "reason", "last_error", "cycle_origin", "control_status", "command_status"} & set(excluded)


async def test_observe_never_calls_a_service_nor_writes_the_legacy_entities(hass: HomeAssistant, init_vtherm_api, hass_storage, service_calls, clock):
    put_states(hass, world(clock.now))
    legacy_before = hass.states.get(LEGACY)
    entry = await setup_central(hass, observe_section())
    service_calls.clear()

    # inputs changing, heating requested, polling, a deadline: still no actuation
    for state in ("on", "off", "unknown"):
        hass.states.async_set(MASTER, state)
        await settle(hass)
    hass.states.async_set("climate.vt_living", "heat", vtherm(["climate.v_liv_1"], now=clock.now, current=10, target=22)["attributes"])
    await settle(hass)
    for _ in range(5):
        await clock.advance(hass, 16)
    await hass.config_entries.async_unload(entry.entry_id)
    await settle(hass)

    # the recorded events come from a @callback listener on the event bus: nothing was called
    assert [call for call in service_calls if call[0] != "persistent_notification"] == []
    assert hass.states.get(LEGACY) == legacy_before


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


async def test_unit_error_is_reported_even_after_a_startup_error(hass: HomeAssistant, init_vtherm_api, clock):
    """Review M2-A I-1: an earlier startup error no longer masks the unit error."""
    put_states(hass, world(clock.now, **{LEGACY: {"state": "garbage"}}))
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry = await setup_central(hass, observe_section())
    assert diagnostic(hass, entry).attributes["last_error"] == "temperature_unit_unsupported"


# ---------------------------------------------------------------------------
# Scheduling and publication
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
    assert manager_of(hass).generation == before + 1  # the master mode changed: published once


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


async def test_recovery_polling_every_15_seconds_and_heartbeat_publication(hass: HomeAssistant, init_vtherm_api, clock):
    """Evaluations every 15 s; an unchanged decision is published only at the heartbeat (review M2-A I-4)."""
    put_states(hass, world(clock.now))
    entry = await setup_central(hass, observe_section())
    start = dict(diagnostic(hass, entry).attributes)
    with count_evaluations() as collected:
        await clock.advance(hass, 10)
        assert collected.call_count == 0
        await clock.advance(hass, 6)  # 16 s: first poll, same decision, younger than the heartbeat
        assert collected.call_count == 1
        assert diagnostic(hass, entry).attributes["generation"] == start["generation"]
        assert diagnostic(hass, entry).attributes["evaluated_at"] == start["evaluated_at"]
        await clock.advance(hass, 15)  # 31 s: second poll, the heartbeat is due
        assert collected.call_count == 2
    attributes = diagnostic(hass, entry).attributes
    assert attributes["generation"] == start["generation"] + 1 and attributes["evaluated_at"] == clock.now
    assert clock.now == start["evaluated_at"] + 31


async def test_a_significant_change_is_published_at_once(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    entry = await setup_central(hass, observe_section())
    before = diagnostic(hass, entry).attributes["generation"]
    hass.states.async_set(MASTER, "heat")
    await settle(hass)
    attributes = diagnostic(hass, entry).attributes
    assert attributes["generation"] == before + 1 and attributes["master_mode"] == "heat"


async def test_sample_updates_alone_do_not_rewrite_the_diagnostic(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    entry = await setup_central(hass, observe_section())
    entity_id = sensor_entity_id(hass, entry)
    writes = async_capture_events(hass, "state_changed")
    writes.clear()
    for index in range(5):
        attributes = dict(hass.states.get("climate.vt_living").attributes)
        attributes["current_temperature"] = 18.0 + index * 0.1  # a new sample, still the same decision
        hass.states.async_set("climate.vt_living", "heat", attributes)
        await settle(hass)
    assert [event for event in writes if event.data["entity_id"] == entity_id] == []


async def test_one_clock_per_evaluation(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    await setup_central(hass, observe_section())
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
        assert not manager._deadline_timer.is_scheduled and manager._pending is None
        assert diagnostic(hass, entry).attributes["deadline"] == clock.now - 30
        await clock.advance(hass, 16)
        assert manager.generation == generation + 1  # recovered by the poll only (the deadline moved: significant)


async def test_future_deadline_schedules_one_timer_and_recomputes(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    with _with_deadline(lambda now: now + 5):
        await setup_central(hass, observe_section())
        manager = manager_of(hass)
        generation = manager.generation
        assert manager._deadline_timer.is_scheduled and manager._deadline_timer.at == clock.now + 5
        await clock.advance(hass, 6)
        assert manager.generation == generation + 1
        # the evaluation moved the deadline to the new now + 5: still a single timer
        assert manager._deadline_timer.is_scheduled and manager._deadline_timer.at == clock.now + 5


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


async def test_a_failing_publisher_does_not_stop_the_evaluation(hass: HomeAssistant, init_vtherm_api, clock):
    """Review M2-A I-5: publication errors never prevent the re-subscription."""
    put_states(hass, world(clock.now))
    await setup_central(hass, observe_section())
    manager = manager_of(hass)

    def broken(state, attributes):
        raise RuntimeError("publisher bug")

    manager.attach(broken)
    hass.states.async_set(MASTER, "heat")
    await settle(hass)
    assert manager.is_running and manager.generation >= 1 and MASTER in manager.watched_entity_ids


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
    # M1 restarts the pause when the saved mode was not off while the master is off (whole seconds)
    assert attributes["cycle"]["off"] == int(clock.now)

    # the external cycle is not followed afterwards
    hass.states.async_set(LEGACY, json.dumps({"m": "off", "off": clock.now, "r": None, "s": []}))
    await settle(hass)
    await clock.advance(hass, 16)
    assert manager.cycle_origin == "legacy_seed"
    assert diagnostic(hass, entry).attributes["cycle_origin"] == "legacy_seed"
    assert json.loads(hass.states.get(LEGACY).state)["m"] == "off"  # untouched by the manager

    # deferred write: not at every evaluation
    assert storage_key(entry) not in hass_storage
    await clock.advance(hass, 31)
    saved = hass_storage[storage_key(entry)]
    assert saved["version"] == 2 and saved["data"]["legacy_seeded"] is True and saved["data"]["command"] is None
    assert set(saved["data"]["cycle"]) == {"m", "off", "r", "s"}


async def test_deferred_write_is_not_postponed_by_every_evaluation(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    put_states(hass, world(clock.now))
    hass.states.async_set(LEGACY, legacy_cycle(clock.now))
    entry = await setup_central(hass, observe_section())
    for _ in range(3):
        await clock.advance(hass, 15)  # an evaluation at each step, the write is due after 30 s
    assert storage_key(entry) in hass_storage


async def _setup_with_stored(hass, hass_storage, clock, stored, *, section=None, version=2):
    put_states(hass, world(clock.now))
    key = f"{STORE_KEY}ENTRY"
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="TheCentralConfigMockName",
        unique_id="centralConfigUniqueId",
        data={**FULL_CENTRAL_CONFIG, CONF_HEATING_ORCHESTRATION: section if section is not None else observe_section()},
        entry_id="ENTRY",
    )
    if stored is not None:
        hass_storage[key] = {"version": version, "minor_version": 1, "key": key, "data": stored}
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    return entry


async def test_local_restore_wins_and_the_seed_is_not_repeated(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    hass.states.async_set(LEGACY, legacy_cycle(clock.now))
    stored = {"cycle": {"m": "off", "off": clock.now - 50, "r": None, "s": []}, "legacy_seeded": True, "first_origin": "legacy_seed", "command": None}
    entry = await _setup_with_stored(hass, hass_storage, clock, stored)
    manager = manager_of(hass)
    assert manager.cycle_origin == "local_restore"
    assert diagnostic(hass, entry).attributes["cycle_origin"] == "local_restore"
    assert manager._legacy_seeded is True


async def test_store_version_1_of_m2a_is_migrated(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    """The M2-A store holds only the cycle: version 2 adds the (empty) command record."""
    stored = {"cycle": {"m": "off", "off": clock.now - 50, "r": None, "s": []}, "legacy_seeded": True, "first_origin": "legacy_seed"}
    entry = await _setup_with_stored(hass, hass_storage, clock, stored, version=1)
    manager = manager_of(hass)
    assert manager.cycle_origin == "local_restore" and manager.command_record is None
    assert diagnostic(hass, entry).attributes["last_error"] is None
    assert hass_storage[f"{STORE_KEY}ENTRY"]["version"] == 2  # rewritten by the migration


async def test_store_of_a_newer_version_falls_back_to_a_new_observation(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    entry = await _setup_with_stored(hass, hass_storage, clock, {"cycle": {}}, version=3)
    attributes = diagnostic(hass, entry).attributes
    assert attributes["last_error"] == "store_load_failed"
    assert manager_of(hass).is_running  # review M2-A M-2: no failed setup


async def test_invalid_stored_context_falls_back_to_a_new_observation(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    section = observe_section()
    del section["legacy_cycle_entity_id"]
    entry = await _setup_with_stored(hass, hass_storage, clock, {"cycle": "garbage"}, section=section)
    attributes = diagnostic(hass, entry).attributes
    assert attributes["cycle_origin"] == "new_observation" and attributes["last_error"] == "store_invalid"


@pytest.mark.parametrize("value", ["garbage", "unknown", "unavailable", "[1, 2]", "null"])
async def test_invalid_legacy_seed_starts_a_new_observation(hass: HomeAssistant, init_vtherm_api, clock, value):
    put_states(hass, world(clock.now, **{LEGACY: {"state": value}}))
    entry = await setup_central(hass, observe_section())
    attributes = diagnostic(hass, entry).attributes
    assert attributes["cycle_origin"] == "new_observation" and attributes["last_error"] == "legacy_seed_invalid"
    assert attributes["inputs_ok"] is True  # the observation itself is not affected


async def test_startup_error_is_released_after_a_minute(hass: HomeAssistant, init_vtherm_api, clock):
    """Review M2-A I-1: an error of the start is not sticky for the whole life of the manager."""
    put_states(hass, world(clock.now, **{LEGACY: {"state": "garbage"}}))
    entry = await setup_central(hass, observe_section())
    assert diagnostic(hass, entry).attributes["last_error"] == "legacy_seed_invalid"
    await clock.advance(hass, 61)
    assert diagnostic(hass, entry).attributes["last_error"] is None


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


async def test_removing_the_entry_removes_its_stored_context(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    put_states(hass, world(clock.now))
    hass.states.async_set(LEGACY, legacy_cycle(clock.now))
    entry = await setup_central(hass, observe_section())
    await hass.config_entries.async_unload(entry.entry_id)
    await settle(hass)
    assert storage_key(entry) in hass_storage
    await hass.config_entries.async_remove(entry.entry_id)
    await settle(hass)
    assert storage_key(entry) not in hass_storage


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
    assert not manager.is_running and not manager._deadline_timer.is_scheduled and not manager._command_timer.is_scheduled
    assert manager._state_unsub is None and not manager._unsubs and manager._pending is None
    with count_evaluations() as collected:
        hass.states.async_set(MASTER, "heat")
        await settle(hass)
        await clock.advance(hass, 60)
        # late callbacks of the cancelled generation
        manager.async_request_evaluation()
        manager._run_pending(old_epoch)
        await settle(hass)
    assert collected.call_count == 0 and published == [] and manager.generation == generation
    state = hass.states.get(sensor_entity_id(hass, entry) or "sensor.missing")
    assert state is None or state.state == "unavailable"


async def test_the_unload_callbacks_are_registered_once_and_stop_the_manager(hass: HomeAssistant, init_vtherm_api, clock):
    """``async_on_unload`` accepts the coroutine function; the explicit stop and the callback agree."""
    put_states(hass, world(clock.now))
    entry = await setup_central(hass, observe_section())
    first = manager_of(hass)
    assert first.is_running
    assert len(entry._on_unload or []) >= 2  # the update listener and the stop of the orchestration
    assert await hass.config_entries.async_unload(entry.entry_id)
    await settle(hass)
    assert not first.is_running and manager_of(hass) is None
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    second = manager_of(hass)
    assert second is not first and second.is_running  # the old callback did not touch the new manager


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


# ---------------------------------------------------------------------------
# M2-B: control
# ---------------------------------------------------------------------------


def last_calls(calls):
    return [(call.data["entity_id"], call.data["hvac_mode"]) for call in calls]


async def settle_control(hass, clock, seconds=30):
    """Two evaluations at least 30 s apart: the post setup settling is over."""
    await clock.advance(hass, 15)
    await clock.advance(hass, seconds - 15)


async def test_control_waits_for_the_settling_then_sends_one_off(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    calls = async_mock_service(hass, "climate", "set_hvac_mode")
    put_states(hass, control_world(clock.now))
    entry = await setup_central(hass, control_section())
    attributes = diagnostic(hass, entry).attributes
    assert attributes["mode"] == "control" and attributes["proposed_action"] == "off" and attributes["reason"] == "no_valid_radiator"
    assert attributes["owner"] == "controller" and attributes["control_status"] == "settling"
    assert calls == []  # not even OFF before the settling
    await clock.advance(hass, 15)
    assert calls == []
    await clock.advance(hass, 15)  # 30 s after the first valid evaluation
    assert last_calls(calls) == [(MASTER, "off")]
    attributes = diagnostic(hass, entry).attributes
    assert attributes["command_status"] == "pending" and attributes["commanded_mode"] == "off" and attributes["command_attempts"] == 1
    assert attributes["last_command_at"] == clock.now and attributes["service_completed_at"] == clock.now
    assert attributes["last_confirmation_at"] is None  # the completed service is not a confirmation
    assert attributes["control_status"] == "pending"


async def test_control_confirmation_comes_from_the_observed_master(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    calls = async_mock_service(hass, "climate", "set_hvac_mode")
    events = async_capture_events(hass, "logbook_entry")
    put_states(hass, control_world(clock.now))
    entry = await setup_central(hass, control_section())
    await settle_control(hass, clock)
    assert len(calls) == 1
    hass.states.async_set(MASTER, "off", {"hvac_modes": ["off", "heat"]})
    await settle(hass)
    attributes = diagnostic(hass, entry).attributes
    assert attributes["command_status"] == "confirmed" and attributes["last_confirmation_at"] == clock.now
    assert attributes["control_status"] == "confirmed"
    # one command only: no repetition while the master keeps the mode
    for _ in range(6):
        await clock.advance(hass, 15)
    assert len(calls) == 1
    messages = [event.data["message"] for event in events]
    assert len(messages) == 2 and messages[0].startswith("OIKOS_HEATING_V1|boiler|sent|off|attempt=1/4") and messages[1].startswith("OIKOS_HEATING_V1|boiler|confirmed|off")
    assert all(event.data["entity_id"] == MASTER and event.data["domain"] == "climate" for event in events)


async def test_control_unconfirmed_command_is_retried_with_growing_waits_up_to_four_sends(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    calls = async_mock_service(hass, "climate", "set_hvac_mode")  # completes, but the master never changes
    put_states(hass, control_world(clock.now))
    entry = await setup_central(hass, control_section())
    await settle_control(hass, clock)
    assert len(calls) == 1
    await clock.advance(hass, 59)
    assert len(calls) == 1 and diagnostic(hass, entry).attributes["command_status"] == "pending"
    await clock.advance(hass, 2)  # the confirmation timeout (60 s) passed: failed, retry in 60 s
    attributes = diagnostic(hass, entry).attributes
    assert attributes["command_status"] == "failed" and attributes["last_error"] == "confirmation_timeout" and attributes["command_attempts"] == 1
    await clock.advance(hass, 61)
    assert len(calls) == 2
    await clock.advance(hass, 62)  # timeout, then a wait of 120 s
    await clock.advance(hass, 121)
    assert len(calls) == 3
    await clock.advance(hass, 62)
    await clock.advance(hass, 301)
    assert len(calls) == 4
    await clock.advance(hass, 62)  # the fourth failure: no fifth send, ever
    attributes = diagnostic(hass, entry).attributes
    assert attributes["control_status"] == "command_failed" and attributes["command_attempts"] == 4
    for _ in range(10):
        await clock.advance(hass, 600)
    assert len(calls) == 4


async def test_control_service_error_is_registered_and_retried(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    calls = async_mock_service(hass, "climate", "set_hvac_mode", raise_exception=HomeAssistantError("boom"))
    put_states(hass, control_world(clock.now))
    entry = await setup_central(hass, control_section())
    await settle_control(hass, clock)
    assert len(calls) == 1
    attributes = diagnostic(hass, entry).attributes
    assert attributes["command_status"] == "failed" and attributes["last_error"].startswith("service_error:") and attributes["service_completed_at"] is None
    assert attributes["command_retry_at"] == clock.now + 60
    await clock.advance(hass, 59)
    assert len(calls) == 1
    await clock.advance(hass, 2)
    assert len(calls) == 2


async def test_control_a_failed_or_slow_save_never_delays_the_off(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    calls = async_mock_service(hass, "climate", "set_hvac_mode")

    async def never_finishes(self, data):
        await asyncio.sleep(3600)

    put_states(hass, control_world(clock.now))
    entry = await setup_central(hass, control_section())
    with patch.object(manager_module._OrchestrationStore, "async_save", never_finishes):
        await settle_control(hass, clock)
        assert last_calls(calls) == [(MASTER, "off")]  # the OFF did not wait for the save
    assert diagnostic(hass, entry).attributes["command_status"] == "pending"


async def test_control_heat_goes_on_when_the_save_fails(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    calls = async_mock_service(hass, "climate", "set_hvac_mode")

    async def broken(self, data):
        raise HomeAssistantError("disk full")

    put_states(hass, heat_world(clock.now))
    entry = await setup_central(hass, control_section())
    with patch.object(manager_module._OrchestrationStore, "async_save", broken):
        await settle_control(hass, clock)
    assert last_calls(calls) == [(MASTER, "heat")]
    assert diagnostic(hass, entry).attributes["last_error"] == "command_save_failed"


async def test_control_is_blocked_while_an_external_controller_is_not_off(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    calls = async_mock_service(hass, "climate", "set_hvac_mode")
    put_states(hass, control_world(clock.now, owners="on"))
    entry = await setup_central(hass, control_section())
    for _ in range(8):
        await clock.advance(hass, 15)
    attributes = diagnostic(hass, entry).attributes
    assert calls == []
    assert attributes["owner"] == "observer" and attributes["control_status"] == "blocked_external_owner" and attributes["proposed_action"] == "off"
    assert attributes["external_owners"] == [{"entity_id": OWNERS[0], "state": "on"}, {"entity_id": OWNERS[1], "state": "on"}]


@pytest.mark.parametrize("state", ["on", "unknown", "unavailable", None])
async def test_control_an_unusable_external_controller_blocks(hass: HomeAssistant, init_vtherm_api, hass_storage, clock, state):
    calls = async_mock_service(hass, "climate", "set_hvac_mode")
    put_states(hass, control_world(clock.now, **({OWNERS[1]: {"state": state}} if state else {})))
    if state is None:
        hass.states.async_remove(OWNERS[1])  # the external controller disappeared: it does not grant the ownership
    entry = await setup_central(hass, control_section())
    for _ in range(8):
        await clock.advance(hass, 15)
    assert calls == [] and diagnostic(hass, entry).attributes["control_status"] == "blocked_external_owner"


async def test_control_an_unknown_master_is_never_commanded(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    calls = async_mock_service(hass, "climate", "set_hvac_mode")
    put_states(hass, control_world(clock.now, master="unavailable"))
    entry = await setup_central(hass, control_section())
    for _ in range(8):
        await clock.advance(hass, 15)
    assert calls == [] and diagnostic(hass, entry).attributes["control_status"] == "blocked_master_unknown"


async def test_control_reactivation_of_the_external_controller_cancels_everything(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    calls = async_mock_service(hass, "climate", "set_hvac_mode")
    put_states(hass, control_world(clock.now))
    entry = await setup_central(hass, control_section())
    await settle_control(hass, clock)
    assert len(calls) == 1
    hass.states.async_set(OWNERS[0], "on")  # somebody switched the old controller on again
    await settle(hass)
    attributes = diagnostic(hass, entry).attributes
    assert attributes["owner"] == "observer" and attributes["control_status"] == "blocked_external_owner" and attributes["command_status"] == "cancelled"
    for _ in range(12):
        await clock.advance(hass, 60)
    assert len(calls) == 1  # no retry, no compensation command
    hass.states.async_set(OWNERS[0], "off")  # the ownership is back: a new request, with a new budget
    await settle(hass)
    await clock.advance(hass, 1)
    assert len(calls) == 2 and diagnostic(hass, entry).attributes["command_attempts"] == 1


async def test_control_revalidates_with_a_new_evaluation_before_the_send(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    calls = async_mock_service(hass, "climate", "set_hvac_mode")
    real = manager_module.evaluate_boiler

    def changing(snapshot, cycle, now, *args, **kwargs):
        decision = real(snapshot, cycle, now, *args, **kwargs)
        task = asyncio.current_task()
        if task is not None and task.get_name().startswith("vtherm boiler command"):
            return dataclasses.replace(decision, action="none", reason="no_demand")  # the situation changed meanwhile
        return decision

    put_states(hass, control_world(clock.now))
    entry = await setup_central(hass, control_section())
    evaluations = []

    def counting(*args, **kwargs):
        evaluations.append(1)
        return changing(*args, **kwargs)

    with patch.object(manager_module, "evaluate_boiler", counting):
        await settle_control(hass, clock)
        before = len(evaluations)
        for _ in range(50):
            await hass.async_block_till_done()  # a zero-delay loop would evaluate at every turn (review M2-B I-3)
        assert len(evaluations) - before <= 4
    assert calls == []  # the revalidation did not confirm the action: nothing sent, no attempt consumed
    assert diagnostic(hass, entry).attributes["command_attempts"] == 0


async def test_control_unload_during_the_wait_cancels_everything_and_the_restart_reads_the_master(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    calls = async_mock_service(hass, "climate", "set_hvac_mode")
    put_states(hass, control_world(clock.now))
    entry = await setup_central(hass, control_section())
    await settle_control(hass, clock)
    assert len(calls) == 1
    manager = manager_of(hass)
    await hass.config_entries.async_unload(entry.entry_id)
    await settle(hass)
    assert not manager.is_running and not manager._command_timer.is_scheduled and manager._send_task is None
    for _ in range(5):
        await clock.advance(hass, 600)
    assert len(calls) == 1  # no retry after the unload
    stored = hass_storage[storage_key(entry)]["data"]["command"]
    assert stored["mode"] == "off" and stored["status"] == "pending" and stored["attempts"] == 1

    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    attributes = diagnostic(hass, entry).attributes
    assert attributes["command_status"] == "unconfirmed" and attributes["last_confirmation_at"] is None
    assert len(calls) == 1  # and settling again: nothing is sent at once
    assert attributes["control_status"] == "settling"


async def test_control_reload_settles_again_before_any_command(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    calls = async_mock_service(hass, "climate", "set_hvac_mode")
    put_states(hass, world(clock.now, **{owner: {"state": "off"} for owner in OWNERS}))
    entry = await setup_central(hass, control_section())
    await settle_control(hass, clock)
    assert calls == [] and manager_of(hass).is_settled
    # a reload: the rooms are transiently not eligible and the master is in heat, so M1 proposes off
    hass.states.async_set(MASTER, "heat", {"hvac_modes": ["off", "heat"]})
    hass.states.async_set(COUNT, "0", {"active_device_ids": []})
    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)
    assert not manager_of(hass).is_settled
    await clock.advance(hass, 15)
    assert calls == []
    await clock.advance(hass, 15)
    assert last_calls(calls) == [(MASTER, "off")]


async def test_control_stored_pending_command_becomes_unconfirmed_and_is_never_confirmed_by_the_restart(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    calls = async_mock_service(hass, "climate", "set_hvac_mode")
    put_states(hass, control_world(clock.now, master="off"))
    stored = {
        "cycle": {"m": "off", "off": int(clock.now) - 10, "r": None, "s": []},
        "legacy_seeded": True,
        "first_origin": "legacy_seed",
        "command": {
            "mode": "off",
            "at": clock.now - 5,
            "attempts": 2,
            "status": "pending",
            "send_id": 3,
            "service_completed_at": clock.now - 4,
            "last_confirmation_at": None,
            "base_master": "heat",
            "error": None,
        },
    }
    entry = await _setup_with_stored(hass, hass_storage, clock, stored, section=control_section())
    attributes = diagnostic(hass, entry).attributes
    assert attributes["command_status"] == "unconfirmed" and attributes["commanded_mode"] == "off" and attributes["command_attempts"] == 2
    assert attributes["last_confirmation_at"] is None  # the master is already off, yet the restart confirms nothing
    assert calls == []


async def test_control_the_diagnostic_lists_the_control_fields_and_keeps_the_schema(hass: HomeAssistant, init_vtherm_api, hass_storage, clock):
    async_mock_service(hass, "climate", "set_hvac_mode")
    put_states(hass, control_world(clock.now))
    entry = await setup_central(hass, control_section())
    await settle_control(hass, clock)
    attributes = dict(diagnostic(hass, entry).attributes)
    assert set(attributes) >= EXPECTED_KEYS and attributes["schema_version"] == 1 and attributes["schema_minor"] == 1
    json.dumps(attributes, allow_nan=False)
    assert attributes["external_owners"] == [{"entity_id": owner, "state": "off"} for owner in OWNERS]


async def test_control_never_touches_other_entities_or_services(hass: HomeAssistant, init_vtherm_api, hass_storage, service_calls, clock):
    """The only service of the controller is climate.set_hvac_mode on the master: no helper, timer or automation."""
    async_mock_service(hass, "climate", "set_hvac_mode")
    put_states(hass, control_world(clock.now))
    await setup_central(hass, control_section())
    service_calls.clear()
    await settle_control(hass, clock)
    await clock.advance(hass, 300)
    used = {(domain, service) for domain, service, _args, _kwargs in service_calls if domain != "persistent_notification"}
    assert used <= {("climate", "set_hvac_mode")}
    for owner in OWNERS:
        assert hass.states.get(owner).state == "off"


# ---------------------------------------------------------------------------
# M2-B: options flow (control is selectable only there, behind a confirmation)
# ---------------------------------------------------------------------------


async def _options_flow_to_orchestration(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "heating_orchestration" in result["menu_options"]
    return await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "heating_orchestration"})


def _central_user_input(mode):
    return {
        "mode": mode,
        "master_entity_id": MASTER,
        "central_mode_entity_id": CENTRAL,
        "active_count_entity_id": COUNT,
        "consent_entity_ids": ["input_boolean.consent_a", "binary_sensor.consent_b"],
        "external_owner_entity_ids": list(OWNERS),
    }


async def test_options_flow_offers_control_with_a_confirmation_step(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, control_world(clock.now))
    entry = await setup_central(hass, observe_section())
    form = await _options_flow_to_orchestration(hass, entry)
    assert form["step_id"] == "heating_orchestration"
    modes = form["data_schema"].schema["mode"].config["options"]
    assert modes == ["disabled", "observe", "control"]
    confirm = await hass.config_entries.options.async_configure(form["flow_id"], _central_user_input("control"))
    assert confirm["type"] == "form" and confirm["step_id"] == "heating_orchestration_control_confirm"
    assert OWNERS[0] in confirm["description_placeholders"]["owners"] and MASTER == confirm["description_placeholders"]["master"]
    # without the explicit confirmation nothing is stored as control
    refused = await hass.config_entries.options.async_configure(confirm["flow_id"], {"confirm_control": False})
    assert refused["step_id"] == "heating_orchestration_control_confirm" and refused["errors"] == {"confirm_control": "ho_control_not_confirmed"}
    accepted = await hass.config_entries.options.async_configure(confirm["flow_id"], {"confirm_control": True})
    assert accepted["step_id"] == "heating_orchestration_rooms"
    done = await hass.config_entries.options.async_configure(accepted["flow_id"], {"next_step_id": "heating_orchestration_rooms_done"})
    final = await hass.config_entries.options.async_configure(done["flow_id"], {"next_step_id": "finalize"})
    assert final["type"] == "create_entry"
    section = entry.data[CONF_HEATING_ORCHESTRATION]
    assert section["mode"] == "control" and section["control_confirmed"] is True and section["external_owner_entity_ids"] == OWNERS


async def test_options_flow_control_requires_the_external_controllers(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, control_world(clock.now))
    entry = await setup_central(hass, observe_section())
    form = await _options_flow_to_orchestration(hass, entry)
    user_input = _central_user_input("control")
    del user_input["external_owner_entity_ids"]
    result = await hass.config_entries.options.async_configure(form["flow_id"], user_input)
    assert result["step_id"] == "heating_orchestration" and result["errors"] == {"external_owner_entity_ids": "ho_required"}


async def test_options_flow_validates_the_external_controllers(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, control_world(clock.now))
    entry = await setup_central(hass, observe_section())
    form = await _options_flow_to_orchestration(hass, entry)
    cases = (
        ([MASTER], "ho_invalid_domain"),  # a climate is not an external controller
        (["input_boolean.consent_a"], "ho_duplicate_binding"),  # already a consent
        (["automation.not_existing"], "unknown_entity"),
    )
    for owners, error in cases:
        result = await hass.config_entries.options.async_configure(form["flow_id"], {**_central_user_input("control"), "external_owner_entity_ids": owners})
        assert result["step_id"] == "heating_orchestration" and list(result["errors"].values()) == [error], (owners, result["errors"])


async def test_options_flow_leaving_control_drops_the_confirmation(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, control_world(clock.now))
    entry = await setup_central(hass, control_section())
    form = await _options_flow_to_orchestration(hass, entry)
    result = await hass.config_entries.options.async_configure(form["flow_id"], _central_user_input("observe"))
    assert result["step_id"] == "heating_orchestration_rooms"
    done = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "heating_orchestration_rooms_done"})
    await hass.config_entries.options.async_configure(done["flow_id"], {"next_step_id": "finalize"})
    section = entry.data[CONF_HEATING_ORCHESTRATION]
    assert section["mode"] == "observe" and "control_confirmed" not in section


async def test_creation_flow_never_offers_control(hass: HomeAssistant, init_vtherm_api):
    from custom_components.versatile_thermostat.config_schema import STEP_CENTRAL_HEATING_ORCHESTRATION_SCHEMA

    assert STEP_CENTRAL_HEATING_ORCHESTRATION_SCHEMA.schema["mode"].config["options"] == ["disabled", "observe"]
