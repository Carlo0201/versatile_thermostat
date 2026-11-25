"""Pure tests of the per-room status projection (``heating_room_status.py``).

The modules are loaded inside a synthetic package that points at the component directory, so the
relative imports work and the component ``__init__`` (which imports Home Assistant) is never run.
Only pytest is needed, from the repository root::

    python -m pytest -q --noconftest --import-mode=importlib -p no:cacheprovider tests/test_heating_room_status.py

No Home Assistant, no real entity ids: every id below is a placeholder.
"""

from __future__ import annotations

import copy
import dataclasses
import importlib
import json
import math
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_DIR = REPO_ROOT / "custom_components" / "versatile_thermostat"
BASELINE_DIR = REPO_ROOT / "plans" / "heating-orchestration-2026-11-24" / "baseline"
PKG = "_vtherm_room_status_pure_pkg_under_test"


def load(name: str) -> types.ModuleType:
    if PKG not in sys.modules:
        package = types.ModuleType(PKG)
        package.__path__ = [str(COMPONENT_DIR)]
        sys.modules[PKG] = package
    return importlib.import_module(f"{PKG}.{name}")


collector = load("heating_boiler_collector")
policy = load("heating_boiler_policy")
rs = load("heating_room_status")

NOW = 1_800_000_000.0
LOCAL_TZ = timezone(timedelta(hours=1))
COUNT, CENTRAL, MASTER = "sensor.valves_active", "select.fixture_central_mode", "climate.boiler_master"
SECTION: dict[str, Any] = {
    "mode": "observe",
    "master_entity_id": MASTER,
    "central_mode_entity_id": CENTRAL,
    "active_count_entity_id": COUNT,
    "manual_request_entity_id": "binary_sensor.manual_request",
    "pilot_filter_entity_id": "input_boolean.pilot_filter",
    "pilot_ready_entity_id": "binary_sensor.pilot_ready",
    "consent_entity_ids": ["input_boolean.consent_a"],
    "rooms": [
        {
            "room_key": "bedroom",
            "climate_entity_id": "climate.vt_bedroom",
            "pilot_filter": False,
            "pdc": {"split_entity_id": "climate.bedroom_split", "session_entity_id": "input_boolean.bedroom_pdc"},
        },
        {"room_key": "living", "climate_entity_id": "climate.vt_living", "pilot_filter": False},
        {"room_key": "study", "climate_entity_id": "climate.vt_study", "pilot_filter": False},
    ],
}
CONFIG = collector.parse_orchestration_config(SECTION)
ROOMS = {room.room_key: room for room in CONFIG.rooms}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def vtherm(
    valves: list[str], *, age: float = 60.0, current: Any = 18.0, target: Any = 20.0, state: str = "heat", demand: Any = True, now: float = NOW, **over: Any
) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "is_ready": True,
        "hvac_action": "heating",
        "current_temperature": current,
        "temperature": target,
        "specific_states": {"is_initialized": True, "last_temperature_datetime": _iso(now - age), "inhibition": {"inhibited": False}},
        "configuration": {"is_used_by_central_boiler": True},
        "vtherm_over_climate": {"underlying_entities": valves, "avatto_room_demand": demand},
        "window_manager": {"window_sensor_entity_id": "binary_sensor.window_default", "window_state": "off", "is_window_bypass": False},
        "safety_manager": {"safety_state": "off"},
        "power_manager": {"overpowering_state": "off"},
    }
    attributes.update(over)
    return {"state": state, "attributes": attributes}


def snapshot(now: float = NOW, **changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        COUNT: {"state": "3", "attributes": {"active_device_ids": ["climate.v_bed", "climate.v_liv_1", "climate.v_liv_2"]}},
        CENTRAL: {"state": "Heat only"},
        MASTER: {"state": "off"},
        "binary_sensor.manual_request": {"state": "off"},
        "input_boolean.pilot_filter": {"state": "off"},
        "binary_sensor.pilot_ready": {"state": "on"},
        "input_boolean.consent_a": {"state": "on"},
        "input_boolean.bedroom_pdc": {"state": "off"},
        "climate.bedroom_split": {"state": "off"},
        "binary_sensor.window_default": {"state": "off"},
        "climate.v_bed": {"state": "heat", "attributes": {"hvac_action": "heating"}},
        "climate.v_liv_1": {"state": "heat", "attributes": {"hvac_action": "heating"}},
        "climate.v_liv_2": {"state": "auto", "attributes": {"hvac_action": "heating"}},
        "climate.vt_bedroom": vtherm(["climate.v_bed"], now=now),
        "climate.vt_living": vtherm(["climate.v_liv_1", "climate.v_liv_2"], now=now),
        "climate.vt_study": vtherm([], now=now, state="off"),
    }
    data.update(changes)
    return data


