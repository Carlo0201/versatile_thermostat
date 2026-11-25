"""Pure tests of the PDC collector (runtime W2).

The collector and the pure PDC modules are loaded as a synthetic package that points at the component
directory, so the relative imports work and the component ``__init__`` (which imports Home Assistant)
is never executed. Run with only pytest installed, from the repository root::

    python -m pytest -q --noconftest --import-mode=importlib tests/test_heating_pdc_collector.py

The parity with the real Jinja templates is checked by
``plans/heating-orchestration-2026-11-24/pdc/verify_collector.py`` (one test below runs it on a seeded sample).
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import importlib
import json
import math
import os
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_DIR = Path(os.environ["PDC_COMPONENT_DIR"]) if os.environ.get("PDC_COMPONENT_DIR") else REPO_ROOT / "custom_components" / "versatile_thermostat"
HARNESS_PATH = REPO_ROOT / "plans" / "heating-orchestration-2026-11-24" / "pdc" / "verify_collector.py"
PKG = "_vtherm_pdc_pure_pkg_under_test"


def load(name: str) -> types.ModuleType:
    """A component module, loaded inside the synthetic package (shared by the PDC test files)."""
    if PKG not in sys.modules:
        package = types.ModuleType(PKG)
        package.__path__ = [str(COMPONENT_DIR)]
        sys.modules[PKG] = package
    return importlib.import_module(f"{PKG}.{name}")


c = load("heating_pdc_collector")
policy = load("heating_pdc_policy")
tr = load("heating_pdc_transaction")
prov = load("heating_pdc_provenance")

NOW = 1_795_500_000.0

SECTION: dict[str, Any] = {
    "mode": "observe",
    "power_entity_id": "sensor.power",
    "central_mode_entity_id": "select.central_mode",
    "rooms": [
        {
            "room_key": "carlo",
            "radiator_entity_id": "climate.trv_vtherm_carlo",
            "radiator_valve_entity_id": "climate.trv_carlo",
            "temperature_entity_id": "sensor.temp_carlo",
            "window_entity_id": "binary_sensor.window_carlo",
            "split_entity_id": "climate.split_carlo",
            "pdc_entity_id": "climate.pdc_carlo",
            "automatic_entity_id": "input_boolean.auto_carlo",
            "schedule_stop_entity_id": "sensor.stop_carlo",
            "override_timer_entity_id": "timer.override_carlo",
            "schedule_switch_entity_ids": ["switch.sched_carlo_a", "switch.sched_carlo_b"],
            "managed_entity_id": "input_boolean.managed_carlo",
            "override_entity_id": "input_boolean.override_carlo",
            "cycle_entity_id": "input_text.cycle_carlo",
            "manual_preset_bypass": True,
            "external_owner_entity_ids": ["automation.chooser_carlo", "script.resume_carlo"],
        },
        {
            "room_key": "diego",
            "radiator_entity_id": "climate.trv_vtherm_diego",
            "radiator_valve_entity_id": "climate.trv_diego",
            "temperature_entity_id": "sensor.temp_diego",
            "window_entity_id": "binary_sensor.window_diego",
            "split_entity_id": "climate.split_diego",
            "pdc_entity_id": "climate.pdc_diego",
            "automatic_entity_id": "input_boolean.auto_diego",
            "schedule_stop_entity_id": "sensor.stop_diego",
            "managed_entity_id": "input_boolean.managed_diego",
            "override_entity_id": "input_boolean.override_diego",
            "setpoint_min_c": 18,
            "external_owner_entity_ids": [],
        },
    ],
}


def section(**changes: Any) -> dict[str, Any]:
    out = copy.deepcopy(SECTION)
    out.update(changes)
    return out


def room_section(index: int = 0, **changes: Any) -> dict[str, Any]:
    out = copy.deepcopy(SECTION)
    out["rooms"][index].update(changes)
    return out


CONFIG = c.parse_pdc_config(SECTION)
CARLO = CONFIG.room("carlo")
DIEGO = CONFIG.room("diego")


def st(state: str | None, attributes: dict[str, Any] | None = None, changed: float | None = None, reported: float | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"state": state, "attributes": attributes or {}}
    if changed is not None:
        out["last_changed"] = changed
    if reported is not None:
        out["last_reported"] = reported
    return out


def world(**overrides: Any) -> dict[str, Any]:
    """A nominal Camera Carlo world: ready to start, on a coherent clock."""
    states: dict[str, Any] = {
        "sensor.power": st("400", reported=NOW - 5),
        "select.central_mode": st("Auto"),
        "climate.trv_vtherm_carlo": st("heat", {"requested_state": {"hvac_mode": "heat"}, "preset_mode": "comfort", "temperature": 19.0, "specific_states": {}}),
        "climate.trv_carlo": st("heat"),
        "sensor.temp_carlo": st("18.5", reported=NOW - 30),
        "binary_sensor.window_carlo": st("off", changed=NOW - 5000),
        "climate.split_carlo": st("off", changed=NOW - 5000),
        "climate.pdc_carlo": st("off", {"temperature": 20.0}),
        "input_boolean.auto_carlo": st("on"),
        "sensor.stop_carlo": st("none"),
        "timer.override_carlo": st("idle"),
        "switch.sched_carlo_a": st("on"),
        "switch.sched_carlo_b": st("off"),
        "input_boolean.managed_carlo": st("off", changed=NOW - 5000),
        "input_boolean.override_carlo": st("off"),
        "input_text.cycle_carlo": st(json.dumps({"m": "off", "on": 1795000000, "off": int(NOW) - 3000, "target": 19.0})),
        "input_boolean.managed_diego": st("off"),
        "climate.split_diego": st("off"),
    }
    states.update(overrides)
    return states


def collect(states: dict[str, Any], **kwargs: Any) -> Any:
    return c.collect_pdc_snapshot(states, CARLO, CONFIG, **kwargs)


# --- configuration ------------------------------------------------------------------------------


def test_a_valid_section_parses_with_the_room_differences_as_parameters():
    assert CONFIG.mode == "observe" and [r.room_key for r in CONFIG.rooms] == ["carlo", "diego"]
    assert (CARLO.policy.setpoint_min_c, CARLO.policy.manual_preset_bypass) == (16.0, True)
    assert (DIEGO.policy.setpoint_min_c, DIEGO.policy.manual_preset_bypass) == (18.0, False)
    assert CARLO.external_owner_entity_ids == ("automation.chooser_carlo", "script.resume_carlo") and DIEGO.external_owners_declared
    assert CONFIG.others("carlo") == (DIEGO,) and CONFIG.room("nobody") is None
    assert CONFIG.transaction == tr.PdcTransactionConfig()
    ids = CONFIG.configured_entity_ids()
    assert len(ids) == len(set(ids)) and "automation.chooser_carlo" in ids and "sensor.power" in ids


def test_the_transaction_timeouts_are_configurable_and_validated():
    cfg = c.parse_pdc_config(section(transaction={"radiator_timeout_s": 40, "return_retry_s": 0}))
    assert cfg.transaction.radiator_timeout_s == 40.0 and cfg.transaction.return_retry_s == 0.0 and cfg.transaction.heat_timeout_s == 20.0
    for bad in ({"radiator_timeout_s": -1}, {"heat_timeout_s": float("nan")}, {"off_timeout_s": True}, {"off_timeout_s": 99999}):
        with pytest.raises(c.PdcConfigError):
            c.parse_pdc_config(section(transaction=bad))
    with pytest.raises(c.PdcConfigError):
        c.parse_pdc_config(section(transaction=[1]))


@pytest.mark.parametrize(
    ("mutate", "code", "key"),
    [
        (lambda s: s.update(mode="turbo"), c.ERR_UNSUPPORTED_MODE, "mode"),
        (lambda s: s.update(mode=3), c.ERR_UNSUPPORTED_MODE, "mode"),
        (lambda s: s.update(rooms=[]), c.ERR_REQUIRED, "rooms"),
        (lambda s: s.update(rooms="x"), c.ERR_REQUIRED, "rooms"),
        (lambda s: s.pop("power_entity_id"), c.ERR_REQUIRED, "power_entity_id"),
        (lambda s: s.update(power_entity_id="light.kitchen"), c.ERR_INVALID_DOMAIN, "power_entity_id"),
        (lambda s: s.update(central_mode_entity_id="Select.Bad"), c.ERR_INVALID_ENTITY, "central_mode_entity_id"),
        (lambda s: s["rooms"][0].update(room_key="Carlo Room"), c.ERR_INVALID_ROOM_KEY, "room_key"),
        (lambda s: s["rooms"][1].update(room_key="carlo"), c.ERR_DUPLICATE_ROOM, "room_key"),
        (lambda s: s["rooms"][0].pop("split_entity_id"), c.ERR_REQUIRED, "split_entity_id"),
        (lambda s: s["rooms"][0].update(split_entity_id="sensor.split"), c.ERR_INVALID_DOMAIN, "split_entity_id"),
        (lambda s: s["rooms"][1].update(split_entity_id="climate.split_carlo"), c.ERR_DUPLICATE_BINDING, "split_entity_id"),
        (lambda s: s["rooms"][0].update(schedule_switch_entity_ids=["switch.a", "switch.a"]), c.ERR_DUPLICATE_BINDING, "schedule_switch_entity_ids"),
        (lambda s: s["rooms"][0].update(external_owner_entity_ids="automation.x"), c.ERR_INVALID_SECTION, "external_owner_entity_ids"),
        (lambda s: s["rooms"][0].update(external_owner_entity_ids=["light.x"]), c.ERR_INVALID_DOMAIN, "external_owner_entity_ids"),
        (lambda s: s["rooms"][0].update(setpoint_min_c=True), c.ERR_INVALID_VALUE, "setpoint_min_c"),
        (lambda s: s["rooms"][0].update(setpoint_min_c=float("inf")), c.ERR_INVALID_VALUE, "setpoint_min_c"),
        (lambda s: s["rooms"][0].update(manual_preset_bypass="yes"), c.ERR_INVALID_VALUE, "manual_preset_bypass"),
        (lambda s: s["rooms"][0].pop("managed_entity_id"), c.ERR_LEGACY_REQUIRED, "managed_entity_id"),
        (lambda s: s["rooms"][0].pop("override_entity_id"), c.ERR_LEGACY_REQUIRED, "override_entity_id"),
    ],
)
def test_invalid_configurations_are_rejected_with_a_stable_code(mutate, code, key):
    data = copy.deepcopy(SECTION)
    mutate(data)
    with pytest.raises(c.PdcConfigError) as err:
        c.parse_pdc_config(data)
    assert (err.value.code, err.value.key) == (code, key)


def test_control_needs_an_explicit_owner_declaration_and_observe_needs_the_legacy_helpers():
    data = copy.deepcopy(SECTION)
    data["mode"] = "control"
    data["rooms"][0].pop("external_owner_entity_ids")
    with pytest.raises(c.PdcConfigError) as err:
        c.parse_pdc_config(data, allow_control=True)
    assert err.value.code == c.ERR_EXTERNAL_OWNERS_REQUIRED
    data["rooms"][0]["external_owner_entity_ids"] = []  # declared empty: the operator states that no other owner exists
    assert c.parse_pdc_config(data, allow_control=True).room("carlo").external_owner_entity_ids == ()
    # in control the legacy helpers are optional (they are only mirrored)
    data["rooms"][0].pop("managed_entity_id")
    data["rooms"][0].pop("override_entity_id")
    assert c.parse_pdc_config(data, allow_control=True).room("carlo").managed_entity_id is None


def test_control_is_refused_unless_the_host_opts_in_and_disabled_keeps_whatever_it_holds():
    data = section(mode="control")
    with pytest.raises(c.PdcConfigError) as err:
        c.parse_pdc_config(data)
    assert (err.value.code, err.value.key) == (c.ERR_CONTROL_NOT_AVAILABLE, "mode")
    assert c.parse_pdc_config({**data, "mode": "disabled"}).mode == "disabled"  # not even looked at
    assert c.parse_pdc_config({"mode": "disabled", "rooms": "garbage"}).rooms == ()
    assert c.parse_pdc_config({**data, "mode": "observe"}).mode == "observe"


def test_disabled_without_rooms_is_valid_and_non_mappings_are_not():
    cfg = c.parse_pdc_config({"mode": "disabled"})
    assert cfg.mode == "disabled" and cfg.rooms == ()
    assert c.parse_pdc_config({}).mode == "disabled"
    for bad in (None, [], "observe", 3):
        with pytest.raises(c.PdcConfigError):
            c.parse_pdc_config(bad)


# --- reading the states -------------------------------------------------------------------------


def test_a_nominal_world_gives_the_expected_snapshot():
    s = collect(world()).snapshot
    assert (s.override, s.managed, s.automatic, s.central_mode) == ("off", "off", "on", "Auto")
    assert (s.radiator_mode, s.requested_hvac_mode, s.preset_mode, s.target, s.temperature) == ("heat", "heat", "comfort", 19.0, 18.5)
    assert (s.split_mode, s.pdc_mode, s.pdc_setpoint, s.power, s.window) == ("off", "off", 20.0, 400.0, "off")
    assert (s.temperature_reported_at, s.power_reported_at, s.split_changed_at, s.managed_changed_at) == (NOW - 30, NOW - 5, NOW - 5000, NOW - 5000)
    assert (s.other_managed, s.other_split_mode, s.schedule_stop, s.heating_override_timer) == ("off", "off", "none", "idle")
    assert s.schedule_switches == ("on", "off") and s.cycle.mode == "off" and s.cycle.target == 19.0 and s.cycle_available
    assert s.radiator_valve_mode == "heat" and s.radiator_inhibited is False  # specific_states present without an inhibition section
    assert policy.evaluate_pdc(s, policy.PdcStartContext(NOW - 200, False), NOW, CARLO.policy).action == "start"


def test_missing_unknown_and_unavailable_are_never_turned_into_off_zero_or_closed():
    states = world()
    for entity_id in (
        "sensor.power",
        "sensor.temp_carlo",
        "binary_sensor.window_carlo",
        "climate.split_carlo",
        "climate.pdc_carlo",
        "input_boolean.auto_carlo",
        "climate.trv_carlo",
    ):
        states[entity_id] = st("unavailable")
    states["sensor.power"] = st("unknown")
    s = collect(states).snapshot
    assert s.power is None and s.temperature is None
    assert (s.window, s.split_mode, s.pdc_mode, s.automatic, s.radiator_valve_mode) == ("unavailable",) * 5
    del states["climate.split_carlo"], states["binary_sensor.window_carlo"], states["input_boolean.managed_carlo"]
    s = collect(states).snapshot
    assert (s.split_mode, s.window, s.managed, s.window_changed_at, s.split_changed_at) == (None, None, None, None, None)
    assert "climate.split_carlo" in collect(states).missing
    decision = policy.evaluate_pdc(s, None, NOW, CARLO.policy)
    assert decision.action == "none" and "data_missing" in decision.diagnostic_codes


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "abc", "", "1e999"])
def test_non_finite_and_garbage_numbers_are_missing(value):
    states = world()
    states["sensor.power"] = st(value, reported=NOW)
    states["sensor.temp_carlo"] = st(value, reported=NOW)
    states["climate.trv_vtherm_carlo"] = st("heat", {"temperature": value, "requested_state": {"hvac_mode": "heat"}, "specific_states": {}})
    states["climate.pdc_carlo"] = st("off", {"temperature": value})
    s = collect(states).snapshot
    assert (s.power, s.temperature, s.target, s.pdc_setpoint) == (None, None, None, None)


def test_numbers_and_booleans_in_attributes():
    assert c.as_number(True) is None and c.as_number(False) is None
    assert c.as_number(3) == 3.0 and c.as_number("3.5") == 3.5 and c.as_number(None) is None and c.as_number([1]) is None and c.as_number(float("nan")) is None


def test_timestamps_accept_datetimes_numbers_and_iso_strings():
    moment = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    assert c.to_timestamp(moment) == moment.timestamp()
    assert c.to_timestamp(moment.replace(tzinfo=None)) == moment.timestamp()  # naive is UTC (Home Assistant states are)
    assert c.to_timestamp(moment.isoformat()) == moment.timestamp() and c.to_timestamp(12.5) == 12.5
    for bad in (None, True, "x", float("nan"), [1]):
        assert c.to_timestamp(bad) is None


def test_state_objects_are_read_like_dictionaries():
    ns = types.SimpleNamespace(state="heat", attributes={"temperature": 21}, last_changed=datetime.fromtimestamp(NOW - 7, timezone.utc), last_reported=None)
    view = c.lookup({"climate.x": ns}, "climate.x")
    assert (view.state, view.attributes["temperature"], view.changed_at, view.reported_at) == ("heat", 21, NOW - 7, None)
    assert c.lookup({}, "climate.x").state is None and c.lookup({}, None).state is None
    broken = c.lookup({"climate.x": types.SimpleNamespace(state=3, attributes=None)}, "climate.x")
    assert broken.state == "3" and broken.attributes == {}


def test_requested_state_and_preset_are_read_defensively():
    assert c.read_requested_hvac_mode({"requested_state": {"hvac_mode": "heat"}}) == "heat"
    for attributes in ({}, {"requested_state": None}, {"requested_state": "heat"}, {"requested_state": {}}):
        assert c.read_requested_hvac_mode(attributes) is None


@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        ({"specific_states": {}}, (False, None)),  # inhibition never used: not inhibited
        ({"specific_states": {"inhibition": {"inhibited": True, "last_transition_at": "t1"}}}, (True, "t1")),
        ({"specific_states": {"inhibition": {"inhibited": False, "last_transition_at": "t2"}}}, (False, "t2")),
        ({"specific_states": {"inhibition": {"inhibited": None}}}, (None, None)),
        ({"specific_states": {"inhibition": {"inhibited": "true"}}}, (None, None)),
        ({"specific_states": {"inhibition": "x"}}, (None, None)),
        ({"specific_states": None}, (None, None)),
        ({}, (None, None)),
    ],
)
def test_the_inhibition_flag_is_false_only_when_the_vtherm_publishes_it_as_unused_or_off(attributes, expected):
    assert c.read_inhibition(attributes) == expected


def test_the_cycle_comes_from_the_legacy_helper_or_from_the_local_value():
    states = world()
    states["input_text.cycle_carlo"] = st("unknown")
    s = collect(states).snapshot
    assert not s.cycle_available and s.cycle == policy.PdcCycle()
    for text in ("[]", "5", "{bad", '"x"', '{"m": NaN}', ""):
        states["input_text.cycle_carlo"] = st(text)
        s = collect(states).snapshot
        assert s.cycle_available and s.cycle == policy.PdcCycle(), text
    local = policy.PdcCycle("heat", 5.0, 3.0, 19.0)
    s = collect(world(), local=c.PdcLocalInputs(override="on", managed="on", managed_changed_at=NOW - 10, cycle=local)).snapshot
    assert (s.cycle, s.cycle_available, s.override, s.managed, s.managed_changed_at) == (local, True, "on", "on", NOW - 10)
    # a local value that is not provided falls back to the legacy helper
    s = collect(world(), local=c.PdcLocalInputs()).snapshot
    assert s.override == "off" and s.managed == "off" and s.managed_changed_at == NOW - 5000


def test_the_other_rooms_are_aggregated_with_unknown_staying_unknown():
    views = c.aggregate_others
    off, on = c.PdcOtherView("off", "off"), c.PdcOtherView("on", "heat")
    assert views([]) == c.PdcOtherView("off", "off")
    assert views([off, off]) == off
    assert views([off, on]) == c.PdcOtherView("on", "heat")
    assert views([c.PdcOtherView("off", "cool")]) == c.PdcOtherView("off", "cool")
    assert views([c.PdcOtherView("unknown", "unavailable")]) == c.PdcOtherView("unknown", "unavailable")
    assert views([off, c.PdcOtherView(None, None)]) == c.PdcOtherView(None, None)
    assert views([c.PdcOtherView("unknown", "off"), c.PdcOtherView("on", "dry")]) == c.PdcOtherView("on", "dry")
    states = world()
    states["input_boolean.managed_diego"] = st("on")
    assert collect(states).snapshot.other_managed == "on"
    assert c.read_other_view(states, DIEGO, local_managed="off").managed == "off"
    # the other room is free only when both its managed flag and its split are confirmed off
    assert not policy.other_pdc_free(collect(states).snapshot)


def test_a_room_with_a_single_room_configuration_has_no_other_pdc():
    cfg = c.parse_pdc_config(section(rooms=[SECTION["rooms"][0]]))
    s = c.collect_pdc_snapshot(world(), cfg.room("carlo"), cfg).snapshot
    assert (s.other_managed, s.other_split_mode) == ("off", "off")


def test_the_split_device_and_its_action_are_exposed_for_the_provenance_and_the_diagnostics():
    states = world()
    states["climate.split_carlo"] = st("heat", {"temperature": 22, "fan_mode": "low", "preset_mode": "none", "swing_mode": "off", "hvac_action": "idle"}, changed=NOW - 3)
    got = collect(states)
    assert got.split_action == "idle" and got.split_changed_at == NOW - 3
    assert got.split_device == prov.DeviceState("heat", 22.0, "low", "none", "off")
    assert c.device_state(c.lookup({}, "climate.nope")) is None
    # the action is information only: it never reaches the snapshot
    assert not any("action" in f.name for f in dataclasses.fields(policy.PdcSnapshot))


# --- the observation of a transaction ----------------------------------------------------------


def stopped_world(**overrides: Any) -> dict[str, Any]:
    states = world(**overrides)
    states["climate.trv_carlo"] = st("off")
    states["climate.trv_vtherm_carlo"] = st(
        "off",
        {
            "requested_state": {"hvac_mode": "heat"},
            "preset_mode": "comfort",
            "temperature": 19.0,
            "specific_states": {"inhibition": {"inhibited": True, "last_transition_at": "t1"}},
        },
    )
    states.update(overrides)
    return states


def test_radiator_stop_reading_distinguishes_not_stopped_from_not_reported():
    assert c.radiator_stop_reading(collect(stopped_world()).snapshot) is True
    assert c.radiator_stop_reading(collect(world()).snapshot) is False  # valve heating, not inhibited
    unknown_valve = stopped_world(**{"climate.trv_carlo": st("unavailable")})
    assert c.radiator_stop_reading(collect(unknown_valve).snapshot) is None
    no_flag = stopped_world()
    no_flag["climate.trv_vtherm_carlo"] = st("off", {"requested_state": {"hvac_mode": "heat"}, "temperature": 19.0})
    assert c.radiator_stop_reading(collect(no_flag).snapshot) is None
    not_inhibited = stopped_world()
    not_inhibited["climate.trv_vtherm_carlo"] = st("off", {"specific_states": {"inhibition": {"inhibited": False}}, "temperature": 19.0})
    assert c.radiator_stop_reading(collect(not_inhibited).snapshot) is False


def test_the_observation_carries_the_phase_recheck_the_target_and_the_override():
    snap = collect(stopped_world()).snapshot
    waiting_radiator = c.build_observation(3, snap, NOW, CARLO.policy, phase="waiting_radiator", override_off=True)
    assert (waiting_radiator.token, waiting_radiator.radiator_stopped, waiting_radiator.conditions_ok, waiting_radiator.override_off) == (3, True, True, True)
    assert waiting_radiator.target_confirmed is True and waiting_radiator.radiator_inhibited is True
    assert (waiting_radiator.split_mode, waiting_radiator.pdc_mode) == ("off", "off")
    wrong_setpoint = c.build_observation(
        3, collect(stopped_world(**{"climate.pdc_carlo": st("off", {"temperature": 25.0})})).snapshot, NOW, CARLO.policy, phase="waiting_target", override_off=True
    )
    assert wrong_setpoint.target_confirmed is False and wrong_setpoint.conditions_ok is False
    no_setpoint = c.build_observation(3, collect(stopped_world(**{"climate.pdc_carlo": st("off", {})})).snapshot, NOW, CARLO.policy, phase="waiting_target", override_off=None)
    assert no_setpoint.target_confirmed is None and no_setpoint.override_off is None
    running = c.build_observation(3, snap, NOW, CARLO.policy, phase="running", override_off=True)
    assert running.conditions_ok is None
    heat_world = stopped_world(**{"climate.split_carlo": st("heat", changed=NOW - 5), "climate.pdc_carlo": st("heat", {"temperature": 20.0})})
    waiting_heat = c.build_observation(3, collect(heat_world).snapshot, NOW, CARLO.policy, phase="waiting_heat", override_off=True)
    assert waiting_heat.conditions_ok is True and waiting_heat.split_mode == "heat"


# --- protections during a transaction -----------------------------------------------------------


@pytest.mark.parametrize("phase", ["waiting_radiator", "waiting_target", "waiting_heat"])
def test_the_hard_protections_interrupt_a_start_in_every_phase(phase):
    safe = collect(stopped_world()).snapshot
    assert not c.unsafe_during_start(safe, NOW, CARLO.policy, phase=phase)
    cases = {
        "power_missing": {"sensor.power": st("unknown", reported=NOW)},
        "power_stale": {"sensor.power": st("400", reported=NOW - 46)},
        "power_limit": {"sensor.power": st("2700", reported=NOW - 1)},
        "central_stopped": {"select.central_mode": st("Stopped")},
        "automatic_off": {"input_boolean.auto_carlo": st("off")},
        "program_off": {
            "climate.trv_vtherm_carlo": st(
                "off", {"requested_state": {"hvac_mode": "off"}, "preset_mode": "comfort", "temperature": 19.0, "specific_states": {"inhibition": {"inhibited": True}}}
            )
        },
    }
    for name, overrides in cases.items():
        assert c.unsafe_during_start(collect(stopped_world(**overrides)).snapshot, NOW, CARLO.policy, phase=phase), name


def test_the_window_grace_applies_only_once_the_pdc_is_heating():
    opened = {"binary_sensor.window_carlo": st("on", changed=NOW - 30)}
    unknown = {"binary_sensor.window_carlo": st("unavailable", changed=NOW - 30)}
    long_open = {"binary_sensor.window_carlo": st("on", changed=NOW - 121)}
    for phase in ("waiting_radiator", "waiting_target"):
        for overrides in (opened, unknown, long_open):
            assert c.unsafe_during_start(collect(stopped_world(**overrides)).snapshot, NOW, CARLO.policy, phase=phase)
    assert not c.unsafe_during_start(collect(stopped_world(**opened)).snapshot, NOW, CARLO.policy, phase="waiting_heat")  # open for less than 120 s
    assert c.unsafe_during_start(collect(stopped_world(**long_open)).snapshot, NOW, CARLO.policy, phase="waiting_heat")
    assert c.unsafe_during_start(collect(stopped_world(**unknown)).snapshot, NOW, CARLO.policy, phase="waiting_heat")  # unknown is not closed


def test_the_start_gap_and_the_capacity_reserve_are_not_protections_of_a_running_start():
    # the PDC's own consumption (above the 1100 W start limit) must not abort the hand-over
    busy = collect(stopped_world(**{"sensor.power": st("2000", reported=NOW - 1)})).snapshot
    assert not c.unsafe_during_start(busy, NOW, CARLO.policy, phase="waiting_heat")
    warm = collect(stopped_world(**{"sensor.temp_carlo": st("18.9", reported=NOW - 1)})).snapshot
    assert not c.unsafe_during_start(warm, NOW, CARLO.policy, phase="waiting_target")


def test_a_radiator_reported_not_inhibited_while_this_transaction_owns_the_inhibition_is_a_second_source():
    snap = collect(world()).snapshot  # not inhibited
    owning = tr.PdcTransaction("carlo", token=1, phase="running", inhibition_owned=True)
    assert c.radiator_released_while_running(snap, owning)
    assert not c.radiator_released_while_running(snap, dataclasses.replace(owning, inhibition_owned=False))
    unknown = dataclasses.replace(snap, radiator_inhibited=None)
    assert not c.radiator_released_while_running(unknown, owning)  # an unreadable flag is not evidence
    assert not c.radiator_released_while_running(collect(stopped_world()).snapshot, owning)


# --- diagnostics --------------------------------------------------------------------------------


def test_the_diagnostics_have_the_contract_shape_and_are_json_safe():
    snap = collect(world()).snapshot
    decision = policy.evaluate_pdc(snap, policy.PdcStartContext(NOW - 200, False), NOW, CARLO.policy)
    tx = tr.PdcTransaction("carlo", token=4, phase="running", started_at=NOW - 60, inhibition_owned=True, inhibited_before=False, reservation_held=True)
    ov = prov.OverrideState(True, NOW - 10, NOW + 7200, "user_command", "user")
    attrs = c.build_pdc_diagnostics(
        room_key="carlo",
        mode="control",
        owner="controller",
        decision=decision,
        snapshot=snap,
        tx=tx,
        reservation=tr.PdcReservation("carlo", 4),
        override=ov,
        policy=CARLO.policy,
        evaluated_at=NOW,
        generation=7,
        last_error="x" * 400,
        suspension=None,
        blocked_by=("automation.chooser_carlo",),
        split_action="heating",
    )
    assert attrs["schema_version"] == 1 and attrs["proposed_action"] == "heat" and attrs["reason"] == "start"
    assert attrs["power_limit_w"] == 1100.0 and attrs["stability_s"] == 120.0
    assert attrs["transaction"]["id"] == 4 and attrs["transaction"]["phase"] == "running" and attrs["transaction"]["inhibition_owned"] is True
    assert attrs["reservation"] == {"holder": "carlo"} and attrs["managed"] is True
    assert attrs["override"] == {"active": True, "until": NOW + 7200, "source": "user_command", "provenance": "user"}
    assert attrs["blocked_by"] == ["automation.chooser_carlo"] and attrs["split_action"] == "heating" and len(attrs["last_error"]) == c.MAX_ERROR_LENGTH
    assert {"id", "status", "reason_code", "actual", "expected", "deadline", "unit"} <= set(attrs["conditions"][0])
    assert json.loads(json.dumps(attrs, allow_nan=False)) == attrs  # no NaN, no Infinity, plain types only


def test_the_proposed_action_uses_the_contract_vocabulary():
    snap = collect(world()).snapshot

    def action(name: str) -> Any:
        return types.SimpleNamespace(action=name)

    assert [c.proposed_action(action(a)) for a in ("start", "stop", "none", "set_setpoint")] == ["heat", "off", "none", "none"]
    assert c.proposed_action(None) == "none"
    assert snap is not None


def test_empty_diagnostics_and_json_safe():
    empty = c.empty_pdc_diagnostics("carlo", "observe", owner="observer", last_error="boom")
    assert empty["transaction"]["phase"] == "idle" and empty["last_error"] == "boom" and empty["proposed_action"] == "none"
    assert c.json_safe({"a": (1, float("nan"), {"b": float("inf")}), "c": object}) == {"a": [1, None, {"b": None}], "c": str(object)}
    deep: Any = 1
    for _ in range(12):
        deep = {"x": deep}
    assert json.dumps(c.json_safe(deep)).count("{") <= 7


# --- parity with the real Jinja templates (optional) -------------------------------------------


def test_the_collector_snapshot_equals_the_one_the_baseline_templates_describe_on_a_seeded_sample():
    pytest.importorskip("jinja2")
    done = subprocess.run([sys.executable, "-I", str(HARNESS_PATH), "--random", "120", "--seed", "20261126"], capture_output=True, text=True, cwd=REPO_ROOT, check=False)
    assert done.returncode == 0, done.stderr[-2500:]
    result = json.loads(done.stdout)
    for room in ("camera_carlo", "camera_diego_luisa"):
        assert result[room]["checked"]["snapshots"] >= 116 + 486 + 120 and result[room]["checked"]["radiator_true"] > 10


def test_the_collector_is_pure():
    tree = ast.parse((COMPONENT_DIR / "heating_pdc_collector.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or "").split(".")[0])
    allowed = {"__future__", "json", "math", "re", "collections", "dataclasses", "datetime", "typing", ".heating_pdc_policy", ".heating_pdc_provenance", ".heating_pdc_transaction"}
    assert imported <= allowed, imported
    assert math.isfinite(1.0)
