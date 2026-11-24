"""Pure tests of the boiler orchestration collector (M2-A).

The collector and the M1 policy are loaded by file path under private module
names, so the component package (whose ``__init__`` imports Home Assistant) is
never executed and no package is replaced in ``sys.modules``. They run with only
pytest installed::

    python -m pytest -q --noconftest --import-mode=importlib tests/test_heating_boiler_collector.py

The 17 observation fixtures of the baseline are read only. The parity with the
real Home Assistant template engine is checked in
``tests/test_heating_orchestration_parity.py``.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_DIR = REPO_ROOT / "custom_components" / "versatile_thermostat"
BASELINE_DIR = REPO_ROOT / "plans" / "heating-orchestration-2026-11-24" / "baseline"


def _load(name: str, filename: str) -> ModuleType:
    path = COMPONENT_DIR / filename
    existing = sys.modules.get(name)
    if existing is not None and Path(getattr(existing, "__file__", "")).resolve() == path:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


collector = _load("_vtherm_pure_heating_boiler_collector_under_test", "heating_boiler_collector.py")
policy = _load("_vtherm_pure_heating_boiler_policy_for_collector", "heating_boiler_policy.py")

NOW = 1_800_000_000.0
FIXTURE_NOW = 2000.0  # the fixtures carry no clock: any value in [1980, 5580] gives the saved results
LOCAL_TZ = timezone(timedelta(hours=1))

COUNT = "sensor.valves_active"
CENTRAL = "select.central_mode"
MASTER = "climate.boiler_master"
MANUAL = "binary_sensor.manual_request"
PILOT = "input_boolean.pilot_filter"
PILOT_READY = "binary_sensor.pilot_ready"
CONSENT_A = "input_boolean.consent_a"
CONSENT_B = "binary_sensor.consent_b"
LEGACY = "input_text.legacy_cycle"

SECTION: dict[str, Any] = {
    "mode": "observe",
    "master_entity_id": MASTER,
    "central_mode_entity_id": CENTRAL,
    "active_count_entity_id": COUNT,
    "manual_request_entity_id": MANUAL,
    "pilot_filter_entity_id": PILOT,
    "pilot_ready_entity_id": PILOT_READY,
    "consent_entity_ids": [CONSENT_A, CONSENT_B],
    "legacy_cycle_entity_id": LEGACY,
    "rooms": [
        {
            "room_key": "bedroom",
            "climate_entity_id": "climate.vt_bedroom",
            "pilot_filter": True,
            "pdc": {"split_entity_id": "climate.bedroom_split", "session_entity_id": "input_boolean.bedroom_pdc"},
        },
        {"room_key": "living", "climate_entity_id": "climate.vt_living", "pilot_filter": False},
    ],
}


def _iso(ts: float, tz: timezone = timezone.utc) -> str:
    return datetime.fromtimestamp(ts, tz).isoformat()


def vtherm(
    valves: list[str], *, age: float = 60.0, current: Any = 17.0, target: Any = 19.0, state: str = "heat", demand: Any = True, now: float = NOW, **over: Any
) -> dict[str, Any]:
    """A VTherm climate state with attributes in the layout the collector reads."""
    attributes: dict[str, Any] = {
        "is_ready": True,
        "current_temperature": current,
        "temperature": target,
        "specific_states": {"is_initialized": True, "last_temperature_datetime": _iso(now - age), "inhibition": {"inhibited": False}},
        "configuration": {"is_used_by_central_boiler": True},
        "vtherm_over_climate": {"underlying_entities": valves, "avatto_room_demand": demand},
        "window_manager": {"window_sensor_entity_id": "binary_sensor.window_default", "window_state": "off", "is_window_bypass": False},
        "safety_manager": {"safety_state": "off"},
        "power_manager": {"overpowering_state": "off"},
    }
    for key, value in over.items():
        attributes[key] = value
    return {"state": state, "attributes": attributes}


def base_snapshot(now: float = NOW, **changes: Any) -> dict[str, dict[str, Any]]:
    """A healthy snapshot: bedroom (one valve, PDC off) and living room (two valves)."""
    snapshot: dict[str, dict[str, Any]] = {
        COUNT: {"state": "3", "attributes": {"active_device_ids": ["climate.v_bed", "climate.v_liv_1", "climate.v_liv_2"]}},
        CENTRAL: {"state": "Heat only"},
        MASTER: {"state": "off"},
        MANUAL: {"state": "off"},
        PILOT: {"state": "off"},
        PILOT_READY: {"state": "on"},
        CONSENT_A: {"state": "on"},
        CONSENT_B: {"state": "on"},
        "input_boolean.bedroom_pdc": {"state": "off"},
        "climate.bedroom_split": {"state": "off"},
        "binary_sensor.window_bed": {"state": "off"},
        "binary_sensor.window_default": {"state": "off"},
        "climate.v_bed": {"state": "heat", "attributes": {"hvac_action": "heating"}},
        "climate.v_liv_1": {"state": "heat", "attributes": {"hvac_action": "heating"}},
        "climate.v_liv_2": {"state": "auto", "attributes": {"hvac_action": "heating"}},
        "climate.vt_bedroom": vtherm(["climate.v_bed"], now=now, window_manager={"window_sensor_entity_id": "binary_sensor.window_bed", "window_state": "off"}),
        "climate.vt_living": vtherm(["climate.v_liv_1", "climate.v_liv_2"], now=now, current=18.0, target=20.0),
    }
    snapshot.update(changes)
    return snapshot


@pytest.fixture(name="config")
def config_fixture():
    return collector.parse_orchestration_config(SECTION)


def collect(snapshot: dict, config, now: float = NOW, **kwargs: Any):
    return collector.collect_boiler_inputs(snapshot, config, now, **kwargs)


def reasons(collection, room_key: str) -> tuple[str, ...]:
    for room in collection.excluded_rooms:
        if room.room_key == room_key:
            return room.reasons
    raise AssertionError(f"{room_key} is not excluded: {collection.rooms}")


# ---------------------------------------------------------------------------
# Baseline fixtures (immutable inputs, normative ``raw`` of the template)
# ---------------------------------------------------------------------------

BASELINE_SECTION = {
    "mode": "observe",
    "master_entity_id": "climate.termostato_ingresso",
    "central_mode_entity_id": "select.central_mode",
    "active_count_entity_id": "sensor.nb_device_active_for_boiler",
    "manual_request_entity_id": "binary_sensor.caldaia_richiesta_manuale_oikos",
    "pilot_filter_entity_id": "input_boolean.caldaia_filtro_pilota_avatto",
    "pilot_ready_entity_id": "binary_sensor.caldaia_avvio_automatico_avatto",
    "consent_entity_ids": ["input_boolean.richiesta_caldaia_vtherm", "binary_sensor.central_boiler"],
    "rooms": [
        {
            "room_key": slug,
            "climate_entity_id": f"climate.trv_vtherm_{slug}",
            "pilot_filter": slug in ("camera_carlo", "bagno_blu"),
            **(
                {"pdc": {"split_entity_id": split, "session_entity_id": session}}
                if (split := {"camera_carlo": "climate.camera_carlo_condizionatore", "camera_diego_luisa": "climate.camera_diego_luisa_condizionatore_aria_stanza"}.get(slug))
                and (session := f"input_boolean.{slug}_pdc_in_gestione")
                else {}
            ),
        }
        for slug in ("camera_carlo", "ingresso", "cucina", "soggiorno", "camera_laura_livia", "bagno_verde", "bagno_blu", "camera_diego_luisa")
    ],
}

FIXTURES = json.loads((BASELINE_DIR / "boiler-observation-cases.json").read_text(encoding="utf-8"))
SAVED = {entry["name"]: entry["result"] for entry in FIXTURES["results"]}


@pytest.mark.parametrize("fixture", FIXTURES["fixtures"], ids=lambda fixture: fixture["name"])
def test_observation_fixtures_reproduce_the_saved_results(fixture):
    config = collector.parse_orchestration_config(BASELINE_SECTION)
    snapshot = copy.deepcopy(fixture["snapshot"])
    collection = collect(snapshot, config, FIXTURE_NOW)
    expected = SAVED[fixture["name"]]
    assert {key: collection.raw[key] for key in expected} == expected
    assert collection.raw["count"] == fixture["count"]
    assert collection.raw["ok"] == fixture["ok"]
    # The collector never mutates its input.
    assert snapshot == fixture["snapshot"]


def test_observation_fixtures_are_the_17_saved_ones():
    assert len(FIXTURES["fixtures"]) == 17
    assert sorted(fixture["name"] for fixture in FIXTURES["fixtures"]) == sorted(entry["name"] for entry in FIXTURES["results"])


# ---------------------------------------------------------------------------
# Healthy snapshot and valve counting
# ---------------------------------------------------------------------------


def test_healthy_snapshot(config):
    collection = collect(base_snapshot(), config)
    assert collection.ok and collection.count_raw == 3 and collection.count_raw_valid
    assert collection.valid_valve_count == 3
    assert [room.room_key for room in collection.rooms] == ["bedroom", "living"]
    living = collection.rooms[1]
    assert living.valves == ("climate.v_liv_1", "climate.v_liv_2")
    assert living.gap == 2.0 and living.target == 20.0
    assert collection.excluded_rooms == ()
    assert collection.raw["master"] == "off" and collection.raw["consent"] is True and collection.raw["pilot_ready"] is True
    assert collection.inputs == {"central_mode": "Heat only", "manual": "off", "pilot": "off", "pilot_ready": True, "consent": True}


def test_two_valves_of_one_room_count_as_two_physical_valves(config):
    collection = collect(base_snapshot(), config)
    assert collection.raw["count"] == 3
    assert len(collection.raw["rooms"]) == 2


def test_valve_listed_by_two_rooms_is_counted_once(config):
    snapshot = base_snapshot(**{"climate.vt_living": vtherm(["climate.v_liv_1", "climate.v_bed"], current=18.0, target=20.0)})
    snapshot[COUNT] = {"state": "2", "attributes": {"active_device_ids": ["climate.v_bed", "climate.v_liv_1"]}}
    collection = collect(snapshot, config)
    assert collection.raw["count"] == 2 and len(collection.rooms) == 2


def test_gap_is_rounded_to_three_decimals(config):
    snapshot = base_snapshot(**{"climate.vt_living": vtherm(["climate.v_liv_1"], current=18.1234, target=20.0)})
    assert collect(snapshot, config).rooms[1].gap == 1.877
    snapshot = base_snapshot(**{"climate.vt_living": vtherm(["climate.v_liv_1"], current="18.4", target="20.1")})
    assert collect(snapshot, config).rooms[1].gap == round(20.1 - 18.4, 3)


@pytest.mark.parametrize("valve_state, action", [("off", "heating"), ("heat", "idle"), ("heat", None), ("unavailable", "heating")])
def test_valve_must_heat_in_heat_or_auto(config, valve_state, action):
    snapshot = base_snapshot(**{"climate.v_bed": {"state": valve_state, "attributes": {"hvac_action": action}}})
    collection = collect(snapshot, config)
    assert reasons(collection, "bedroom") == ("no_active_valve",)
    assert collection.raw["count"] == 2


def test_valve_missing_from_the_active_list_does_not_count(config):
    snapshot = base_snapshot()
    snapshot[COUNT] = {"state": "2", "attributes": {"active_device_ids": ["climate.v_liv_1", "climate.v_liv_2"]}}
    collection = collect(snapshot, config)
    assert reasons(collection, "bedroom") == ("no_active_valve",)
    assert [room.room_key for room in collection.rooms] == ["living"]


def test_underlying_entities_missing_or_malformed_means_no_valve(config):
    snapshot = base_snapshot(**{"climate.vt_living": vtherm([], current=18.0, target=20.0)})
    assert reasons(collect(snapshot, config), "living") == ("no_active_valve",)
    snapshot = base_snapshot()
    snapshot["climate.vt_living"]["attributes"]["vtherm_over_climate"]["underlying_entities"] = "climate.v_liv_1"
    assert reasons(collect(snapshot, config), "living") == ("no_active_valve",)
    snapshot["climate.vt_living"]["attributes"]["vtherm_over_climate"] = "garbage"
    assert reasons(collect(snapshot, config), "living") == ("no_active_valve",)


# ---------------------------------------------------------------------------
# Raw count validity (the baseline deduplicates ids)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state, ids, valid, raw",
    [
        ("3", ["a", "b", "c"], True, 3),
        ("3.0", ["a", "b", "c"], True, 3),
        ("2", ["a", "b", "b"], True, 2),  # repeated id: valid when the count matches the unique ids
        ("3", ["a", "b", "b"], False, 3),
        ("2", ["a", "B", "b"], True, 2),  # unique() is case insensitive
        ("0", [], True, 0),
        ("1", [], False, 1),
        ("-1", [], False, -1),
        ("1.5", ["a"], False, None),
        ("abc", ["a"], False, None),
        ("unknown", ["a"], False, None),
        ("unavailable", ["a"], False, None),
        ("inf", ["a"], False, None),
        ("nan", ["a"], False, None),
        ("1", "a", False, 1),
        ("1", None, False, 1),
        ("1", [["a"]], False, 1),  # unhashable ids
    ],
)
def test_raw_count_validity(config, state, ids, valid, raw):
    snapshot = base_snapshot()
    snapshot[COUNT] = {"state": state, "attributes": {"active_device_ids": ids}}
    collection = collect(snapshot, config)
    assert collection.count_raw_valid is valid
    assert collection.count_raw == raw
    assert collection.ok is valid
    if not valid:
        # Invalid central data: no room is evaluated, the structural zero is flagged by ok=false.
        assert collection.raw["rooms"] == [] and collection.raw["count"] == 0
        for room in collection.excluded_rooms:
            assert "count_invalid" in room.reasons


def test_missing_count_entity_or_attribute(config):
    snapshot = base_snapshot()
    del snapshot[COUNT]
    collection = collect(snapshot, config)
    assert not collection.ok and collection.count_raw is None
    snapshot = base_snapshot()
    snapshot[COUNT] = {"state": "1"}
    collection = collect(snapshot, config)
    assert not collection.count_raw_valid and collection.count_raw == 1


# ---------------------------------------------------------------------------
# Central inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "central, ok", [("Auto", True), ("Heat only", True), ("Stopped", False), ("Cool only", False), ("heat only", False), ("unknown", False), ("unavailable", False)]
)
def test_central_mode_allowing_heat(config, central, ok):
    snapshot = base_snapshot(**{CENTRAL: {"state": central}})
    assert collect(snapshot, config).ok is ok


def test_missing_central_mode_is_not_ok(config):
    snapshot = base_snapshot()
    del snapshot[CENTRAL]
    collection = collect(snapshot, config)
    assert not collection.ok and collection.inputs["central_mode"] is None


@pytest.mark.parametrize("state", ["off", "heat", "unknown", "unavailable"])
def test_master_state_is_reported_as_is(config, state):
    snapshot = base_snapshot(**{MASTER: {"state": state}})
    assert collect(snapshot, config).raw["master"] == state


def test_missing_master_is_unknown_not_off(config):
    snapshot = base_snapshot()
    del snapshot[MASTER]
    assert collect(snapshot, config).raw["master"] == "unknown"


def test_consent_needs_every_native_consent_on(config):
    assert collect(base_snapshot(), config).raw["consent"] is True
    assert collect(base_snapshot(**{CONSENT_B: {"state": "off"}}), config).raw["consent"] is False
    assert collect(base_snapshot(**{CONSENT_A: {"state": "unavailable"}}), config).raw["consent"] is False
    snapshot = base_snapshot()
    del snapshot[CONSENT_A]
    assert collect(snapshot, config).raw["consent"] is False


def test_pilot_and_manual_defaults(config):
    snapshot = base_snapshot(**{PILOT: {"state": "on"}, MANUAL: {"state": "on"}})
    collection = collect(snapshot, config)
    assert collection.raw["pilot"] == "on" and collection.raw["manual"] == "on"
    snapshot = base_snapshot()
    del snapshot[PILOT], snapshot[MANUAL], snapshot[PILOT_READY]
    collection = collect(snapshot, config)
    assert collection.raw["pilot"] == "unknown" and collection.raw["manual"] == "unknown" and collection.raw["pilot_ready"] is False


def test_optional_bindings_absent_use_neutral_values():
    section = {key: value for key, value in SECTION.items() if key not in ("manual_request_entity_id", "pilot_filter_entity_id", "pilot_ready_entity_id", "legacy_cycle_entity_id")}
    config = collector.parse_orchestration_config(section)
    collection = collect(base_snapshot(), config)
    assert collection.raw["manual"] == "off" and collection.raw["pilot"] == "off" and collection.raw["pilot_ready"] is False


# ---------------------------------------------------------------------------
# Room qualification
# ---------------------------------------------------------------------------


def test_every_exclusion_code_can_be_collected_at_once(config):
    """Simultaneous conditions: all the codes are collected, the qualification stays the baseline's."""
    attributes = vtherm(["climate.v_bed"], age=7200, current=None, target=None, state="off", demand=False)["attributes"]
    attributes["is_ready"] = False
    attributes["specific_states"]["is_initialized"] = False
    attributes["specific_states"]["inhibition"] = {"inhibited": True}
    attributes["configuration"] = {"is_used_by_central_boiler": False}
    attributes["window_manager"] = {"window_sensor_entity_id": "binary_sensor.window_bed", "window_state": "on"}
    attributes["safety_manager"] = {"safety_state": "on"}
    attributes["power_manager"] = {"overpowering_state": "on"}
    snapshot = base_snapshot(
        **{"climate.vt_bedroom": {"state": "off", "attributes": attributes}, "input_boolean.bedroom_pdc": {"state": "on"}, "climate.bedroom_split": {"state": "heat"}}
    )
    snapshot["binary_sensor.window_bed"] = {"state": "on"}
    collection = collect(snapshot, config)
    assert reasons(collection, "bedroom") == (
        "hvac_not_heating",
        "not_ready",
        "not_initialized",
        "not_boiler_participant",
        "window_open",
        "inhibited",
        "safety",
        "overpowering",
        "avatto_no_demand",
        "temperature_missing",
        "temperature_stale",
        "pdc_session_active",
        "split_not_off",
    )
    assert [room.room_key for room in collection.rooms] == ["living"]