def policy_snapshot(collection):
    raw = collection.raw
    return policy.BoilerSnapshot(
        ok=bool(raw["ok"]),
        master=raw["master"],
        count=int(raw["count"]),
        rooms=tuple(policy.QualifiedBoilerRoom(slug=r["slug"], target=r["target"], gap=r["gap"], pilot_ok=r["pilot_ok"]) for r in raw["rooms"]),
        manual=raw["manual"],
        pilot=raw["pilot"],
        pilot_ready=raw["pilot_ready"],
        consent=raw["consent"],
    )


OBSERVE = rs.ControllerView("observe", "observer", "no_demand")


def run(states: dict[str, Any], room_key: str = "living", *, pdc: Any = None, controller: Any = OBSERVE, now: float = NOW, **kwargs: Any):
    collection = collector.collect_boiler_inputs(states, CONFIG, now, local_tz=LOCAL_TZ, **kwargs)
    decision = policy.evaluate_boiler(policy_snapshot(collection), None, now)
    return (
        rs.evaluate_room_status(ROOMS[room_key], states, collection, decision, pdc, controller, now, local_tz=LOCAL_TZ, active_count_entity_id=CONFIG.active_count_entity_id),
        collection,
        decision,
    )


def cond(status, condition_id: str):
    matches = [item for item in status.conditions if item.id == condition_id]
    assert len(matches) == 1, f"{condition_id}: {[c.id for c in status.conditions]}"
    return matches[0]


# ---------------------------------------------------------------------------
# Healthy room and the collector projection.
# ---------------------------------------------------------------------------


def test_healthy_room_has_every_ready_and_demand_condition_passing():
    status, collection, _ = run(snapshot())
    for condition_id in (
        "room_available",
        "hvac_enabled",
        "room_ready",
        "initialized",
        "central_mode",
        "window",
        "inhibition",
        "safety",
        "overpowering",
        "temperature",
        "room_demand",
        "valve_active",
    ):
        assert cond(status, condition_id).status == "pass", condition_id
    assert not any(item.id == "pdc_released" for item in status.conditions)
    assert [stage.key for stage in status.stages] == ["ready", "demand", "boiler"]
    assert status.stages[0].status == "pass" and status.stages[0].passed == status.stages[0].total == 10
    assert status.gap == 2.0 and status.start_gap == 0.3
    assert any(room.room_key == "living" for room in collection.rooms)


def test_a_room_with_pdc_gets_the_released_condition_and_the_pump_stage():
    status, _, _ = run(snapshot(), "bedroom")
    assert cond(status, "pdc_released").status == "pass"
    assert [stage.key for stage in status.stages] == ["ready", "demand", "boiler", "pump"]
    assert cond(status, "pump_controller").status == "unknown" and cond(status, "pump_controller").reason_code == "pdc_unavailable"


