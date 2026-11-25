# pylint: disable=wildcard-import, unused-wildcard-import, protected-access, unused-argument, line-too-long, redefined-outer-name

"""Home Assistant tests of the integration of the PDC runtime in the central configuration (observe only).

WRITTEN AND NEVER EXECUTED: Carlo forbade running Home Assistant (and the pytest-homeassistant fixtures) for this work.
They may contain errors and must be run once, in the isolated test environment, before any activation.

They cover what needs a real ``hass``: the setup, unload and reload of the central entry with a PDC sub-section, the diagnostic
sensors (one per room), the refusal of ``control`` (error state, nothing built), the entries that do not carry the new keys, and
the options flow (the PDC steps never offer ``control`` and the boiler step keeps the PDC keys).
"""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.versatile_thermostat.vtherm_central_api import VersatileThermostatAPI

from .commons import *  # pylint: disable=wildcard-import, unused-wildcard-import
from .test_heating_orchestration import (  # noqa: F401  (the fixtures are used by name)
    clock_fixture,
    observe_section,
    put_states,
    service_calls_fixture,
    settle,
    setup_central,
    world,
)

PDC_SUFFIX = "_heating_orchestration_pdc_"
ROOM = {
    "room_key": "carlo",
    "radiator_entity_id": "climate.trv_vtherm_carlo",
    "radiator_valve_entity_id": "climate.trv_carlo",
    "temperature_entity_id": "sensor.temp_carlo",
    "window_entity_id": "binary_sensor.window_carlo",
    "split_entity_id": "climate.split_carlo",
    "pdc_entity_id": "climate.pdc_carlo",
    "automatic_entity_id": "input_boolean.auto_carlo",
    "schedule_stop_entity_id": "sensor.stop_carlo",
    "managed_entity_id": "input_boolean.managed_carlo",
    "override_entity_id": "input_boolean.override_carlo",
    "cycle_entity_id": "input_text.cycle_carlo",
    "manual_preset_bypass": True,
}


def pdc_section(pdc_mode="observe", **runtime_changes):
    runtime = {"power_entity_id": "sensor.power", "central_mode_entity_id": "select.central_mode_pdc", "rooms": [dict(ROOM)]}
    runtime.update(runtime_changes)
    return {**observe_section(), "pdc_mode": pdc_mode, "pdc_runtime": runtime}


def put_pdc_states(hass: HomeAssistant) -> None:
    s = hass.states.async_set
    s("sensor.power", "400")
    s("select.central_mode_pdc", "Auto")
    s("climate.trv_vtherm_carlo", "heat", {"requested_state": {"hvac_mode": "heat"}, "preset_mode": "comfort", "temperature": 19.0, "specific_states": {}})
    s("climate.trv_carlo", "heat")
    s("sensor.temp_carlo", "18.5")
    s("binary_sensor.window_carlo", "off")
    s("climate.split_carlo", "off", {"temperature": 22.0})
    s("climate.pdc_carlo", "off", {"temperature": 20.0})
    s("input_boolean.auto_carlo", "on")
    s("sensor.stop_carlo", "none")
    s("input_boolean.managed_carlo", "off")
    s("input_boolean.override_carlo", "off")
    s("input_text.cycle_carlo", "{}")


def pdc_sensor(hass: HomeAssistant, entry):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}{PDC_SUFFIX}carlo")
    return None if entity_id is None else hass.states.get(entity_id)


def pdc_calls(calls):
    return [call for call in calls if call[0] in ("climate", "input_boolean", "input_text", "timer", "persistent_notification")]


async def test_observe_creates_the_manager_and_one_sensor_per_room_and_sends_nothing(hass: HomeAssistant, init_vtherm_api, hass_storage, service_calls, clock):
    put_states(hass, world(clock.now))
    put_pdc_states(hass)
    entry = await setup_central(hass, pdc_section())
    api = VersatileThermostatAPI.get_vtherm_api(hass)
    manager = api.heating_pdc_manager
    assert manager is not None and manager.entry_id == entry.entry_id and manager.mode == "observe" and manager.is_running
    service_calls.clear()
    await clock.advance(hass, 400)
    sensor = pdc_sensor(hass, entry)
    assert sensor is not None and sensor.attributes["mode"] == "observe" and sensor.attributes["owner"] == "observer" and sensor.attributes["room_key"] == "carlo"
    assert sensor.attributes["transaction"]["phase"] == "idle"
    assert pdc_calls(service_calls) == []
    assert hass.states.get("climate.split_carlo").state == "off" and hass.states.get("climate.trv_vtherm_carlo").attributes["specific_states"] == {}
    assert f"versatile_thermostat.heating_orchestration_pdc.{entry.entry_id}" not in hass_storage  # observe never writes


