# pylint: disable=wildcard-import, unused-wildcard-import, protected-access, unused-argument, line-too-long, redefined-outer-name

"""Home Assistant lifecycle tests of the PDC runtime manager (W2).

WRITTEN AND NEVER EXECUTED: Carlo forbade running Home Assistant (and the
pytest-homeassistant fixtures) for this work. They may contain errors and must be run once, in
the isolated test environment, before any activation.

The decisions and the sequence of the hand-over are tested without Home Assistant in
``tests/test_heating_pdc_runtime.py`` (a simulated plant, a controlled clock). These tests only cover what
needs a real ``hass``: the listeners and what they forward, the ``Store``, the services that are really called
(replaced by recording mocks, with no user context), the VTherm inhibition adapter, the sensors' publishers and
the cancellation of everything on stop.
"""

import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Context, HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from custom_components.versatile_thermostat import feature_heating_pdc_manager as manager_module
from custom_components.versatile_thermostat.feature_heating_pdc_manager import (
    CONF_PDC_RUNTIME,
    PDC_STORAGE_KEY_PREFIX,
    FeatureHeatingPdcManager,
    extract_pdc_section,
)

from .commons import *  # pylint: disable=wildcard-import, unused-wildcard-import
from .test_heating_pdc_runtime import ROOM_TEMPLATE, section

ENTRY = "central_entry"
SPLIT, PDC, RAD, VALVE = "climate.split_carlo", "climate.pdc_carlo", "climate.trv_vtherm_carlo", "climate.trv_carlo"