CASES = [
    # (name, states mutation, room, condition id, status, reason_code)
    ("unavailable", {"climate.vt_living": {"state": "unavailable", "attributes": {}}}, "living", "room_available", "fail", "entity_unavailable"),
    ("hvac off", {"climate.vt_living": vtherm(["climate.v_liv_1"], state="off")}, "living", "hvac_enabled", "fail", "hvac_not_heating"),
    ("not ready", {"climate.vt_living": vtherm(["climate.v_liv_1"], is_ready=False)}, "living", "room_ready", "fail", "not_ready"),
    (
        "not initialized",
        {
            "climate.vt_living": vtherm(
                ["climate.v_liv_1"], specific_states={"is_initialized": False, "last_temperature_datetime": _iso(NOW - 60), "inhibition": {"inhibited": False}}
            )
        },
        "living",
        "initialized",
        "fail",
        "not_initialized",
    ),
    ("central off", {CENTRAL: {"state": "Off"}}, "living", "central_mode", "fail", "central_off"),
    ("central unknown", {CENTRAL: {"state": "unknown"}}, "living", "central_mode", "unknown", "central_mode_unknown"),
    (
        "window open",
        {
            "binary_sensor.window_default": {"state": "on"},
            "climate.vt_living": vtherm(
                ["climate.v_liv_1"], window_manager={"window_sensor_entity_id": "binary_sensor.window_default", "window_state": "on", "is_window_bypass": False}
            ),
        },
        "living",
        "window",
        "fail",
        "window_open",
    ),
    ("window pending", {"binary_sensor.window_default": {"state": "on"}}, "living", "window", "wait", "window_pending"),
    ("window unknown", {"binary_sensor.window_default": {"state": "unavailable"}}, "living", "window", "unknown", "window_unknown"),
    (
        "window bypass",
        {
            "binary_sensor.window_default": {"state": "on"},
            "climate.vt_living": vtherm(
                ["climate.v_liv_1"], window_manager={"window_sensor_entity_id": "binary_sensor.window_default", "window_state": "off", "is_window_bypass": True}
            ),
        },
        "living",
        "window",
        "pass",
        None,
    ),
    (
        "inhibited",
        {
            "climate.vt_living": vtherm(
                ["climate.v_liv_1"], specific_states={"is_initialized": True, "last_temperature_datetime": _iso(NOW - 60), "inhibition": {"inhibited": True}}
            )
        },
        "living",
        "inhibition",
        "fail",
        "inhibited",
    ),
    ("safety", {"climate.vt_living": vtherm(["climate.v_liv_1"], safety_manager={"safety_state": "on"})}, "living", "safety", "fail", "safety"),
    ("overpowering", {"climate.vt_living": vtherm(["climate.v_liv_1"], power_manager={"overpowering_state": "on"})}, "living", "overpowering", "fail", "overpowering"),
    ("temperature stale", {"climate.vt_living": vtherm(["climate.v_liv_1"], age=4000.0)}, "living", "temperature", "fail", "temperature_stale"),
    ("temperature future", {"climate.vt_living": vtherm(["climate.v_liv_1"], age=-30.0)}, "living", "temperature", "fail", "temperature_future"),
    ("temperature missing", {"climate.vt_living": vtherm(["climate.v_liv_1"], current=None)}, "living", "temperature", "unknown", "temperature_missing"),
    ("demand false", {"climate.vt_living": vtherm(["climate.v_liv_1"], demand=False)}, "living", "room_demand", "fail", "avatto_no_demand"),
    ("demand continuing", {"climate.vt_living": vtherm(["climate.v_liv_1"], current=19.8)}, "living", "room_demand", "pass", "continuing_demand"),
    ("demand fallback to action", {"climate.vt_living": vtherm(["climate.v_liv_1"], demand=None)}, "living", "room_demand", "pass", "trv_request"),
    ("demand unknown", {"climate.vt_living": vtherm(["climate.v_liv_1"], demand=None, hvac_action="idle")}, "living", "room_demand", "unknown", "demand_unknown"),
    (
        "no active valve",
        {"climate.v_liv_1": {"state": "heat", "attributes": {"hvac_action": "idle"}}, "climate.v_liv_2": {"state": "heat", "attributes": {"hvac_action": "idle"}}},
        "living",
        "valve_active",
        "fail",
        "no_active_valve",
    ),
    (
        "valve read even when excluded",
        {CENTRAL: {"state": "Off"}, "climate.vt_living": vtherm(["climate.v_liv_1"], safety_manager={"safety_state": "on"})},
        "living",
        "valve_active",
        "pass",
        None,
    ),
    (
        "no valve and excluded",
        {"climate.vt_living": vtherm(["climate.v_liv_1"], safety_manager={"safety_state": "on"}), "climate.v_liv_1": {"state": "heat", "attributes": {"hvac_action": "idle"}}},
        "living",
        "valve_active",
        "fail",
        "no_active_valve",
    ),
    ("valve with invalid count", {COUNT: {"state": "9", "attributes": {"active_device_ids": ["climate.v_liv_1"]}}}, "living", "valve_active", "unknown", "count_invalid"),
    ("valve without count ids", {COUNT: {"state": "3", "attributes": {}}}, "living", "valve_active", "unknown", "count_invalid"),
    ("pdc session active", {"input_boolean.bedroom_pdc": {"state": "on"}}, "bedroom", "pdc_released", "fail", "pdc_session_active"),
    ("pdc split not off", {"climate.bedroom_split": {"state": "heat"}}, "bedroom", "pdc_released", "fail", "split_not_off"),
    ("pdc session unknown", {"input_boolean.bedroom_pdc": {"state": "unavailable"}}, "bedroom", "pdc_released", "unknown", "pdc_session_unknown"),
]