def test_codes_follow_the_documented_reporting_order():
    assert collector.EXCLUSION_CODES == (
        "entity_unavailable",
        "hvac_not_heating",
        "not_ready",
        "not_initialized",
        "not_boiler_participant",
        "window_open",
        "window_unknown",
        "inhibited",
        "safety",
        "overpowering",
        "avatto_no_demand",
        "temperature_missing",
        "temperature_stale",
        "temperature_future",
        "pdc_session_active",
        "pdc_session_unknown",
        "split_not_off",
        "split_unknown",
        "no_active_valve",
        "unit_unsupported",
        "count_invalid",
    )


@pytest.mark.parametrize("state", ["unavailable", "unknown"])
def test_unavailable_vtherm_is_excluded_not_off(config, state):
    collection = collect(base_snapshot(**{"climate.vt_living": {"state": state, "attributes": {}}}), config)
    assert reasons(collection, "living") == ("entity_unavailable",)


def test_missing_vtherm_is_excluded(config):
    snapshot = base_snapshot()
    del snapshot["climate.vt_living"]
    assert reasons(collect(snapshot, config), "living") == ("entity_unavailable",)


@pytest.mark.parametrize("hvac", ["off", "cool", "heat_cool", "dry"])
def test_hvac_mode_must_be_heat_or_auto(config, hvac):
    snapshot = base_snapshot(**{"climate.vt_living": vtherm(["climate.v_liv_1"], state=hvac)})
    assert reasons(collect(snapshot, config), "living") == ("hvac_not_heating",)


