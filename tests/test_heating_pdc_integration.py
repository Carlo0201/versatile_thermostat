"""Pure checks of the PDC runtime and explicit opt-in in the shared files.

Home Assistant is neither imported nor executed: the shared modules are read as text/AST and the translations as JSON.
The behavior of the manager against a fake Home Assistant is checked by ``test_heating_pdc_fake_ha.py``; the real
Home Assistant tests are in ``test_heating_pdc_manager.py`` (written, not executed). Run with only pytest installed::

    python -m pytest -q --noconftest --import-mode=importlib tests/test_heating_pdc_integration.py
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import re
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_DIR = Path(os.environ["PDC_COMPONENT_DIR"]) if os.environ.get("PDC_COMPONENT_DIR") else REPO_ROOT / "custom_components" / "versatile_thermostat"
PKG = "_vtherm_pdc_pure_pkg_under_test"


def load(name: str) -> types.ModuleType:
    if PKG not in sys.modules:
        package = types.ModuleType(PKG)
        package.__path__ = [str(COMPONENT_DIR)]
        sys.modules[PKG] = package
    return importlib.import_module(f"{PKG}.{name}")


c = load("heating_pdc_collector")


def text(name: str) -> str:
    return (COMPONENT_DIR / name).read_text(encoding="utf-8")


def constants(name: str) -> dict[str, object]:
    """The simple constants of a module, read from its AST (no import of Home Assistant)."""
    out: dict[str, object] = {}
    for node in ast.parse(text(name)).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                pass
    return out


def translations(language: str) -> dict:
    return json.loads((COMPONENT_DIR / "translations" / f"{language}.json").read_text(encoding="utf-8"))


# --- explicit control -----------------------------------------------------------------------------


def test_the_release_offers_pdc_control_only_through_the_gated_options_flow():
    const = constants("const.py")
    assert const["HO_PDC_CONTROL_ENABLED"] is True
    assert const["HO_PDC_SELECTABLE_MODES"] == ["disabled", "observe"]
    assert "control" not in const["HO_PDC_SELECTABLE_MODES"]
    schema = text("config_schema.py")
    block = schema[schema.index("STEP_HEATING_PDC_SCHEMA") : schema.index("STEP_HEATING_PDC_ROOM_SCHEMA")]
    assert "build_step_heating_pdc_schema(HO_PDC_SELECTABLE_MODES)" in block
    assert "control" not in block.lower()
    flow = text("config_flow.py")
    step = flow[flow.index("async def async_step_heating_pdc(") : flow.index("async def async_step_heating_pdc_rooms(")]
    # The mode list is computed at runtime: control is appended only when the instance gate allows it.
    assert "[*HO_PDC_SELECTABLE_MODES, HO_MODE_CONTROL] if self._pdc_allow_control else list(HO_PDC_SELECTABLE_MODES)" in step
    assert "if mode not in allowed_modes" in step and "pdc_control_not_available" in step
    # The confirmation step is gated by the same flag and stores the section-level confirmation.
    confirm = flow[flow.index("async def async_step_heating_pdc_control_confirm(") : flow.index("def _pdc_control_placeholders")]
    assert "if not self._pdc_allow_control:" in confirm
    assert "section[CONF_HO_PDC_CONTROL_CONFIRMED] = True" in confirm
    # The base flow keeps the gate closed; only the options flow reads the release flag.
    assert flow.count("_pdc_allow_control = False") == 1
    assert "_pdc_allow_control = HO_PDC_CONTROL_ENABLED" in flow
    # The gate is also re-checked on save: no control is persisted without it or without confirmation.
    finalize = flow[flow.index("Never persist a ``control`` mode") :]
    assert "not self._pdc_allow_control or ho_section.get(CONF_HO_PDC_CONTROL_CONFIRMED) is not True" in finalize
    assert "CONF_HO_PDC_MODE: HO_MODE_OBSERVE, CONF_HO_PDC_CONTROL_CONFIRMED: False" in finalize


def test_the_manager_and_parser_require_explicit_opt_in_for_control():
    manager = text("feature_heating_pdc_manager.py")
    assert constants("feature_heating_pdc_manager.py")["PDC_CONTROL_AVAILABLE"] is True
    assert "allow_control=PDC_CONTROL_AVAILABLE" in manager and manager.count("allow_control=PDC_CONTROL_AVAILABLE") == 2
    assert "PDC_CONTROL_AVAILABLE" not in text("config_flow.py") + text("__init__.py") + text("vtherm_central_api.py") + text("sensor.py")
    with pytest.raises(c.PdcConfigError) as err:
        c.parse_pdc_config({"mode": "control", "power_entity_id": "sensor.p", "central_mode_entity_id": "select.c", "rooms": []})
    assert err.value.code == c.ERR_CONTROL_NOT_AVAILABLE
    # Even where the caller allows control, the parser refuses it without the explicit confirmation.
    with pytest.raises(c.PdcConfigError) as err:
        c.parse_pdc_config({"mode": "control", "power_entity_id": "sensor.p", "central_mode_entity_id": "select.c", "rooms": []}, allow_control=True)
    assert err.value.code == c.ERR_CONTROL_NOT_CONFIRMED and err.value.key == c.KEY_CONTROL_CONFIRMED
    # the second barrier of the ports: a manager that is not in control cannot reach a service, the inhibition or the store
    guard = manager[manager.index("class _HassPorts") : manager.index("class FeatureHeatingPdcManager")]
    for method in ("call_service", "set_inhibition", "unset_inhibition", "save"):
        body = guard[guard.index(f"async def {method}(") :]
        assert "self._guard()" in body[: body.index("\n\n")], method
    assert "manager.mode == MODE_CONTROL and PDC_CONTROL_AVAILABLE" in guard


def test_nothing_outside_the_pdc_modules_sends_a_command_or_an_inhibition_for_the_pdc():
    for name in ("config_flow.py", "config_schema.py", "sensor.py", "vtherm_central_api.py"):
        source = text(name)
        assert "async_suspend" not in source and "set_inhibition" not in source.replace("async_set_inhibition", "") or name == "config_flow.py"
    assert "async_call(" not in text("feature_heating_pdc_manager.py").replace("self._hass.services.async_call(domain, service, data, blocking=True, context=Context())", "")


# --- wiring ---------------------------------------------------------------------------------------


def test_the_setup_unload_and_removal_are_wired_with_the_lifecycle_of_the_central_entry():
    init = text("__init__.py")
    assert "_has_pdc_runtime(entry)" in init and "async_create_heating_pdc_manager" in init
    assert init.index("pdc = await api.async_create_heating_pdc_manager") < init.index("async_forward_entry_setups(entry, PLATFORMS)") < init.index("await pdc.async_start()")
    assert "entry.async_on_unload(_stop_pdc)" in init
    unload = init[init.index("async def async_unload_entry") : init.index("async def async_remove_entry")]
    assert "if had_pdc:\n        await api.async_remove_heating_pdc_manager(entry.entry_id)" in unload and unload.index("async_remove_heating_pdc_manager") < unload.index(
        "async_unload_platforms"
    )
    assert "async_create_heating_pdc_manager" in unload  # restarted when the unload fails
    removal = init[init.index("async def async_remove_entry") :]
    assert "HO_PDC_STORAGE_KEY_PREFIX" in removal
    # a failed setup does not leave a running manager behind
    setup = init[init.index("await hass.config_entries.async_forward_entry_setups") : init.index("async def update_listener")]
    assert "async_remove_heating_pdc_manager(entry.entry_id)" in setup and "raise" in setup
    api = text("vtherm_central_api.py")
    assert "async def async_create_heating_pdc_manager" in api and "async def async_remove_heating_pdc_manager" in api
    assert "await self.async_remove_heating_pdc_manager()" in api  # a previous manager is stopped first


def test_an_entry_without_the_new_keys_is_not_touched():
    init = text("__init__.py")
    helper = init[init.index("def _has_pdc_runtime") :]
    assert "CONF_HO_PDC_RUNTIME" in helper and "return isinstance(section, Mapping) and isinstance(section.get(CONF_HO_PDC_RUNTIME), Mapping)" in helper
    sensor = text("sensor.py")
    block = sensor[sensor.index("# PDC (heat pump) diagnostic, one per configured room") : sensor.index("else:\n        entities = [\n            LastTemperatureSensor")]
    assert "CONF_HO_PDC_RUNTIME" in block and "pdc_section" in block
    # the boiler part of the flow only carries the PDC keys over
    flow = text("config_flow.py")
    assert "for key in (CONF_HO_PDC_MODE, CONF_HO_PDC_RUNTIME, CONF_HO_PDC_CONTROL_CONFIRMED):" in flow
    assert constants("const.py")["HO_STORAGE_VERSION"] == 2 and constants("const.py")["HO_STORAGE_KEY_PREFIX"] == "versatile_thermostat.heating_orchestration"


def test_the_storage_key_has_a_single_value_and_does_not_collide_with_the_boiler_one():
    const, manager = constants("const.py"), constants("feature_heating_pdc_manager.py")
    assert const["HO_PDC_STORAGE_KEY_PREFIX"] == manager["PDC_STORAGE_KEY_PREFIX"] != const["HO_STORAGE_KEY_PREFIX"]
    assert not str(const["HO_PDC_STORAGE_KEY_PREFIX"]).startswith(str(const["HO_STORAGE_KEY_PREFIX"]) + ".")
    assert const["CONF_HO_PDC_MODE"] == "pdc_mode" and const["CONF_HO_PDC_RUNTIME"] == "pdc_runtime"
    assert manager["CONF_PDC_MODE"] == const["CONF_HO_PDC_MODE"] and manager["CONF_PDC_RUNTIME"] == const["CONF_HO_PDC_RUNTIME"]


def test_the_sensor_is_a_diagnostic_one_per_room_with_unrecorded_volatile_attributes():
    sensor = text("sensor.py")
    cls = sensor[sensor.index("class HeatingOrchestrationPdcSensor") : sensor.index("class HeatingOrchestrationBoilerSensor")]
    assert "_unrecorded_attributes = frozenset(" in cls and '"conditions"' in cls and '"evaluated_at"' in cls and '"generation"' in cls
    assert "EntityCategory.DIAGNOSTIC" in cls and 'f"{entry.entry_id}{HO_PDC_SENSOR_SUFFIX}{room_key}"' in cls
    assert constants("const.py")["HO_PDC_SENSOR_SUFFIX"] == "_heating_orchestration_pdc_"  # CONTRATTI section 4: <entry>_heating_orchestration_pdc_<room_key>
    assert "heating_orchestration_pdc" in cls and "manager.attach(self._room_key" in cls


# --- translations -----------------------------------------------------------------------------------

PDC_STEPS = ("heating_pdc", "heating_pdc_rooms", "heating_pdc_add_room", "heating_pdc_remove_room", "heating_pdc_control_confirm")


def _paths(node, prefix=""):
    if isinstance(node, dict):
        out = set()
        for key, value in node.items():
            out |= _paths(value, f"{prefix}/{key}")
        return out
    return {prefix}


def test_the_translations_of_the_pdc_exist_with_the_same_keys_in_italian_and_english():
    en, it = translations("en"), translations("it")
    for data in (en, it):
        for section in ("config", "options"):
            steps = data[section]["step"]
            assert "heating_pdc" in steps["menu"]["menu_options"]
            for step in PDC_STEPS:
                assert step in steps and steps[step]["title"]
            assert set(steps["heating_pdc_rooms"]["menu_options"]) == {"heating_pdc_add_room", "heating_pdc_remove_room", "heating_pdc_rooms_done"}
            assert set(steps["heating_pdc"]["data"]) == {"pdc_mode", "power_entity_id", "central_mode_entity_id"}
            assert set(steps["heating_pdc_control_confirm"]["data"]) == {"confirm_pdc_control"}
            assert "{rooms}" in steps["heating_pdc_control_confirm"]["description"]
        # The selector labels all three modes; runtime availability is tested in the flow.
        assert set(data["selector"]["heating_pdc_mode"]["options"]) == {"disabled", "observe", "control"}
        assert "{room}" in data["entity"]["sensor"]["heating_orchestration_pdc"]["name"]
    mine = lambda data: {p for p in _paths(data) if re.search(r"heating_pdc|pdc_|heating_orchestration_pdc", p)}  # noqa: E731
    assert mine(en) == mine(it) and mine(en)


def test_every_form_field_and_every_error_code_of_the_pdc_flow_is_translated():
    en = translations("en")
    schema_text = text("config_schema.py")
    room_fields = {
        c.KEY_ROOM_KEY,
        c.KEY_RADIATOR,
        c.KEY_VALVE,
        c.KEY_TEMPERATURE,
        c.KEY_WINDOW,
        c.KEY_SPLIT,
        c.KEY_PDC,
        c.KEY_AUTOMATIC,
        c.KEY_SCHEDULE_STOP,
        c.KEY_MANAGED,
        c.KEY_OVERRIDE,
        c.KEY_CYCLE,
        c.KEY_OVERRIDE_TIMER,
        c.KEY_SCHEDULE_SWITCHES,
        c.KEY_EXTERNAL_OWNERS,
        c.KEY_SETPOINT_MIN,
        c.KEY_MANUAL_BYPASS,
    }
    for section in ("config", "options"):
        assert room_fields <= set(en[section]["step"]["heating_pdc_add_room"]["data"])
        assert set(en[section]["step"]["heating_pdc_add_room"]["data"]) <= room_fields
        codes = {value for name, value in vars(c).items() if name.startswith("ERR_") and isinstance(value, str)}
        assert {f"pdc_{code}" for code in codes} <= set(en[section]["error"]), sorted({f"pdc_{code}" for code in codes} - set(en[section]["error"]))
    for key in room_fields:
        assert f"_pdc.KEY_" in schema_text  # every field is built from the collector keys
    assert len(room_fields) == 17


def test_the_translation_files_are_valid_and_keep_their_format():
    for language in ("en", "it"):
        raw = (COMPONENT_DIR / "translations" / f"{language}.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        assert json.dumps(data, indent=4, ensure_ascii=False) + "\n" == raw
        assert "\r" not in raw