@pytest.mark.parametrize("name,changes,room,condition_id,status,reason_code", CASES, ids=[c[0] for c in CASES])
def test_condition_table(name, changes, room, condition_id, status, reason_code):
    states = snapshot()
    states.update(copy.deepcopy(changes))
    result, _, _ = run(states, room)
    item = cond(result, condition_id)
    assert (item.status, item.reason_code) == (status, reason_code)


def test_unsupported_unit_fails_the_temperature_condition():
    result, _, _ = run(snapshot(), temperature_unit="°F")
    item = cond(result, "temperature")
    assert (item.status, item.reason_code) == ("fail", "unit_unsupported")


def test_unavailable_room_marks_the_other_checks_as_not_evaluated():
    states = snapshot()
    states["climate.vt_living"] = {"state": "unavailable", "attributes": {}}
    result, _, _ = run(states)
    assert result.heat_state == "unavailable"
    for condition_id in ("hvac_enabled", "room_ready", "window", "temperature", "room_demand"):
        item = cond(result, condition_id)
        assert (item.status, item.reason_code) == ("unknown", "not_evaluated")
    assert result.blocker.id == "room_available"


def test_a_room_outside_the_boiler_by_configuration_has_no_boiler_stage():
    states = snapshot()
    states["climate.vt_living"] = vtherm(["climate.v_liv_1"], configuration={"is_used_by_central_boiler": False})
    result, _, _ = run(states)
    boiler = [item for item in result.conditions if item.stage == "boiler"]
    assert boiler and all(item.status == "na" for item in boiler)
    assert next(stage for stage in result.stages if stage.key == "boiler").status == "na"
    assert result.blocker is None or result.blocker.stage != "boiler"


def test_m1_conditions_are_copied_with_their_units_and_deadlines():
    result, _, decision = run(snapshot())
    copied = {item.id: item for item in result.conditions if item.stage == "boiler" and item.id in {c.id for c in decision.conditions}}
    assert set(copied) == {c.id for c in decision.conditions}
    for original in decision.conditions:
        item = copied[original.id]
        assert (item.status, item.reason_code, item.deadline) == (original.status, original.reason_code, original.deadline)
        assert item.unit == collector.CONDITION_UNITS.get(original.id)


# ---------------------------------------------------------------------------
# Stages and blocker.
# ---------------------------------------------------------------------------


def _c(id_, stage, status, code=None):
    return rs.RoomCondition(id_, stage, status, code)


def test_stage_status_counts_and_na():
    conditions = [_c("a", "ready", "pass"), _c("b", "ready", "na"), _c("c", "ready", "observe"), _c("d", "demand", "wait"), _c("e", "demand", "unknown"), _c("f", "boiler", "na")]
    stages = {stage.key: stage for stage in rs.build_stages(conditions, False)}
    assert (stages["ready"].status, stages["ready"].passed, stages["ready"].total) == ("pass", 2, 2)
    assert (stages["demand"].status, stages["demand"].passed, stages["demand"].total) == ("wait", 0, 2)
    assert (stages["boiler"].status, stages["boiler"].total) == ("na", 0)
    assert "pump" not in stages
    assert "pump" in {s.key for s in rs.build_stages(conditions, True)}


def test_blocker_is_the_first_fail_or_wait_in_chain_order_then_unknown():
    conditions = [_c("r1", "ready", "unknown"), _c("d1", "demand", "wait", "x"), _c("b1", "boiler", "fail"), _c("p1", "pump", "fail")]
    assert rs.find_blocker(conditions).id == "d1"
    only_unknown = [_c("r1", "ready", "unknown", "u"), _c("b1", "boiler", "pass")]
    assert rs.find_blocker(only_unknown).id == "r1"
    assert rs.find_blocker([_c("r1", "ready", "pass"), _c("b1", "boiler", "observe")]) is None