@pytest.mark.parametrize("flag", [None, False, "true", "on"])
def test_boolean_gates_need_a_true_value(config, flag):
    for key, path, code in (
        ("is_ready", None, "not_ready"),
        ("is_initialized", "specific_states", "not_initialized"),
        ("is_used_by_central_boiler", "configuration", "not_boiler_participant"),
    ):
        snapshot = base_snapshot()
        attributes = snapshot["climate.vt_living"]["attributes"]
        target = attributes if path is None else attributes[path]
        target[key] = flag
        assert reasons(collect(snapshot, config), "living") == (code,)


def test_jinja_equality_accepts_integer_one_as_true(config):
    snapshot = base_snapshot()
    snapshot["climate.vt_living"]["attributes"]["is_ready"] = 1
    assert [room.room_key for room in collect(snapshot, config).rooms] == ["bedroom", "living"]


@pytest.mark.parametrize(
    "age, code",
    [
        (3599, None),
        (3600, None),
        (3600.5, "temperature_stale"),
        (3601, "temperature_stale"),
        (0, None),
        (-0.001, "temperature_future"),
        (-1, "temperature_future"),
        (-86400, "temperature_future"),
    ],
)
def test_temperature_age_boundaries(config, age, code):
    snapshot = base_snapshot(**{"climate.vt_living": vtherm(["climate.v_liv_1"], age=age)})
    collection = collect(snapshot, config)
    if code is None:
        assert "living" in [room.room_key for room in collection.rooms]
    else:
        assert reasons(collection, "living") == (code,)