async def test_the_pdc_mode_is_independent_of_the_boiler_mode(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    put_pdc_states(hass)
    section = {**pdc_section("disabled"), "mode": "observe"}
    entry = await setup_central(hass, section)
    api = VersatileThermostatAPI.get_vtherm_api(hass)
    assert api.heating_orchestration_manager.mode == "observe"
    assert api.heating_pdc_manager.mode == "disabled" and not api.heating_pdc_manager.is_running
    assert pdc_sensor(hass, entry).state == "disabled"


async def test_an_entry_without_the_new_keys_gets_no_pdc_manager_and_no_pdc_sensor(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    entry = await setup_central(hass, observe_section())
    api = VersatileThermostatAPI.get_vtherm_api(hass)
    assert api.heating_pdc_manager is None and api.heating_orchestration_manager is not None
    assert pdc_sensor(hass, entry) is None


async def test_control_is_refused_with_an_error_state_and_nothing_is_built(hass: HomeAssistant, init_vtherm_api, hass_storage, service_calls, clock):
    put_states(hass, world(clock.now))
    put_pdc_states(hass)
    entry = await setup_central(hass, pdc_section("control"))
    api = VersatileThermostatAPI.get_vtherm_api(hass)
    manager = api.heating_pdc_manager
    assert manager is not None and not manager.is_running and manager.runtime is None and manager.config_error == "config_control_not_available:mode"
    sensor = pdc_sensor(hass, entry)
    assert sensor.state == "error" and sensor.attributes["last_error"] == "config_control_not_available:mode"
    service_calls.clear()
    await clock.advance(hass, 400)
    assert pdc_calls(service_calls) == [] and not any("heating_orchestration_pdc" in key for key in hass_storage)


async def test_unload_and_reload_cancel_the_manager_and_never_duplicate_it(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    put_pdc_states(hass)
    entry = await setup_central(hass, pdc_section())
    api = VersatileThermostatAPI.get_vtherm_api(hass)
    first = api.heating_pdc_manager
    assert first.is_running
    assert await hass.config_entries.async_unload(entry.entry_id)
    await settle(hass)
    assert api.heating_pdc_manager is None and not first.is_running and first._unsubs == [] and first.runtime._stopped
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    second = api.heating_pdc_manager
    assert second is not first and second.is_running and not first.is_running
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_stop_after_the_unload_never_publishes_to_a_removed_sensor(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    put_pdc_states(hass)
    entry = await setup_central(hass, pdc_section())
    manager = VersatileThermostatAPI.get_vtherm_api(hass).heating_pdc_manager
    assert await hass.config_entries.async_unload(entry.entry_id)
    await settle(hass)
    assert manager._publishers.get("carlo") == []  # the sensor detached itself
    hass.states.async_set("sensor.power", "900")
    await hass.async_block_till_done()


async def _options_to_pdc(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "heating_pdc" in result["menu_options"]
    return await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "heating_pdc"})


async def test_the_options_flow_never_offers_control_for_the_pdc(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    put_pdc_states(hass)
    entry = await setup_central(hass, observe_section())
    form = await _options_to_pdc(hass, entry)
    assert form["step_id"] == "heating_pdc"
    assert form["data_schema"].schema["pdc_mode"].config["options"] == ["disabled", "observe"]
    with pytest.raises(Exception):  # the selector refuses a value outside its options before the step runs
        await hass.config_entries.options.async_configure(
            form["flow_id"], {"pdc_mode": "control", "power_entity_id": "sensor.power", "central_mode_entity_id": "select.central_mode_pdc"}
        )


async def test_the_options_flow_stores_the_pdc_section_and_the_boiler_step_keeps_it(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    put_pdc_states(hass)
    entry = await setup_central(hass, observe_section())
    form = await _options_to_pdc(hass, entry)
    rooms_menu = await hass.config_entries.options.async_configure(
        form["flow_id"], {"pdc_mode": "observe", "power_entity_id": "sensor.power", "central_mode_entity_id": "select.central_mode_pdc"}
    )
    assert rooms_menu["step_id"] == "heating_pdc_rooms"
    # an observe without any room is not left silently as it is
    again = await hass.config_entries.options.async_configure(rooms_menu["flow_id"], {"next_step_id": "heating_pdc_rooms_done"})
    assert again["step_id"] == "heating_pdc_rooms"
    add = await hass.config_entries.options.async_configure(rooms_menu["flow_id"], {"next_step_id": "heating_pdc_add_room"})
    bad = await hass.config_entries.options.async_configure(add["flow_id"], {**ROOM, "split_entity_id": "climate.nowhere"})
    assert bad["step_id"] == "heating_pdc_add_room" and bad["errors"]
    ok = await hass.config_entries.options.async_configure(add["flow_id"], dict(ROOM))
    assert ok["step_id"] == "heating_pdc_rooms"
    done = await hass.config_entries.options.async_configure(ok["flow_id"], {"next_step_id": "heating_pdc_rooms_done"})
    # edit the boiler step: the PDC keys survive it
    boiler = await hass.config_entries.options.async_configure(done["flow_id"], {"next_step_id": "heating_orchestration"})
    assert boiler["step_id"] == "heating_orchestration"
    result = await hass.config_entries.options.async_configure(boiler["flow_id"], {"mode": "disabled"})
    assert result["type"] == "menu"
    final = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "finalize"})
    assert final["type"] == "create_entry"
    section = entry.data["heating_orchestration"]
    assert section["pdc_mode"] == "observe" and section["pdc_runtime"]["rooms"][0]["room_key"] == "carlo" and section["pdc_runtime"]["power_entity_id"] == "sensor.power"


async def test_a_refused_control_value_in_the_stored_section_is_an_error_not_a_control(hass: HomeAssistant, init_vtherm_api, clock):
    put_states(hass, world(clock.now))
    put_pdc_states(hass)
    entry = await setup_central(hass, pdc_section("observe"))
    manager = VersatileThermostatAPI.get_vtherm_api(hass).heating_pdc_manager
    assert manager.mode == "observe"
    await hass.config_entries.async_unload(entry.entry_id)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "heating_orchestration": pdc_section("control")})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    refused = VersatileThermostatAPI.get_vtherm_api(hass).heating_pdc_manager
    assert refused.runtime is None and refused.config_error == "config_control_not_available:mode"