def test_the_pump_never_names_the_blocker_of_a_radiator_start():
    conditions = [_c("r1", "ready", "pass"), _c("p1", "pump", "fail", "power_high"), _c("p2", "pump", "wait")]
    assert rs.find_blocker(conditions) is None


# ---------------------------------------------------------------------------
# Heat state explanation.
# ---------------------------------------------------------------------------


def test_radiator_heating_with_the_boiler_on():
    result, _, _ = run(snapshot(**{MASTER: {"state": "heat"}}))
    assert (result.heat_state, result.reason, result.source) == ("radiator_heating", "radiator_heating", "radiator")


def test_waiting_for_the_boiler_reports_the_specific_reason_and_deadline():
    result, _, decision = run(snapshot())
    assert result.heat_state == "boiler_waiting"
    assert result.reason in {"boiler_stability", "boiler_single", "boiler_cooldown", "boiler_consent", "boiler_off", "boiler_diagnostic"}
    assert result.deadline == decision.deadline
    assert result.source == "radiator"


def test_at_target_and_small_gap_and_regulating():
    at_target, _, _ = run(snapshot(**{"climate.vt_living": vtherm(["climate.v_liv_1"], current=20.0, demand=None, hvac_action="idle")}))
    assert (at_target.heat_state, at_target.reason) == ("at_target", "at_target")
    small, _, _ = run(snapshot(**{"climate.vt_living": vtherm(["climate.v_liv_1"], current=19.8, demand=False, hvac_action="idle")}))
    assert (small.heat_state, small.reason) == ("below_start_threshold", "small_gap")
    regulating, _, _ = run(snapshot(**{"climate.vt_living": vtherm(["climate.v_liv_1"], current=18.0, demand=False, hvac_action="idle")}))
    assert (regulating.heat_state, regulating.reason) == ("regulating", "demand_standby")


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"climate.vt_living": vtherm(["climate.v_liv_1"], safety_manager={"safety_state": "on"})}, "safety"),
        ({"climate.vt_living": vtherm(["climate.v_liv_1"], power_manager={"overpowering_state": "on"})}, "power"),
        ({"climate.vt_living": vtherm(["climate.v_liv_1"], state="off", specific_states={"is_initialized": True, "hvac_off_reason": "hvac_off_central_mode"})}, "central_off"),
        ({"climate.vt_living": vtherm(["climate.v_liv_1"], state="off", specific_states={"is_initialized": True, "hvac_off_reason": "hvac_off_auto_start_stop"})}, "auto_pause"),
        ({"climate.vt_living": vtherm(["climate.v_liv_1"], state="off", demand=False, hvac_action="idle")}, "off"),
        ({"climate.vt_living": vtherm(["climate.v_liv_1"], state="cool", demand=False, hvac_action="idle")}, "other_mode"),
        ({"climate.vt_living": vtherm(["climate.v_liv_1"], is_ready=False)}, "initializing"),
        ({"climate.vt_living": {"state": "unavailable", "attributes": {}}}, "unavailable"),
        (
            {
                "climate.vt_living": vtherm(
                    ["climate.v_liv_1"], window_manager={"window_sensor_entity_id": "binary_sensor.window_default", "window_state": "on", "is_window_bypass": False}
                ),
                "binary_sensor.window_default": {"state": "on"},
            },
            "window",
        ),
    ],
)
def test_room_wide_states(changes, expected):
    result, _, _ = run(snapshot(**changes))
    assert result.heat_state == expected


def test_a_window_that_closed_is_reported_as_closing():
    states = snapshot(**{"climate.vt_living": vtherm(["climate.v_liv_1"], state="off", specific_states={"is_initialized": True, "hvac_off_reason": "hvac_off_window_detection"})})
    result, _, _ = run(states)
    assert (result.heat_state, result.reason) == ("window", "window_closing")


def pdc_view(
    *,
    mode="control",
    owner="controller",
    managed=True,
    phase="waiting_heat",
    split_mode="off",
    split_action="idle",
    conditions=(),
    diagnostic="idle",
    now=NOW,
    failure=None,
    state="start",
):
    return rs.PdcView(
        state,
        {
            "schema_version": 1,
            "mode": mode,
            "owner": owner,
            "evaluated_at": now - 1.0,
            "managed": managed,
            "split_mode": split_mode,
            "split_action": split_action,
            "diagnostic_state": diagnostic,
            "deadline": now + 30.0,
            "transaction": {"phase": phase, "started_at": now - 10.0, "deadline": now + 30.0, "failure": failure},
            "conditions": list(conditions),
        },
    )