@pytest.mark.parametrize("stamp", [None, "", "garbage", 12])
def test_missing_or_unparsable_sample_time_counts_as_epoch_zero(config, stamp):
    snapshot = base_snapshot()
    snapshot["climate.vt_living"]["attributes"]["specific_states"]["last_temperature_datetime"] = stamp
    assert reasons(collect(snapshot, config), "living") == ("temperature_stale",)


def test_timestamps_with_offset_are_converted(config):
    zone = timezone(timedelta(hours=-5))
    snapshot = base_snapshot()
    snapshot["climate.vt_living"]["attributes"]["specific_states"]["last_temperature_datetime"] = _iso(NOW - 3000, zone)
    assert "living" in [room.room_key for room in collect(snapshot, config).rooms]
    snapshot["climate.vt_living"]["attributes"]["specific_states"]["last_temperature_datetime"] = _iso(NOW - 3700, zone)
    assert reasons(collect(snapshot, config), "living") == ("temperature_stale",)
    snapshot["climate.vt_living"]["attributes"]["specific_states"]["last_temperature_datetime"] = _iso(NOW + 5, zone)
    assert reasons(collect(snapshot, config), "living") == ("temperature_future",)


def test_naive_timestamp_is_read_in_the_local_zone(config):
    naive = datetime.fromtimestamp(NOW - 60, LOCAL_TZ).replace(tzinfo=None).isoformat()
    snapshot = base_snapshot()
    snapshot["climate.vt_living"]["attributes"]["specific_states"]["last_temperature_datetime"] = naive
    assert "living" in [room.room_key for room in collect(snapshot, config, local_tz=LOCAL_TZ).rooms]
    assert reasons(collect(snapshot, config, local_tz=timezone.utc), "living") == ("temperature_future",)


def test_datetime_object_is_accepted(config):
    snapshot = base_snapshot()
    snapshot["climate.vt_living"]["attributes"]["specific_states"]["last_temperature_datetime"] = datetime.fromtimestamp(NOW - 10, timezone.utc)
    assert "living" in [room.room_key for room in collect(snapshot, config).rooms]


@pytest.mark.parametrize("current, target", [(None, 20), (17, None), ("abc", 20), (17, "abc"), (float("nan"), 20), (17, float("inf")), ({}, 20)])
def test_temperatures_must_be_finite_numbers(config, current, target):
    snapshot = base_snapshot(**{"climate.vt_living": vtherm(["climate.v_liv_1"], current=current, target=target)})
    assert reasons(collect(snapshot, config), "living") == ("temperature_missing",)


def test_numeric_strings_are_accepted_as_in_the_template(config):
    snapshot = base_snapshot(**{"climate.vt_living": vtherm(["climate.v_liv_1"], current="17.5", target="20")})
    room = collect(snapshot, config).rooms[1]
    assert room.target == 20.0 and room.gap == 2.5


