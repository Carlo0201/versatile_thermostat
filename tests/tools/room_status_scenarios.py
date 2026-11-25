"""Scenario inputs for the parity between the Oikos card and ``heating_room_status``.

Pure Python, no Home Assistant, placeholder entity ids only. ``build_scenarios`` returns the inputs
(VTherm and helper states, the published boiler diagnostic, the published PDC diagnostic, the
controller view). ``export_card_reference.mjs`` runs the *current card code* on the same inputs and
writes the reference outputs; ``tests/test_heating_room_status_parity.py`` compares them with the
Python projection. Regenerate the inputs with::

    python tests/tools/room_status_scenarios.py tests/fixtures/room_status/scenarios.json
"""

from __future__ import annotations

import copy
import importlib
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPONENT_DIR = REPO_ROOT / "custom_components" / "versatile_thermostat"
PKG = "_vtherm_room_status_scenarios_pkg"

NOW = 1_800_000_000.0
COUNT, CENTRAL, MASTER = "sensor.valves_active", "select.fixture_central_mode", "climate.boiler_master"
BOILER_SENSOR, PDC_SENSOR = "sensor.fixture_boiler_orchestration", "sensor.fixture_pdc_bedroom"
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
        {"room_key": "bedroom", "climate_entity_id": "climate.vt_bedroom", "pilot_filter": False, "pdc": {"split_entity_id": "climate.bedroom_split", "session_entity_id": "input_boolean.bedroom_pdc"}},
        {"room_key": "living", "climate_entity_id": "climate.vt_living", "pilot_filter": False},
        {"room_key": "study", "climate_entity_id": "climate.vt_study", "pilot_filter": False},
    ],
}


def load_modules() -> tuple[Any, Any, Any]:
    if PKG not in sys.modules:
        package = types.ModuleType(PKG)
        package.__path__ = [str(COMPONENT_DIR)]
        sys.modules[PKG] = package
    return tuple(importlib.import_module(f"{PKG}.{name}") for name in ("heating_boiler_collector", "heating_boiler_policy", "heating_room_status"))


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def vtherm(valves: list[str], *, age: float = 60.0, current: Any = 18.0, target: Any = 20.0, state: str = "heat", demand: Any = True, **over: Any) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "is_ready": True,
        "hvac_action": "heating",
        "current_temperature": current,
        "temperature": target,
        "specific_states": {"is_initialized": True, "last_temperature_datetime": _iso(NOW - age), "inhibition": {"inhibited": False}},
        "configuration": {"is_used_by_central_boiler": True},
        "vtherm_over_climate": {"underlying_entities": valves, "avatto_room_demand": demand},
        "window_manager": {"window_sensor_entity_id": "binary_sensor.window_default", "window_state": "off", "is_window_bypass": False},
        "safety_manager": {"safety_state": "off"},
        "power_manager": {"overpowering_state": "off"},
    }
    attributes.update(over)
    return {"state": state, "attributes": attributes}


def base_states() -> dict[str, Any]:
    return {
        COUNT: {"state": "3", "attributes": {"active_device_ids": ["climate.v_bed", "climate.v_liv_1", "climate.v_liv_2"]}},
        CENTRAL: {"state": "Heat only"},
        MASTER: {"state": "off"},
        "binary_sensor.manual_request": {"state": "off"},
        "input_boolean.pilot_filter": {"state": "off"},
        "binary_sensor.pilot_ready": {"state": "on"},
        "input_boolean.consent_a": {"state": "on"},
        "input_boolean.bedroom_pdc": {"state": "off"},
        "climate.bedroom_split": {"state": "off", "attributes": {}},
        "binary_sensor.window_default": {"state": "off"},
        "climate.v_bed": {"state": "heat", "attributes": {"hvac_action": "heating"}},
        "climate.v_liv_1": {"state": "heat", "attributes": {"hvac_action": "heating"}},
        "climate.v_liv_2": {"state": "auto", "attributes": {"hvac_action": "heating"}},
        "climate.vt_bedroom": vtherm(["climate.v_bed"]),
        "climate.vt_living": vtherm(["climate.v_liv_1", "climate.v_liv_2"]),
        "climate.vt_study": vtherm([], state="off", demand=False, hvac_action="idle"),
    }