def test_pump_transition_uses_the_phase_and_keeps_room_conditions_as_details():
    states = snapshot(**{"climate.vt_bedroom": vtherm(["climate.v_bed"], safety_manager={"safety_state": "on"})})
    result, _, _ = run(states, "bedroom", pdc=pdc_view())
    assert (result.heat_state, result.reason, result.source) == ("pump_transition", "waiting_split", "pdc")
    assert "safety" in result.detail_reasons
    assert result.pump["phase"] == "waiting_heat"


def test_pump_heating_measured_in_observe_and_serving_when_managed():
    measured, _, _ = run(snapshot(), "bedroom", pdc=pdc_view(mode="observe", owner="observer", managed=False, phase="idle", split_mode="heat", split_action="heating"))
    assert (measured.heat_state, measured.reason) == ("pump_heating", "pump_measured")
    serving, _, _ = run(snapshot(), "bedroom", pdc=pdc_view(phase="running", split_mode="heat", split_action="heating"))
    assert (serving.heat_state, serving.reason) == ("pump_heating", "pump_serving")


def test_pump_selected_and_unknown_reason_when_the_split_is_not_in_heat():
    selected, _, _ = run(snapshot(), "bedroom", pdc=pdc_view(phase="running", split_mode="heat", split_action="idle"))
    assert (selected.heat_state, selected.reason) == ("pump_selected", "pump_selected")
    other, _, _ = run(snapshot(), "bedroom", pdc=pdc_view(phase="running", split_mode="off", split_action="idle"))
    assert (other.heat_state, other.reason) == ("pump_selected", "unknown_reason")


def test_pump_conditions_are_forwarded_and_pump_reasons_explain_a_blocked_start():
    conditions = [
        {"id": "automatic", "status": "pass", "reason_code": None, "actual": "on", "expected": "on", "deadline": None, "unit": None},
        {"id": "power_capacity", "status": "fail", "reason_code": "power_high", "actual": 3500, "expected": "<=3000", "deadline": None, "unit": "W"},
    ]
    view = pdc_view(mode="observe", owner="observer", managed=False, phase="idle", conditions=conditions)
    result, _, _ = run(snapshot(), "bedroom", pdc=view)
    pump = [item for item in result.conditions if item.stage == "pump"]
    assert [(item.id, item.status, item.unit) for item in pump] == [("automatic", "pass", None), ("power_capacity", "fail", "W")]
    assert next(stage for stage in result.stages if stage.key == "pump").status == "fail"
    assert result.blocker is None or result.blocker.stage != "pump"
    assert "power_high" in (result.reason, *result.detail_reasons)


def test_pdc_unavailable_reports_diagnostics_missing_and_unknown_pump_row():
    result, _, _ = run(snapshot(), "bedroom", pdc=None)
    assert "diagnostics_missing" in (result.reason, *result.detail_reasons)
    assert cond(result, "pump_controller").reason_code == "pdc_unavailable"
    stale = pdc_view(now=NOW - 600)
    again, _, _ = run(snapshot(), "bedroom", pdc=stale)
    assert cond(again, "pump_controller").status == "unknown"


def test_controller_row_never_passes_in_observe():
    observe, _, _ = run(snapshot())
    assert cond(observe, "controller").status == "observe"
    owner, _, _ = run(snapshot(), controller=rs.ControllerView("control", "controller", "heat"))
    assert cond(owner, "controller").status == "pass"
    external, _, _ = run(snapshot(), controller=rs.ControllerView("control", "observer", "off"))
    assert (cond(external, "controller").status, cond(external, "controller").reason_code) == ("fail", "not_owner")
    assert cond(run(snapshot(), controller=None)[0], "controller").status == "unknown"


# ---------------------------------------------------------------------------
# Attributes, invariants, non regression.
# ---------------------------------------------------------------------------