# --- window ---------------------------------------------------------------


def test_window_open_before_the_confirmed_suspension(config):
    """The sensor is already ``on`` while the VTherm manager still says ``off``."""
    snapshot = base_snapshot(**{"binary_sensor.window_bed": {"state": "on"}})
    assert reasons(collect(snapshot, config), "bedroom") == ("window_open",)
    # and the other way round: the manager is open while the sensor is back to off
    snapshot = base_snapshot()
    snapshot["climate.vt_bedroom"]["attributes"]["window_manager"]["window_state"] = "on"
    assert reasons(collect(snapshot, config), "bedroom") == ("window_open",)


@pytest.mark.parametrize("sensor_state, manager_state", [("unavailable", "off"), ("unknown", "off"), ("off", None), ("off", "unknown")])
def test_window_unknown(config, sensor_state, manager_state):
    snapshot = base_snapshot(**{"binary_sensor.window_bed": {"state": sensor_state}})
    snapshot["climate.vt_bedroom"]["attributes"]["window_manager"]["window_state"] = manager_state
    assert reasons(collect(snapshot, config), "bedroom") == ("window_unknown",)


def test_window_sensor_missing_from_the_registry_is_unknown(config):
    snapshot = base_snapshot()
    del snapshot["binary_sensor.window_bed"]
    assert reasons(collect(snapshot, config), "bedroom") == ("window_unknown",)


@pytest.mark.parametrize("bypass", [True, 1])
def test_explicit_bypass_ignores_the_window(config, bypass):
    snapshot = base_snapshot(**{"binary_sensor.window_bed": {"state": "on"}})
    snapshot["climate.vt_bedroom"]["attributes"]["window_manager"]["is_window_bypass"] = bypass
    assert "bedroom" in [room.room_key for room in collect(snapshot, config).rooms]


@pytest.mark.parametrize("bypass", [False, None, "true", 0])
def test_non_true_bypass_does_not_ignore_the_window(config, bypass):
    snapshot = base_snapshot(**{"binary_sensor.window_bed": {"state": "on"}})
    snapshot["climate.vt_bedroom"]["attributes"]["window_manager"]["is_window_bypass"] = bypass
    assert reasons(collect(snapshot, config), "bedroom") == ("window_open",)


def test_room_without_window_sensor_needs_a_bypass(config):
    snapshot = base_snapshot(**{"climate.vt_living": vtherm(["climate.v_liv_1"], window_manager={"window_state": "off"})})
    assert reasons(collect(snapshot, config), "living") == ("window_unknown",)


# --- protections ----------------------------------------------------------


@pytest.mark.parametrize(
    "key, value, code",
    [
        ("inhibition", {"inhibited": True}, "inhibited"),
        ("inhibition", {"inhibited": None}, "inhibited"),
        ("inhibition", {"inhibited": 1}, "inhibited"),
        ("inhibition", "garbage", "inhibited"),
    ],
)
def test_inhibition(config, key, value, code):
    snapshot = base_snapshot()
    snapshot["climate.vt_living"]["attributes"]["specific_states"][key] = value
    assert reasons(collect(snapshot, config), "living") == (code,)


@pytest.mark.parametrize("value", [None, {}, {"inhibited": False}, {"inhibited": 0}])
def test_missing_inhibition_means_not_inhibited(config, value):
    snapshot = base_snapshot()
    snapshot["climate.vt_living"]["attributes"]["specific_states"]["inhibition"] = value
    assert "living" in [room.room_key for room in collect(snapshot, config).rooms]


@pytest.mark.parametrize("value, code", [({"safety_state": "on"}, "safety"), ({"safety_state": "unknown"}, "safety"), ("garbage", "safety")])
def test_safety(config, value, code):
    snapshot = base_snapshot()
    snapshot["climate.vt_living"]["attributes"]["safety_manager"] = value
    assert reasons(collect(snapshot, config), "living") == (code,)


@pytest.mark.parametrize("value, code", [({"overpowering_state": "on"}, "overpowering"), ({"overpowering_state": "unavailable"}, "overpowering"), ("garbage", "overpowering")])
def test_overpowering(config, value, code):
    snapshot = base_snapshot()
    snapshot["climate.vt_living"]["attributes"]["power_manager"] = value
    assert reasons(collect(snapshot, config), "living") == (code,)


@pytest.mark.parametrize("value", [None, {}])
def test_missing_safety_and_power_means_off(config, value):
    snapshot = base_snapshot()
    snapshot["climate.vt_living"]["attributes"]["safety_manager"] = value
    snapshot["climate.vt_living"]["attributes"]["power_manager"] = value
    assert "living" in [room.room_key for room in collect(snapshot, config).rooms]


def test_avatto_demand_only_blocks_when_explicitly_false(config):
    snapshot = base_snapshot(**{"climate.vt_living": vtherm(["climate.v_liv_1"], demand=False)})
    assert reasons(collect(snapshot, config), "living") == ("avatto_no_demand",)
    for value in (None, "true", 1):
        snapshot = base_snapshot(**{"climate.vt_living": vtherm(["climate.v_liv_1"], demand=value)})
        assert "living" in [room.room_key for room in collect(snapshot, config).rooms], value
    snapshot = base_snapshot()
    del snapshot["climate.vt_living"]["attributes"]["vtherm_over_climate"]["avatto_room_demand"]
    assert "living" in [room.room_key for room in collect(snapshot, config).rooms]


# --- pilot ----------------------------------------------------------------


@pytest.mark.parametrize("demand, pilot_ok", [(True, True), (1, True), (None, False), ("true", False)])
def test_pilot_ok_only_for_rooms_subject_to_the_filter(config, demand, pilot_ok):
    snapshot = base_snapshot(
        **{"climate.vt_bedroom": vtherm(["climate.v_bed"], demand=demand, window_manager={"window_sensor_entity_id": "binary_sensor.window_bed", "window_state": "off"})}
    )
    snapshot["climate.vt_living"] = vtherm(["climate.v_liv_1", "climate.v_liv_2"], demand=demand)
    collection = collect(snapshot, config)
    by_key = {room.room_key: room for room in collection.rooms}
    assert by_key["bedroom"].pilot_ok is pilot_ok  # pilot_filter true
    assert by_key["living"].pilot_ok is True  # pilot_filter false: the veto never reaches the normal path


