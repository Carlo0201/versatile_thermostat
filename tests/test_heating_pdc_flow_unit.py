"""Execute actual PDC flow methods with lightweight forms, without importing HA.

These cover flow decisions and parser wiring, not HA's UI/schema engine; the
matching HA integration tests remain necessary before the release gate opens.
"""

import ast
import copy
import logging
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

from .test_heating_pdc_runtime import c, run, section


class Marker(str):
    @property
    def schema(self):
        return str(self)


class Form:
    def __init__(self, fields):
        self.schema = {Marker(key): None for key in fields}

    def extend(self, fields):
        result = Form(self.schema)
        result.schema.update(fields)
        return result


def make_flow(mode="control", allow=True):
    path = Path(__file__).resolve().parents[1] / "custom_components/versatile_thermostat/config_flow.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    base = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "VersatileThermostatBaseConfigFlow")
    methods = [
        node
        for node in base.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (node.name.startswith("_pdc_") or node.name.startswith("async_step_heating_pdc") or node.name == "_ho_section")
    ]
    node = ast.ClassDef(name="Flow", bases=[], keywords=[], body=methods, decorator_list=[])
    constants = {
        "CONF_HEATING_ORCHESTRATION": "heating_orchestration",
        "CONF_HO_PDC_MODE": "pdc_mode",
        "CONF_HO_PDC_RUNTIME": "pdc_runtime",
        "CONF_HO_PDC_CONTROL_CONFIRMED": "pdc_control_confirmed",
        "CONF_HO_PDC_CONTROL_CONFIRM": "confirm_pdc_control",
        "CONF_HO_MODE": "mode",
        "CONF_HO_CONTROL_CONFIRMED": "control_confirmed",
        "CONF_HO_ROOMS": "rooms",
        "CONF_HO_ROOM_KEY": "room_key",
        "CONF_HO_CENTRAL_MODE": "central_mode_entity_id",
        "CONF_HO_PDC_ROOMS_TO_REMOVE": "pdc_rooms_to_remove",
        "HO_MODE_CONTROL": "control",
        "HO_MODE_OBSERVE": "observe",
        "HO_MODE_DISABLED": "disabled",
        "PDC_KEY_EXTERNAL_OWNERS": "external_owner_entity_ids",
        "HO_PDC_SELECTABLE_MODES": ["disabled", "observe"],
    }
    data = section("control", rooms=("carlo",))
    namespace = {
        **constants,
        "copy": copy,
        "Mapping": Mapping,
        "_LOGGER": logging.getLogger(__name__),
        "parse_pdc_config": c.parse_pdc_config,
        "PdcConfigError": c.PdcConfigError,
        "STEP_HEATING_PDC_ROOM_SCHEMA": Form(data["rooms"][0]),
        "STEP_HEATING_PDC_CONTROL_CONFIRM_SCHEMA": Form(["confirm_pdc_control"]),
        "vol": SimpleNamespace(Required=Marker),
        "add_suggested_values_to_schema": lambda schema, values: schema,
        "build_step_heating_pdc_schema": lambda modes: Form(["pdc_mode", "power_entity_id", "central_mode_entity_id"]),
    }
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    flow = namespace["Flow"]()
    flow._infos = {"heating_orchestration": {"pdc_mode": mode, "pdc_runtime": {key: value for key, value in data.items() if key not in ("mode", "control_confirmed")}}}
    flow._pdc_allow_control = allow
    flow._placeholders = {"name": "central"}
    flow.hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity_id: SimpleNamespace(state="off")))
    flow.async_show_form = lambda **kwargs: {"type": "form", **kwargs}
    flow.async_show_menu = lambda **kwargs: {"type": "menu", **kwargs}

    async def menu():
        return {"type": "menu", "step_id": "menu"}

    flow.async_step_menu = menu
    return flow


def test_control_requires_a_literal_confirmation_after_final_room_configuration():
    async def scenario():
        flow = make_flow()
        form = await flow.async_step_heating_pdc_rooms_done()
        assert form["step_id"] == "heating_pdc_control_confirm"
        refused = await flow.async_step_heating_pdc_control_confirm({"confirm_pdc_control": False})
        assert refused["errors"] == {"confirm_pdc_control": "pdc_control_not_confirmed"}
        assert "pdc_control_confirmed" not in flow._ho_section()
        accepted = await flow.async_step_heating_pdc_control_confirm({"confirm_pdc_control": True})
        assert accepted["step_id"] == "menu"
        assert flow._ho_section()["pdc_control_confirmed"] is True
        # Every subsequent PDC edit invalidates that confirmation, preserving the boiler.
        flow._infos["heating_orchestration"]["mode"] = "observe"
        flow._pdc_store("control", flow._pdc_runtime())
        assert "pdc_control_confirmed" not in flow._ho_section()
        assert flow._ho_section()["mode"] == "observe"

    run(scenario())


def test_omitting_owners_is_not_an_explicit_empty_control_declaration():
    async def scenario():
        flow = make_flow()
        room = flow._pdc_runtime()["rooms"][0]
        flow._infos["heating_orchestration"]["pdc_runtime"]["rooms"] = []
        room.pop("external_owner_entity_ids")
        missing = await flow.async_step_heating_pdc_add_room(room)
        assert missing["errors"] == {"external_owner_entity_ids": "pdc_external_owners_required"}
        assert flow._pdc_runtime()["rooms"] == []
        accepted = await flow.async_step_heating_pdc_add_room({**room, "external_owner_entity_ids": []})
        assert accepted["step_id"] == "heating_pdc_rooms"
        assert flow._pdc_runtime()["rooms"][0]["external_owner_entity_ids"] == []

    run(scenario())


def test_observe_never_writes_an_empty_owner_list_that_control_would_read_as_declared():
    async def scenario():
        flow = make_flow(mode="observe")
        room = flow._pdc_runtime()["rooms"][0]
        flow._infos["heating_orchestration"]["pdc_runtime"]["rooms"] = []
        room.pop("external_owner_entity_ids")
        accepted = await flow.async_step_heating_pdc_add_room(dict(room))
        assert accepted["step_id"] == "heating_pdc_rooms"
        stored = flow._pdc_runtime()["rooms"][0]
        assert "external_owner_entity_ids" not in stored  # nobody declared anything
        # the same stored room is refused when moving to control: the declaration is still missing
        errors = flow._pdc_check(flow._pdc_runtime(), set(), mode="control", confirmed=True)
        assert errors, "observe -> control must require an explicit owner declaration"
        # an owner actually selected in observe is kept
        flow._infos["heating_orchestration"]["pdc_runtime"]["rooms"] = []
        await flow.async_step_heating_pdc_add_room({**room, "external_owner_entity_ids": ["automation.legacy"]})
        assert flow._pdc_runtime()["rooms"][0]["external_owner_entity_ids"] == ["automation.legacy"]

    run(scenario())


def test_release_gate_refuses_control_but_preserves_observe():
    async def scenario():
        flow = make_flow(mode="observe", allow=False)
        refused = await flow.async_step_heating_pdc({"pdc_mode": "control", "power_entity_id": "sensor.power", "central_mode_entity_id": "select.central_mode"})
        assert refused["errors"] == {"pdc_mode": "pdc_control_not_available"}
        assert flow._ho_section()["pdc_mode"] == "observe"
        accepted = await flow.async_step_heating_pdc({"pdc_mode": "observe", "power_entity_id": "sensor.power", "central_mode_entity_id": "select.central_mode"})
        assert accepted["step_id"] == "heating_pdc_rooms"

    run(scenario())