def test_attributes_are_finite_json_and_below_the_size_limit_with_every_condition():
    conditions = [
        {"id": cid, "status": "wait", "reason_code": "power_high", "actual": 123456.789, "expected": "<=3000", "deadline": NOW + 5, "unit": "W"}
        for cid in (
            "automatic",
            "override",
            "central_mode",
            "program",
            "preset",
            "window_closed",
            "window_running",
            "temperature",
            "thermal_gap",
            "power_capacity",
            "power_stop",
            "other_pdc",
            "split_free",
            "split_heat",
            "schedule",
            "cooldown",
            "stability",
            "thermal_stop",
            "min_on",
            "setpoint",
        )
    ]
    result, _, _ = run(snapshot(), "bedroom", pdc=pdc_view(conditions=conditions))
    attributes = rs.room_attributes(result, mode="observe", evaluated_at=NOW, generation=7)
    text = json.dumps(attributes, allow_nan=False, separators=(",", ":"), ensure_ascii=False)
    assert len(text.encode()) <= rs.MAX_ATTRIBUTE_BYTES
    assert len(text.encode()) > 6000  # the worst case is really exercised
    assert attributes["schema_version"] == 1 and attributes["generation"] == 7
    assert set(attributes) == {
        "schema_version",
        "mode",
        "evaluated_at",
        "generation",
        "room_key",
        "climate_entity_id",
        "heat_state",
        "reason",
        "detail_reasons",
        "source",
        "current",
        "target",
        "gap",
        "start_gap",
        "deadline",
        "stages",
        "blocker",
        "conditions",
        "pump",
        "last_error",
    }


def test_non_finite_values_never_reach_the_attributes():
    states = snapshot(**{"climate.vt_living": vtherm(["climate.v_liv_1"], current=float("inf"))})
    result, _, _ = run(states)
    attributes = rs.room_attributes(result, mode="observe", evaluated_at=NOW, generation=1)
    json.dumps(attributes, allow_nan=False)
    assert result.current is None and result.gap is None


def test_oversized_attributes_raise_instead_of_being_published(monkeypatch):
    result, _, _ = run(snapshot())
    monkeypatch.setattr(rs, "MAX_ATTRIBUTE_BYTES", 100)
    with pytest.raises(ValueError):
        rs.room_attributes(result, mode="observe", evaluated_at=NOW, generation=1)


def test_evaluation_is_deterministic_and_does_not_touch_its_inputs():
    states = snapshot()
    before = copy.deepcopy(states)
    first, collection, decision = run(states)
    second, _, _ = run(states)
    assert first == second
    assert states == before
    again = rs.evaluate_room_status(ROOMS["living"], states, collection, decision, None, OBSERVE, NOW, local_tz=LOCAL_TZ, active_count_entity_id=CONFIG.active_count_entity_id)
    assert again == first


def test_the_clock_must_be_finite_and_shared():
    states = snapshot()
    collection = collector.collect_boiler_inputs(states, CONFIG, NOW, local_tz=LOCAL_TZ)
    decision = policy.evaluate_boiler(policy_snapshot(collection), None, NOW)
    for bad in (math.nan, math.inf, True):
        with pytest.raises(ValueError):
            rs.evaluate_room_status(ROOMS["living"], states, collection, decision, None, OBSERVE, bad)
    other = policy.evaluate_boiler(policy_snapshot(collection), None, NOW + 5)
    with pytest.raises(ValueError):
        rs.evaluate_room_status(ROOMS["living"], states, collection, other, None, OBSERVE, NOW)


def test_the_module_imports_no_home_assistant_and_no_fixed_entity_id():
    source = (COMPONENT_DIR / "heating_room_status.py").read_text(encoding="utf-8")
    assert "homeassistant" not in source
    import re

    assert not re.search(r"['\"](climate|sensor|select|timer|binary_sensor|input_boolean|switch)\.[a-z0-9_]+['\"]", source)