def pdc_attributes(*, mode: str = "control", owner: str | None = "controller", managed: bool = False, phase: str = "idle", split_mode: str = "off", split_action: str = "idle", conditions: list[dict[str, Any]] | None = None, diagnostic: str = "idle|1100|120", failure: str | None = None, started_at: float | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "room_key": "bedroom",
        "mode": mode,
        "owner": owner if mode == "control" else "observer",
        "evaluated_at": NOW - 1.0,
        "generation": 11,
        "managed": managed,
        "split_mode": split_mode,
        "split_action": split_action,
        "pdc_mode": "observe" if mode == "observe" else "control",
        "diagnostic_state": diagnostic,
        "power_limit_w": 1100,
        "deadline": NOW + 90.0 if diagnostic.startswith("ready") else None,
        "transaction": {"id": 3, "phase": phase, "started_at": started_at if started_at is not None else (NOW - 20.0 if phase != "idle" else None), "deadline": NOW + 40.0 if phase != "idle" else None, "failure": failure},
        "override": {"active": False, "until": None, "source": None, "provenance": "none"},
        "conditions": conditions or [],
    }


def pdc_condition(cid: str, status: str = "pass", code: str | None = None, actual: Any = None, expected: Any = None, unit: str | None = None) -> dict[str, Any]:
    return {"id": cid, "status": status, "reason_code": code, "actual": actual, "expected": expected, "deadline": None, "unit": unit}