# --- PDC ------------------------------------------------------------------


@pytest.mark.parametrize(
    "session, split, codes",
    [
        ("off", "off", ()),
        ("on", "off", ("pdc_session_active",)),
        ("unknown", "off", ("pdc_session_unknown",)),
        ("unavailable", "off", ("pdc_session_unknown",)),
        ("off", "heat", ("split_not_off",)),
        ("off", "cool", ("split_not_off",)),
        ("off", "unavailable", ("split_unknown",)),
        ("off", "unknown", ("split_unknown",)),
        (None, "off", ("pdc_session_unknown",)),
        ("off", None, ("split_unknown",)),
        (None, None, ("pdc_session_unknown", "split_unknown")),
        ("on", "heat", ("pdc_session_active", "split_not_off")),
    ],
)
def test_pdc_confirmation_needs_session_and_split_off(config, session, split, codes):
    snapshot = base_snapshot()
    for entity_id, state in (("input_boolean.bedroom_pdc", session), ("climate.bedroom_split", split)):
        if state is None:
            del snapshot[entity_id]
        else:
            snapshot[entity_id] = {"state": state}
    collection = collect(snapshot, config)
    if codes:
        assert reasons(collection, "bedroom") == codes
    else:
        assert "bedroom" in [room.room_key for room in collection.rooms]


def test_pdc_not_available_does_not_become_off(config):
    snapshot = base_snapshot(**{"input_boolean.bedroom_pdc": {"state": "unavailable"}, "climate.bedroom_split": {"state": "unavailable"}})
    collection = collect(snapshot, config)
    assert reasons(collection, "bedroom") == ("pdc_session_unknown", "split_unknown")
    assert collection.raw["count"] == 2


def test_room_without_pdc_is_not_checked(config):
    snapshot = base_snapshot()
    del snapshot["input_boolean.bedroom_pdc"], snapshot["climate.bedroom_split"]
    assert "living" in [room.room_key for room in collect(snapshot, config).rooms]


# --- unit -----------------------------------------------------------------


@pytest.mark.parametrize("unit", ["°F", "K", None, ""])
def test_unsupported_temperature_unit_invalidates_the_inputs(config, unit):
    collection = collect(base_snapshot(), config, temperature_unit=unit)
    assert collection.ok is False and collection.temperature_unit_supported is False
    assert collection.raw["rooms"] == [] and collection.raw["count"] == 0
    assert all("unit_unsupported" in room.reasons for room in collection.excluded_rooms)
    assert len(collection.excluded_rooms) == 2


def test_celsius_is_the_default_unit(config):
    assert collect(base_snapshot(), config).temperature_unit_supported is True
    assert collect(base_snapshot(), config, temperature_unit="°C").ok is True


# --- clock ----------------------------------------------------------------


@pytest.mark.parametrize("now", [float("nan"), float("inf"), "1", None, True])
def test_now_must_be_a_finite_number(config, now):
    with pytest.raises(ValueError):
        collect(base_snapshot(), config, now)


def test_collection_is_bound_to_the_given_clock_and_pure(config):
    snapshot = base_snapshot()
    frozen = copy.deepcopy(snapshot)
    first = collect(snapshot, config, NOW)
    second = collect(snapshot, config, NOW)
    assert first == second and snapshot == frozen and first.now == NOW
    later = collect(snapshot, config, NOW + 3600)  # the 60 s old sample is now 3660 s old
    assert reasons(later, "living") == ("temperature_stale",)


def test_collector_accepts_state_like_objects(config):
    class FakeState:
        def __init__(self, state, attributes=None):
            self.state, self.attributes = state, attributes or {}

    snapshot = {key: FakeState(value["state"], value.get("attributes")) for key, value in base_snapshot().items()}
    assert collect(snapshot, config).raw == collect(base_snapshot(), config).raw


# --- watched entities -----------------------------------------------------


def test_watched_entities_include_configuration_windows_and_valves(config):
    watched = set(collect(base_snapshot(), config).watched_entity_ids)
    assert {
        MASTER,
        CENTRAL,
        COUNT,
        MANUAL,
        PILOT,
        PILOT_READY,
        CONSENT_A,
        CONSENT_B,
        "climate.vt_bedroom",
        "climate.vt_living",
        "climate.bedroom_split",
        "input_boolean.bedroom_pdc",
    } <= watched
    assert {"binary_sensor.window_bed", "climate.v_bed", "climate.v_liv_1", "climate.v_liv_2"} <= watched
    assert LEGACY not in watched  # the seed is read once, never listened to


# ---------------------------------------------------------------------------
# Configuration parsing
# ---------------------------------------------------------------------------


def section(**changes: Any) -> dict[str, Any]:
    result = copy.deepcopy(SECTION)
    result.update(changes)
    return result


def parse_error(value: Any) -> tuple[str, str | None]:
    with pytest.raises(collector.OrchestrationConfigError) as raised:
        collector.parse_orchestration_config(value)
    return raised.value.code, raised.value.key


def test_absent_section_is_disabled():
    config = collector.parse_orchestration_config(None)
    assert config.mode == "disabled" and config.rooms == () and config.configured_entity_ids() == ()


def test_disabled_section_is_accepted_whatever_the_rest_contains():
    assert collector.parse_orchestration_config({"mode": "disabled", "master_entity_id": "garbage"}).mode == "disabled"
    assert collector.parse_orchestration_config({}).mode == "disabled"


def test_valid_section_roundtrip(config):
    assert config.mode == "observe"
    assert [room.room_key for room in config.rooms] == ["bedroom", "living"]
    assert config.rooms[0].has_pdc and not config.rooms[1].has_pdc
    assert config.consent_entity_ids == (CONSENT_A, CONSENT_B)
    assert config.required_entity_ids() == (MASTER, CENTRAL, COUNT, CONSENT_A, CONSENT_B)