class FakeInhibition:
    """The VTherm inhibition API on real states: the radiator VTherm publishes ``specific_states.inhibition`` like the core does."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[tuple[str, str]] = []
        self.transition = 0

    def _publish(self, entity_id: str, inhibited: bool) -> None:
        self.transition += 1
        state = self.hass.states.get(entity_id)
        attributes = dict(state.attributes)
        attributes["specific_states"] = {"inhibition": {"inhibited": inhibited, "last_transition_at": f"t{self.transition}"}}
        self.hass.states.async_set(entity_id, "off" if inhibited else "heat", attributes)
        self.hass.states.async_set(VALVE, "off" if inhibited else "heat")

    async def set(self, entity_id: str, stop_underlyings: bool) -> None:
        self.calls.append(("set", entity_id))
        self._publish(entity_id, True)

    async def unset(self, entity_id: str) -> None:
        self.calls.append(("unset", entity_id))
        self._publish(entity_id, False)

    def started(self, entity_id: str) -> bool:
        return True


def seed_states(hass: HomeAssistant) -> None:
    s = hass.states.async_set
    s("sensor.power", "400")
    s("select.central_mode", "Auto")
    s("automation.chooser_carlo", "off")
    s(RAD, "heat", {"requested_state": {"hvac_mode": "heat"}, "preset_mode": "comfort", "temperature": 19.0, "specific_states": {}})
    s(VALVE, "heat")
    s("sensor.temp_carlo", "18.5")
    s("binary_sensor.window_carlo", "off")
    s(SPLIT, "off", {"temperature": 22.0, "fan_mode": "auto", "preset_mode": "none", "swing_mode": "off"})
    s(PDC, "off", {"temperature": 20.0})
    s("input_boolean.auto_carlo", "on")
    s("sensor.stop_carlo", "none")
    s("input_boolean.managed_carlo", "off")
    s("input_boolean.override_carlo", "off")
    s("input_text.cycle_carlo", json.dumps({"m": "off", "off": int(dt_util.utcnow().timestamp()) - 3000}))


def make_manager(hass: HomeAssistant, mode: str, **kwargs):
    data = section(mode, rooms=("carlo",))
    return FeatureHeatingPdcManager(hass, ENTRY, data, inhibition=kwargs.get("inhibition") or FakeInhibition(hass))


def test_the_pdc_sub_section_is_optional_and_absent_means_disabled():
    assert extract_pdc_section({CONF_PDC_RUNTIME: {"mode": "observe"}}) == {"mode": "observe"}
    assert extract_pdc_section({}) is None and extract_pdc_section(None) is None and extract_pdc_section({CONF_PDC_RUNTIME: 3}) is None
    # the PDC has its own mode, next to the sub-section, independent of the boiler mode
    assert extract_pdc_section({"mode": "control", "pdc_mode": "observe", CONF_PDC_RUNTIME: {"mode": "control", "rooms": []}}) == {"mode": "observe", "rooms": []}


async def test_a_disabled_or_invalid_configuration_owns_nothing(hass: HomeAssistant) -> None:
    seen = []
    off = FeatureHeatingPdcManager(hass, ENTRY, None)
    off.attach("carlo", lambda state, attrs: seen.append(state))
    await off.async_start()
    assert off.mode == "disabled" and off.runtime is None and not off.is_running and off.room_keys == ()
    invalid = FeatureHeatingPdcManager(hass, ENTRY, {"mode": "control", "rooms": "nope"})
    assert invalid.mode == "disabled" and invalid.config is None and invalid.config_error.startswith("config_")
    await invalid.async_start()
    assert not invalid.is_running
    state, attrs = invalid.state("pdc")
    assert state == "error" and attrs["last_error"] == invalid.config_error
    # control needs the explicit declaration of the other owners
    data = section("control")
    data["rooms"][0].pop("external_owner_entity_ids")
    refused = FeatureHeatingPdcManager(hass, ENTRY, data)
    assert refused.mode == "disabled" and refused.config_error == "config_external_owners_required:external_owner_entity_ids"


async def test_observe_publishes_the_diagnostics_and_calls_no_service(hass: HomeAssistant) -> None:
    seed_states(hass)
    calls = async_mock_service(hass, "climate", "set_hvac_mode")
    calls += async_mock_service(hass, "climate", "set_temperature")
    manager = make_manager(hass, "observe")
    published: list[tuple[str, dict]] = []
    manager.attach("carlo", lambda state, attrs: published.append((state, attrs)))
    await manager.async_start()
    await hass.async_block_till_done()
    hass.states.async_set("sensor.power", "500")
    await hass.async_block_till_done()
    assert manager.is_running and manager.mode == "observe" and calls == []
    state, attrs = manager.state("carlo")
    assert attrs["mode"] == "observe" and attrs["owner"] in ("observer", None)
    assert manager.store is not None  # only the load; observe never writes
    await manager.async_stop()
    assert not manager.is_running


async def test_the_sensor_is_not_flooded_by_every_power_sample(hass: HomeAssistant) -> None:
    manager = make_manager(hass, "observe")
    pushes: list = []
    manager.attach("carlo", lambda state, attrs: pushes.append(state))
    base = len(pushes)
    attrs = {"schema_version": 1, "evaluated_at": 1.0, "generation": 1, "reason": "x"}
    manager.publish("carlo", "x", dict(attrs))
    manager.publish("carlo", "x", {**attrs, "evaluated_at": 2.0, "generation": 2})
    manager.publish("carlo", "x", {**attrs, "evaluated_at": 3.0, "generation": 3})
    assert len(pushes) == base + 1
    manager.publish("carlo", "y", dict(attrs))
    manager.publish("carlo", "y", {**attrs, "reason": "z"})
    assert len(pushes) == base + 3


async def test_control_forwards_a_user_command_as_a_manual_override_and_never_sends_one_with_a_user_context(hass: HomeAssistant) -> None:
    seed_states(hass)
    mode_calls = async_mock_service(hass, "climate", "set_hvac_mode")
    async_mock_service(hass, "climate", "set_temperature")
    inhibition = FakeInhibition(hass)
    manager = make_manager(hass, "control", inhibition=inhibition)
    await manager.async_start()
    await hass.async_block_till_done()
    # an authenticated user switches the split on
    hass.states.async_set(SPLIT, "heat", {"temperature": 22.0})
    hass.bus.async_fire(
        EVENT_CALL_SERVICE,
        {"domain": "climate", "service": "set_hvac_mode", "service_data": {"entity_id": SPLIT, "hvac_mode": "heat"}},
        context=Context(user_id="user-1"),
    )
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=20))
    await hass.async_block_till_done()
    runtime = manager.runtime
    assert runtime.override("carlo").active and runtime.override("carlo").provenance == "user"
    assert runtime.transaction("carlo").phase == "override_hold" and ("set", RAD) in inhibition.calls
    for call in mode_calls:
        assert call.context.user_id is None  # the integration's own commands are never taken as a user command
    await manager.async_stop()


async def test_suspension_is_persisted_first_stops_the_pdc_and_is_refused_in_observe(hass: HomeAssistant, hass_storage) -> None:
    seed_states(hass)
    mode_calls = async_mock_service(hass, "climate", "set_hvac_mode")
    observer = make_manager(hass, "observe")
    await observer.async_start()
    assert (await observer.async_suspend("test")).accepted is False and mode_calls == []
    await observer.async_stop()
    hass.states.async_set(PDC, "heat", {"temperature": 20.0})
    hass.states.async_set(SPLIT, "heat", {"temperature": 22.0})
    manager = make_manager(hass, "control")
    await manager.async_start()
    await hass.async_block_till_done()
    result = await manager.async_suspend("test", hold_radiator_inhibition=True)
    await hass.async_block_till_done()
    assert result.accepted and manager.is_suspended
    stored = hass_storage[f"{PDC_STORAGE_KEY_PREFIX}.{ENTRY}"]["data"]
    assert stored["suspension"]["active"] is True and stored["schema_version"] == 1
    assert {(c.data["entity_id"], c.data["hvac_mode"]) for c in mode_calls} >= {(PDC, "off"), (SPLIT, "off")}
    state, attrs = manager.suspension_attributes()
    assert attrs["active"] is True and attrs["reason"] == "test"
    resumed = await manager.async_resume()
    assert resumed.accepted and not manager.is_suspended
    await manager.async_stop()


async def test_a_stored_transaction_is_loaded_and_an_unreadable_store_blocks_control(hass: HomeAssistant, hass_storage) -> None:
    seed_states(hass)
    key = f"{PDC_STORAGE_KEY_PREFIX}.{ENTRY}"
    hass_storage[key] = {
        "version": 1,
        "data": {"schema_version": 1, "reservation": {"holder": None, "token": None}, "rooms": {}, "suspension": {"active": True, "reason": "stored"}},
    }
    manager = make_manager(hass, "control")
    await manager.async_start()
    assert manager.runtime.suspension.active and manager.runtime.suspension.reason == "stored"
    await manager.async_stop()
    with patch.object(manager_module.Store, "async_load", AsyncMock(side_effect=OSError("disk"))):
        blind = make_manager(hass, "control")
        await blind.async_start()
        assert not blind.is_running and blind.state("carlo")[0] == "error" and blind.state("carlo")[1]["last_error"] == "store_load_failed"
        observer = make_manager(hass, "observe")
        await observer.async_start()
        assert observer.is_running  # observe never commands: an unreadable store does not matter
        await observer.async_stop()


async def test_stop_cancels_every_listener_and_is_idempotent(hass: HomeAssistant) -> None:
    seed_states(hass)
    manager = make_manager(hass, "control")
    await manager.async_start()
    assert manager._unsubs
    runtime = manager.runtime
    await manager.async_stop()
    await manager.async_stop()
    assert not manager._unsubs and not manager.is_running
    n = len(hass.states.async_all())
    hass.states.async_set("sensor.power", "900")
    await hass.async_block_till_done()
    assert all(room.worker is None or room.worker.done() for room in runtime._rooms.values())
    assert len(hass.states.async_all()) >= n


async def test_the_vtherm_inhibition_adapter_goes_through_the_existing_api(hass: HomeAssistant) -> None:
    api = MagicMock()
    api.set_inhibition = AsyncMock()
    api.unset_inhibition = AsyncMock()
    api.is_inhibition_started = MagicMock(return_value=True)
    adapter = manager_module._VThermInhibition(hass)
    with patch.object(adapter, "_api", return_value=api):
        await adapter.set("climate.x", True)
        await adapter.unset("climate.x")
        assert adapter.started("climate.x") is True
        api.is_inhibition_started.side_effect = LookupError("no vtherm")
        assert adapter.started("climate.x") is None
    api.set_inhibition.assert_awaited_once_with("climate.x", True)
    api.unset_inhibition.assert_awaited_once_with("climate.x")
    assert ROOM_TEMPLATE and asyncio.iscoroutinefunction(adapter.set)