def test_heat_state_and_reason_codes_are_in_the_contract():
    contract = (REPO_ROOT / "plans" / "heating-orchestration-2026-11-24" / "ROOM-CONDITIONS-CONTRATTO.md").read_text(encoding="utf-8")
    section = contract.split("## 4. Valori di `reason`")[1].split("## 5.")[0]
    allowed = set(__import__("re").findall(r"`([a-z_]+)`", section))
    states = snapshot()
    for case in (
        {},
        {MASTER: {"state": "heat"}},
        {"climate.vt_living": vtherm(["climate.v_liv_1"], current=20.0, demand=None, hvac_action="idle")},
        {"climate.vt_living": vtherm(["climate.v_liv_1"], safety_manager={"safety_state": "on"})},
    ):
        result, _, _ = run({**states, **copy.deepcopy(case)})
        assert result.heat_state in rs.HEAT_STATES
        assert result.reason in allowed, result.reason
        assert set(result.detail_reasons) <= allowed


def test_empty_attributes_for_disabled_starting_error():
    attributes = rs.empty_room_attributes("living", "climate.vt_living", mode="disabled", evaluated_at=None, generation=0)
    assert attributes["conditions"] == [] and attributes["stages"] == [] and attributes["blocker"] is None
    json.dumps(attributes, allow_nan=False)


def test_publication_signature_ignores_the_clock_and_small_gap_noise_but_not_a_status_change():
    result, _, _ = run(snapshot())
    first = rs.room_attributes(result, mode="observe", evaluated_at=NOW, generation=1)
    second = rs.room_attributes(result, mode="observe", evaluated_at=NOW + 10, generation=2)
    assert rs.room_publication_signature(result.heat_state, first) == rs.room_publication_signature(result.heat_state, second)
    changed, _, _ = run(snapshot(**{CENTRAL: {"state": "Off"}}))
    third = rs.room_attributes(changed, mode="observe", evaluated_at=NOW, generation=1)
    assert rs.room_publication_signature(changed.heat_state, third) != rs.room_publication_signature(result.heat_state, first)


# ---------------------------------------------------------------------------
# Non regression: the projection never changes what the collector produced.
# ---------------------------------------------------------------------------

FIXTURES = json.loads((BASELINE_DIR / "boiler-observation-cases.json").read_text(encoding="utf-8"))
BASELINE_SECTION = {
    "mode": "observe",
    "master_entity_id": "climate.boiler_master",
    "central_mode_entity_id": "select.central_mode",
    "active_count_entity_id": "sensor.nb_device_active_for_boiler",
    "manual_request_entity_id": "binary_sensor.manual",
    "pilot_filter_entity_id": "input_boolean.pilot",
    "pilot_ready_entity_id": "binary_sensor.pilot_ready",
    "consent_entity_ids": ["input_boolean.consent_one", "binary_sensor.consent_two"],
    "rooms": [
        {"room_key": slug, "climate_entity_id": f"climate.trv_vtherm_{slug}", "pilot_filter": slug in ("camera_carlo", "bagno_blu")}
        for slug in ("camera_carlo", "ingresso", "cucina", "soggiorno", "camera_laura_livia", "bagno_verde", "bagno_blu", "camera_diego_luisa")
    ],
}


@pytest.mark.parametrize("fixture", FIXTURES["fixtures"], ids=lambda fixture: fixture["name"])
def test_projection_never_changes_the_collector_output_on_the_baseline_fixtures(fixture):
    config = collector.parse_orchestration_config(BASELINE_SECTION)
    states = copy.deepcopy(fixture["snapshot"])
    now = 2000.0
    collection = collector.collect_boiler_inputs(states, config, now)
    before = (copy.deepcopy(collection.raw), [r.as_dict() for r in collection.rooms], [r.as_dict() for r in collection.excluded_rooms], dict(collection.inputs))
    decision = policy.evaluate_boiler(policy_snapshot(collection), None, now)
    diagnostics_before = collector.build_boiler_diagnostics(collection, decision, mode="observe", owner="observer", generation=1, cycle_origin="new_observation")
    for room in config.rooms:
        result = rs.evaluate_room_status(room, states, collection, decision, None, OBSERVE, now, active_count_entity_id=config.active_count_entity_id)
        rs.room_attributes(result, mode="observe", evaluated_at=now, generation=1)
        assert result.heat_state in rs.HEAT_STATES
    after = (collection.raw, [r.as_dict() for r in collection.rooms], [r.as_dict() for r in collection.excluded_rooms], dict(collection.inputs))
    assert before == after
    assert states == fixture["snapshot"]
    assert collector.build_boiler_diagnostics(collection, decision, mode="observe", owner="observer", generation=1, cycle_origin="new_observation") == diagnostics_before