def test_control_is_not_selectable_and_unknown_modes_are_rejected():
    assert collector.SELECTABLE_MODES == ("disabled", "observe")
    assert parse_error(section(mode="control")) == ("control_not_available", "mode")
    assert parse_error(section(mode="Observe")) == ("unsupported_mode", "mode")
    assert parse_error(section(mode=3)) == ("unsupported_mode", "mode")


def test_section_must_be_a_mapping():
    for value in ("observe", 1, [], ["mode"]):
        assert parse_error(value) == ("invalid_section", None)


@pytest.mark.parametrize("key", ["master_entity_id", "central_mode_entity_id", "active_count_entity_id"])
def test_required_central_entities(key):
    changed = copy.deepcopy(SECTION)
    del changed[key]
    assert parse_error(changed) == ("required", key)
    changed[key] = ""
    assert parse_error(changed) == ("required", key)


def test_at_least_one_consent_is_required():
    for value in (None, [], ()):
        changed = copy.deepcopy(SECTION)
        changed["consent_entity_ids"] = value
        assert parse_error(changed) == ("required", "consent_entity_ids")


def test_single_consent_string_is_accepted():
    changed = copy.deepcopy(SECTION)
    changed["consent_entity_ids"] = CONSENT_A
    assert collector.parse_orchestration_config(changed).consent_entity_ids == (CONSENT_A,)


@pytest.mark.parametrize("value", ["climate", "Climate.Boiler", "climate.", ".boiler", "climate.a b", "climate.a.b", 5, ["climate.a"], "climate.a__b", "_climate.a"])
def test_invalid_entity_ids(value):
    changed = copy.deepcopy(SECTION)
    changed["master_entity_id"] = value
    assert parse_error(changed) == ("invalid_entity", "master_entity_id")


@pytest.mark.parametrize(
    "key, value",
    [
        ("master_entity_id", "switch.boiler"),
        ("central_mode_entity_id", "sensor.mode"),
        ("active_count_entity_id", "binary_sensor.count"),
        ("manual_request_entity_id", "climate.manual"),
        ("pilot_filter_entity_id", "sensor.pilot"),
        ("pilot_ready_entity_id", "light.pilot"),
        ("legacy_cycle_entity_id", "sensor.cycle"),
    ],
)
def test_invalid_domains(key, value):
    changed = copy.deepcopy(SECTION)
    changed[key] = value
    assert parse_error(changed) == ("invalid_domain", key)


def test_duplicate_bindings_are_rejected():
    changed = copy.deepcopy(SECTION)
    changed["manual_request_entity_id"] = PILOT
    assert parse_error(changed) == ("duplicate_binding", "pilot_filter_entity_id")
    changed = copy.deepcopy(SECTION)
    changed["consent_entity_ids"] = [CONSENT_A, CONSENT_A]
    assert parse_error(changed) == ("duplicate_binding", "consent_entity_ids")
    changed = copy.deepcopy(SECTION)
    changed["consent_entity_ids"] = [CONSENT_A, PILOT]
    assert parse_error(changed) == ("duplicate_binding", "consent_entity_ids")


def test_a_consent_entity_cannot_be_a_room_binding():
    changed = copy.deepcopy(SECTION)
    changed["rooms"][0]["pdc"]["session_entity_id"] = CONSENT_A
    assert parse_error(changed) == ("duplicate_binding", "session_entity_id")


def test_room_validation():
    def rooms(*items: Any) -> dict[str, Any]:
        changed = copy.deepcopy(SECTION)
        changed["rooms"] = list(items)
        return changed

    good = {"room_key": "a", "climate_entity_id": "climate.a"}
    assert collector.parse_orchestration_config(rooms(good)).rooms[0].pilot_filter is False
    assert parse_error(rooms(good, {"room_key": "a", "climate_entity_id": "climate.b"})) == ("duplicate_room", "room_key")
    assert parse_error(rooms(good, {"room_key": "b", "climate_entity_id": "climate.a"})) == ("duplicate_binding", "climate_entity_id")
    assert parse_error(rooms({"room_key": "Bad Key", "climate_entity_id": "climate.a"})) == ("invalid_room_key", "room_key")
    assert parse_error(rooms({"room_key": "", "climate_entity_id": "climate.a"})) == ("invalid_room_key", "room_key")
    assert parse_error(rooms({"room_key": "a" * 65, "climate_entity_id": "climate.a"})) == ("invalid_room_key", "room_key")
    assert parse_error(rooms({"room_key": "a"})) == ("required", "climate_entity_id")
    assert parse_error(rooms({"room_key": "a", "climate_entity_id": "switch.a"})) == ("invalid_domain", "climate_entity_id")
    assert parse_error(rooms({"room_key": "a", "climate_entity_id": "climate.a", "pilot_filter": "yes"})) == ("invalid_room", "pilot_filter")
    assert parse_error(rooms("garbage")) == ("invalid_room", "rooms")
    assert parse_error(rooms({"room_key": "a", "climate_entity_id": "climate.a", "pdc": {"split_entity_id": "climate.s"}})) == ("pdc_incomplete", "pdc")
    assert parse_error(rooms({"room_key": "a", "climate_entity_id": "climate.a", "pdc": {"session_entity_id": "input_boolean.s"}})) == ("pdc_incomplete", "pdc")
    assert parse_error(rooms({"room_key": "a", "climate_entity_id": "climate.a", "pdc": "garbage"})) == ("pdc_incomplete", "pdc")
    assert parse_error(rooms({"room_key": "a", "climate_entity_id": "climate.a", "pdc": {"split_entity_id": "climate.a", "session_entity_id": "input_boolean.s"}})) == (
        "duplicate_binding",
        "split_entity_id",
    )
    assert collector.parse_orchestration_config(rooms({"room_key": "a", "climate_entity_id": "climate.a", "pdc": {}})).rooms[0].has_pdc is False


def test_room_list_limits():
    changed = copy.deepcopy(SECTION)
    changed["rooms"] = [{"room_key": f"r{i}", "climate_entity_id": f"climate.r{i}"} for i in range(collector.MAX_ROOMS + 1)]
    assert parse_error(changed) == ("too_many_rooms", "rooms")
    changed = copy.deepcopy(SECTION)
    changed["rooms"] = "climate.a"
    assert parse_error(changed) == ("invalid_room", "rooms")
    changed = copy.deepcopy(SECTION)
    changed["consent_entity_ids"] = [f"input_boolean.c{i}" for i in range(collector.MAX_CONSENTS + 1)]
    assert parse_error(changed) == ("too_many_consents", "consent_entity_ids")