def build_scenarios(statuses: dict[str, Any] | None = None) -> dict[str, Any]:
    """Scenario inputs; when ``statuses`` is given it receives the Python ``RoomStatus`` of each scenario."""
    collector, policy, rs = load_modules()
    config = collector.parse_orchestration_config(SECTION)
    scenarios: list[dict[str, Any]] = []

    def add(name: str, room: str = "living", changes: dict[str, Any] | None = None, *, pdc: dict[str, Any] | None = None, pdc_state: str = "idle", cycle: Any = None, controller: dict[str, Any] | None = None, unit: str = "°C") -> None:
        states = base_states()
        states.update(copy.deepcopy(changes or {}))
        collection = collector.collect_boiler_inputs(states, config, NOW, temperature_unit=unit)
        snapshot = policy.BoilerSnapshot(
            ok=bool(collection.raw["ok"]), master=collection.raw["master"], count=int(collection.raw["count"]),
            rooms=tuple(policy.QualifiedBoilerRoom(slug=r["slug"], target=r["target"], gap=r["gap"], pilot_ok=r["pilot_ok"]) for r in collection.raw["rooms"]),
            manual=collection.raw["manual"], pilot=collection.raw["pilot"], pilot_ready=collection.raw["pilot_ready"], consent=collection.raw["consent"],
        )
        decision = policy.evaluate_boiler(snapshot, cycle, NOW)
        view_controller = controller or {"mode": "observe", "owner": "observer", "state": decision.reason}
        diagnostics = collector.build_boiler_diagnostics(collection, decision, mode=view_controller["mode"], owner=view_controller["owner"], generation=5, cycle_origin="new_observation")
        if statuses is not None:
            room_binding = next(item for item in config.rooms if item.room_key == room)
            view = None if pdc is None else rs.PdcView(pdc_state, pdc)
            statuses[name] = rs.evaluate_room_status(room_binding, states, collection, decision, view, rs.ControllerView(**(controller or {"mode": "observe", "owner": "observer", "state": decision.reason})), NOW, active_count_entity_id=config.active_count_entity_id)
        scenarios.append(
            {
                "name": name,
                "room_key": room,
                "climate_entity_id": f"climate.vt_{room}",
                "states": states,
                "boiler": {"state": decision.reason, "attributes": diagnostics},
                "pdc": None if pdc is None else {"state": pdc_state, "attributes": pdc},
                "controller": controller or {"mode": "observe", "owner": "observer", "state": decision.reason},
                "temperature_unit": unit,
            }
        )

    SESSION_ON = {"input_boolean.bedroom_pdc": {"state": "on"}}
    solo = {"climate.vt_bedroom": vtherm(["climate.v_bed"], state="off", demand=False, hvac_action="idle"), COUNT: {"state": "2", "attributes": {"active_device_ids": ["climate.v_liv_1", "climate.v_liv_2"]}}}
    cool = policy.BoilerCycle(mode="off", off_since=NOW - 100.0)
    old = policy.BoilerCycle(mode="off", off_since=NOW - 4000.0, normal_since=NOW - 90.0)
    single = policy.BoilerCycle(mode="off", off_since=NOW - 4000.0, single=policy.BoilerSingleWait("living", 20.0, NOW - 300.0))
    L = lambda **kw: {"climate.vt_living": vtherm(["climate.v_liv_1", "climate.v_liv_2"], **kw)}  # noqa: E731

    add("baseline waiting two rooms")
    add("baseline waiting cooldown", cycle=cool)
    add("baseline waiting stability", cycle=old)
    add("single room waiting", changes=solo, cycle=single)
    add("single room waiting no anchor", changes=solo)
    add("consent missing", changes={"input_boolean.consent_a": {"state": "off"}}, cycle=old)
    add("manual request", changes={"binary_sensor.manual_request": {"state": "on"}})
    add("radiator heating boiler on", changes={MASTER: {"state": "heat"}})
    add("radiator heating boiler unavailable", changes={MASTER: {"state": "unavailable"}})
    add("trv heating nonparticipant", changes=L(configuration={"is_used_by_central_boiler": False}))
    add("trv heating nonparticipant boiler on", changes={**L(configuration={"is_used_by_central_boiler": False}), MASTER: {"state": "heat"}})
    add("at target", changes=L(current=20.0, demand=None, hvac_action="idle"))
    add("above target", changes=L(current=21.0, demand=False, hvac_action="idle"))
    add("small gap standby", changes=L(current=19.8, demand=False, hvac_action="idle"))
    add("regulating standby", changes=L(current=18.0, demand=False, hvac_action="idle"))
    add("continuing demand", changes=L(current=19.8, hvac_action="idle"))
    add("continuing demand heating", changes=L(current=19.8))
    add("demand without attribute heating", changes=L(demand=None))
    add("demand without attribute idle", changes=L(demand=None, hvac_action="idle"))
    add("demand without attribute idle below", changes=L(demand=None, hvac_action="idle", current=17.0))
    add("window open", changes={"binary_sensor.window_default": {"state": "on"}, **L(window_manager={"window_sensor_entity_id": "binary_sensor.window_default", "window_state": "on", "is_window_bypass": False})})
    add("window open pending", changes={"binary_sensor.window_default": {"state": "on"}})
    add("window unknown", changes={"binary_sensor.window_default": {"state": "unavailable"}})
    add("window bypass", changes={"binary_sensor.window_default": {"state": "on"}, **L(window_manager={"window_sensor_entity_id": "binary_sensor.window_default", "window_state": "off", "is_window_bypass": True})})
    add("window suspended off", changes=L(state="off", demand=False, hvac_action="off", specific_states={"is_initialized": True, "last_temperature_datetime": _iso(NOW - 60), "hvac_off_reason": "hvac_off_window_detection", "inhibition": {"inhibited": False}}))
    add("safety", changes=L(safety_manager={"safety_state": "on"}))
    add("power", changes=L(power_manager={"overpowering_state": "on"}))
    add("central mode off", changes={CENTRAL: {"state": "Off"}})
    add("central mode unknown", changes={CENTRAL: {"state": "unknown"}})
    add("central off reason", changes=L(state="off", demand=False, hvac_action="off", specific_states={"is_initialized": True, "last_temperature_datetime": _iso(NOW - 60), "hvac_off_reason": "hvac_off_central_mode", "inhibition": {"inhibited": False}}))
    add("auto pause", changes=L(state="off", demand=False, hvac_action="off", specific_states={"is_initialized": True, "last_temperature_datetime": _iso(NOW - 60), "hvac_off_reason": "hvac_off_auto_start_stop", "inhibition": {"inhibited": False}}))
    add("room off", changes=L(state="off", demand=False, hvac_action="off"))
    add("room off no demand attribute", changes=L(state="off", demand=None, hvac_action="off"))
    add("other mode", changes=L(state="cool", demand=False, hvac_action="idle"))
    add("unavailable", changes={"climate.vt_living": {"state": "unavailable", "attributes": {}}})
    add("not initialized", changes=L(is_ready=False))
    add("not specific initialized", changes=L(specific_states={"is_initialized": False, "last_temperature_datetime": _iso(NOW - 60), "inhibition": {"inhibited": False}}))
    add("inhibited", changes=L(specific_states={"is_initialized": True, "last_temperature_datetime": _iso(NOW - 60), "inhibition": {"inhibited": True}}))
    add("temperature stale", changes=L(age=4000.0))
    add("temperature future", changes=L(age=-30.0))
    add("temperature missing", changes=L(current=None))
    add("temperature datetime missing", changes=L(specific_states={"is_initialized": True, "inhibition": {"inhibited": False}}))
    add("no active valve", changes={"climate.v_liv_1": {"state": "heat", "attributes": {"hvac_action": "idle"}}, "climate.v_liv_2": {"state": "heat", "attributes": {"hvac_action": "idle"}}})
    add("valve unreadable", changes={"climate.v_liv_1": {"state": "unavailable", "attributes": {}}, "climate.v_liv_2": {"state": "unavailable", "attributes": {}}})
    add("fahrenheit", unit="°F")
    add("count invalid", changes={COUNT: {"state": "9", "attributes": {"active_device_ids": ["climate.v_liv_1"]}}})
    add("observe controller")
    add("control owner", controller={"mode": "control", "owner": "controller", "state": "heat"})
    add("control external owner", controller={"mode": "control", "owner": "observer", "state": "off"})

    ready = [pdc_condition("automatic", actual="on", expected="on"), pdc_condition("override", actual="off", expected="off"), pdc_condition("central_mode", actual="Auto", expected="Auto|Cool only"), pdc_condition("split_free", actual="off", expected="off")]
    blocked = lambda cid, code: [*(c for c in ready if c["id"] != cid), pdc_condition(cid, "fail", code)]  # noqa: E731
    add("pdc idle ready", "bedroom", pdc=pdc_attributes(conditions=ready, diagnostic="ready|1100|120"), pdc_state="ready")
    add("pdc observe measured heating", "bedroom", pdc=pdc_attributes(mode="observe", split_mode="heat", split_action="heating", conditions=ready, diagnostic="power_high|1100|120"))
    add("pdc observe blocked power", "bedroom", pdc=pdc_attributes(mode="observe", conditions=blocked("power_capacity", "power_high"), diagnostic="power_high|1100|120"))
    add("pdc transition waiting heat", "bedroom", changes=SESSION_ON, pdc=pdc_attributes(managed=True, phase="waiting_heat", split_mode="heat", conditions=ready), pdc_state="start")
    add("pdc transition waiting radiator", "bedroom", changes=SESSION_ON, pdc=pdc_attributes(managed=True, phase="waiting_radiator", conditions=ready), pdc_state="start")
    add("pdc transition waiting target", "bedroom", changes=SESSION_ON, pdc=pdc_attributes(managed=True, phase="waiting_target", conditions=ready), pdc_state="start")
    add("pdc returning", "bedroom", changes=SESSION_ON, pdc=pdc_attributes(managed=True, phase="returning", conditions=ready), pdc_state="stop")
    add("pdc transition window", "bedroom", changes={**SESSION_ON, "binary_sensor.window_default": {"state": "on"}, "climate.vt_bedroom": vtherm(["climate.v_bed"], window_manager={"window_sensor_entity_id": "binary_sensor.window_default", "window_state": "on", "is_window_bypass": False})}, pdc=pdc_attributes(managed=True, phase="waiting_heat", conditions=ready), pdc_state="start")
    add("pdc running heating", "bedroom", changes={"input_boolean.bedroom_pdc": {"state": "on"}, "climate.bedroom_split": {"state": "heat", "attributes": {"hvac_action": "heating"}}}, pdc=pdc_attributes(managed=True, phase="running", split_mode="heat", split_action="heating", conditions=ready), pdc_state="run")
    add("pdc running selected", "bedroom", changes={"input_boolean.bedroom_pdc": {"state": "on"}, "climate.bedroom_split": {"state": "heat", "attributes": {"hvac_action": "idle"}}}, pdc=pdc_attributes(managed=True, phase="running", split_mode="heat", split_action="idle", conditions=ready), pdc_state="run")
    add("pdc running split not heat", "bedroom", changes={"input_boolean.bedroom_pdc": {"state": "on"}}, pdc=pdc_attributes(managed=True, phase="running", split_mode="off", split_action="idle", conditions=ready), pdc_state="run")
    add("pdc failure active", "bedroom", changes=SESSION_ON, pdc=pdc_attributes(managed=True, phase="return_unconfirmed", failure="return_unconfirmed", conditions=ready), pdc_state="stop")
    add("pdc failure resolved", "bedroom", pdc=pdc_attributes(phase="idle", failure="split_unconfirmed", started_at=NOW - 500.0, conditions=ready))
    for cid, code in (("window_closed", "window"), ("temperature", "temperature_missing"), ("power_capacity", "power_missing"), ("other_pdc", "other_pdc"), ("split_free", "split_busy"), ("cooldown", "cooldown"), ("schedule", "schedule_change"), ("preset", "preset"), ("automatic", "automatic_off"), ("override", "manual_override")):
        add(f"pdc blocked {code}", "bedroom", pdc=pdc_attributes(conditions=blocked(cid, code), diagnostic=f"{code}|1100|120"))
    add("pdc blocked with boiler heating", "bedroom", changes={MASTER: {"state": "heat"}}, pdc=pdc_attributes(conditions=blocked("power_capacity", "power_high"), diagnostic="power_high|1100|120"))
    add("pdc data missing", "bedroom", pdc=pdc_attributes(conditions=blocked("temperature", "data_missing"), diagnostic="data_missing|1100|120"))
    add("pdc unavailable", "bedroom")
    add("pdc session on radiator excluded", "bedroom", changes={"input_boolean.bedroom_pdc": {"state": "on"}}, pdc=pdc_attributes(conditions=ready))
    add("pdc split not off radiator excluded", "bedroom", changes={"climate.bedroom_split": {"state": "heat", "attributes": {"hvac_action": "idle"}}}, pdc=pdc_attributes(conditions=ready))

    return {
        "now": NOW,
        "config_section": SECTION,
        "boiler_entity_id": BOILER_SENSOR,
        "pdc_entity_id": PDC_SENSOR,
        "master_entity_id": MASTER,
        "central_mode_entity_id": CENTRAL,
        "scenarios": scenarios,
    }


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(build_scenarios(), indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(out, len(json.loads(out.read_text(encoding="utf-8"))["scenarios"]), "scenarios")