def test_eight_rooms_configuration_is_valid():
    config = collector.parse_orchestration_config(BASELINE_SECTION)
    assert len(config.rooms) == 8 and sum(room.has_pdc for room in config.rooms) == 2 and sum(room.pilot_filter for room in config.rooms) == 2


# ---------------------------------------------------------------------------
# Diagnostics payload (schema v1)
# ---------------------------------------------------------------------------

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


def policy_snapshot(collection) -> Any:
    raw = collection.raw
    return policy.BoilerSnapshot(
        ok=raw["ok"],
        master=raw["master"],
        count=raw["count"],
        rooms=tuple(policy.QualifiedBoilerRoom(slug=room["slug"], target=room["target"], gap=room["gap"], pilot_ok=room["pilot_ok"]) for room in raw["rooms"]),
        manual=raw["manual"],
        pilot=raw["pilot"],
        pilot_ready=raw["pilot_ready"],
        consent=raw["consent"],
    )


def test_diagnostics_schema_v1(config):
    collection = collect(base_snapshot(), config)
    decision = policy.evaluate_boiler(policy_snapshot(collection), None, NOW)
    attributes = collector.build_boiler_diagnostics(collection, decision, mode="observe", owner="observer", generation=7, cycle_origin="new_observation")
    assert set(attributes) == EXPECTED_KEYS
    assert attributes["schema_version"] == 1 and attributes["mode"] == "observe" and attributes["owner"] == "observer"
    assert attributes["generation"] == 7 and attributes["evaluated_at"] == NOW
    assert attributes["proposed_action"] == decision.action and attributes["reason"] == decision.reason
    assert attributes["commanded_mode"] is None and attributes["last_command_at"] is None and attributes["last_confirmation_at"] is None
    assert attributes["rooms"][0] == {"climate_entity_id": "climate.vt_bedroom", "room_key": "bedroom", "target": 19.0, "gap": 2.0, "pilot_ok": True, "valves": ["climate.v_bed"]}
    assert attributes["count_raw"] == 3 and attributes["count_raw_valid"] is True and attributes["valid_valve_count"] == 3 and attributes["inputs_ok"] is True
    assert attributes["cycle"].keys() == {"m", "off", "r", "s"}
    json.dumps(attributes, allow_nan=False)
    for condition in attributes["conditions"]:
        assert set(condition) == {"id", "status", "reason_code", "actual", "expected", "deadline", "unit"}
        assert condition["status"] in ("pass", "fail", "wait", "unknown", "na")
        assert condition["unit"] in ("°C", "s", "count", None)
    assert len(json.dumps(attributes)) < 16_000


def test_diagnostics_reject_a_decision_from_another_clock(config):
    collection = collect(base_snapshot(), config)
    decision = policy.evaluate_boiler(policy_snapshot(collection), None, NOW + 1)
    with pytest.raises(ValueError):
        collector.build_boiler_diagnostics(collection, decision, mode="observe", owner="observer", generation=1, cycle_origin="new_observation")


def test_excluded_rooms_are_reported_with_stable_codes(config):
    snapshot = base_snapshot(**{"climate.vt_living": vtherm(["climate.v_liv_1"], state="off")})
    collection = collect(snapshot, config)
    decision = policy.evaluate_boiler(policy_snapshot(collection), None, NOW)
    attributes = collector.build_boiler_diagnostics(collection, decision, mode="observe", owner="observer", generation=1, cycle_origin="new_observation")
    assert attributes["excluded_rooms"] == [{"climate_entity_id": "climate.vt_living", "room_key": "living", "reasons": ["hvac_not_heating"]}]
    assert [room["room_key"] for room in attributes["rooms"]] == ["bedroom"]


def test_empty_diagnostics_for_inactive_states():
    for mode, owner in (("disabled", None), ("observe", "observer")):
        attributes = collector.empty_diagnostics(mode, owner=owner, last_error="x" * 1000)
        assert set(attributes) == EXPECTED_KEYS
        assert attributes["reason"] is None and attributes["proposed_action"] is None and attributes["count_raw"] is None
        assert attributes["count_raw_valid"] is False and attributes["inputs_ok"] is False
        assert len(attributes["last_error"]) == collector.MAX_ERROR_LENGTH
        json.dumps(attributes, allow_nan=False)


def test_json_safe_removes_non_finite_numbers_and_unbounded_values():
    value = {"a": float("nan"), "b": [float("inf"), 1, (2, 3)], "c": object, "d": {"e": {"f": {"g": {"h": {"i": {"j": 1}}}}}}, "n": list(range(500))}
    safe = collector.json_safe(value)
    json.dumps(safe, allow_nan=False)
    assert safe["a"] is None and safe["b"] == [None, 1, [2, 3]] and len(safe["n"]) == collector.MAX_LIST_ITEMS
    assert math.isfinite(collector.json_safe(1.5))


def test_cycle_serialization_matches_the_legacy_keys():
    assert collector.cycle_to_dict(None) == {"m": None, "off": None, "r": None, "s": []}
    cycle = policy.parse_cycle_context({"m": "normal", "off": 10.0, "r": 20.0, "s": ["bedroom", 19.0, 30.0]})
    assert collector.cycle_to_dict(cycle) == {"m": "normal", "off": 10.0, "r": 20.0, "s": ["bedroom", 19.0, 30.0]}
    assert collector.cycle_to_dict(policy.parse_cycle_context(collector.cycle_to_dict(cycle))) == collector.cycle_to_dict(cycle)


def test_collector_source_has_no_home_assistant_import_or_global_clock():
    source = (COMPONENT_DIR / "heating_boiler_collector.py").read_text(encoding="utf-8")
    for forbidden in ("homeassistant", "import time", "datetime.now", "utcnow", "time.time", "call_service", "async_call"):
        assert forbidden not in source, forbidden
