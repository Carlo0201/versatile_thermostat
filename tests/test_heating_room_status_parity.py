"""Parity between the Oikos card (reference run) and ``heating_room_status``.

``tests/fixtures/room_status/reference_card.json`` is produced by running the CURRENT card code on
``scenarios.json`` (see ``tests/tools/export_card_reference.mjs``); this test needs neither Node nor the
SDK. Differences are allowed only when listed in ``parity_differences.json`` (documented in the outcome
file); any new or vanished difference fails. To list the differences::

    PARITY_UPDATE=1 python -m pytest -q --noconftest --import-mode=importlib -p no:cacheprovider tests/test_heating_room_status_parity.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "room_status"
TOOLS = REPO_ROOT / "tests" / "tools" / "room_status_scenarios.py"

_spec = importlib.util.spec_from_file_location("_room_status_scenarios_under_test", TOOLS)
scenarios_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = scenarios_module
_spec.loader.exec_module(scenarios_module)

STATUSES: dict = {}
BUILT = scenarios_module.build_scenarios(STATUSES)
INPUT = json.loads((FIXTURES / "scenarios.json").read_text(encoding="utf-8"))
REFERENCE = {item["name"]: item for item in json.loads((FIXTURES / "reference_card.json").read_text(encoding="utf-8"))["scenarios"]}
DIFFS_PATH = FIXTURES / "parity_differences.json"

# Card row id -> ids of our conditions (a tuple is compared as the worst status of the group).
RADIATOR = {
    "roomEnabled": ("hvac_enabled",),
    "roomReady": ("room_ready",),
    "central": ("central_mode",),
    "window": ("window",),
    "temperature": ("temperature",),
    "roomDemand": ("room_demand",),
    "radiatorReleased": ("inhibition", "pdc_released"),
    "valveActive": ("valve_active",),
}
BOILER = {
    "boilerParticipation": ("boiler_participation",),
    "boilerRoomState": ("room_available", "hvac_enabled"),
    "boilerRoomReady": ("room_ready", "initialized"),
    "boilerWindow": ("window",),
    "boilerInhibition": ("inhibition",),
    "safety": ("safety",),
    "overpowering": ("overpowering",),
    "boilerRoomDemand": ("room_demand",),
    "boilerTemperature": ("temperature",),
    "boilerPumpReleased": ("pdc_released",),
    "radiatorSplitOff": ("pdc_released",),
    "boilerValve": ("valve_active",),
    "eligibleRadiator": ("eligible_radiator",),
    "nativeController": ("controller",),
    "validInputs": ("inputs",),
    "boilerMaster": ("master",),
    "boilerValveCount": ("valve_count",),
    "boilerThermalGap": ("thermal_gap",),
    "boilerPilot": ("pilot",),
    "boilerSingleRoomPilot": ("single_room_pilot",),
    "boilerManual": ("manual_request",),
    "boilerRoute": ("path",),
    "boilerHold": ("stability",),
    "boilerCooldown": ("cooldown",),
    "boilerConsent": ("consent",),
}
PUMP = {
    "pumpAutomatic": "automatic",
    "pumpOverride": "override",
    "central": "central_mode",
    "pumpProgram": "program",
    "pumpPreset": "preset",
    "pumpWindow": "window_closed",
    "pumpWindowGrace": "window_running",
    "pumpTemperature": "temperature",
    "pumpGap": "thermal_gap",
    "power": "power_capacity",
    "pumpPowerStop": "power_stop",
    "otherPump": "other_pdc",
    "splitOff": "split_free",
    "pumpSplitHeat": "split_heat",
    "pumpSchedule": "schedule",
    "pumpCooldown": "cooldown",
    "pumpHold": "stability",
    "pumpThermalStop": "thermal_stop",
    "pumpMinimumOn": "min_on",
}
SEVERITY = ("fail", "wait", "unknown", "observe", "pass", "na")


def snake(value):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower() if isinstance(value, str) else value


def worst(statuses):
    present = [s for s in statuses if s is not None]
    for level in SEVERITY:
        if level in present:
            return level
    return None


def compare(name):
    ours = STATUSES[name]
    ref = REFERENCE[name]
    # The condition id is unique only inside its stage (``central_mode``, ``temperature``, ``cooldown``... exist in two).
    by_id = {(item.stage, item.id): item.status for item in ours.conditions}
    assert len(by_id) == len(ours.conditions), f"{name}: duplicate condition within a stage"
    RADIATOR_STAGE = {
        "hvac_enabled": "ready",
        "room_ready": "ready",
        "central_mode": "ready",
        "window": "ready",
        "temperature": "ready",
        "room_demand": "demand",
        "inhibition": "ready",
        "pdc_released": "ready",
        "valve_active": "demand",
        "room_available": "ready",
        "initialized": "ready",
        "safety": "ready",
        "overpowering": "ready",
    }
    diffs: list[str] = []

    def lookup(i, stage=None):
        return by_id.get((stage or RADIATOR_STAGE.get(i, "boiler"), i))

    def row_diff(kind, card_id, card_status, ids, stage=None):
        mine = worst([lookup(i, stage) for i in ids])
        if mine is None:
            return
        if card_status != mine:
            diffs.append(f"{kind}:{card_id}: card={card_status} integrazione={mine}")

    merged = {"radiatorReleased", "boilerPumpReleased", "radiatorSplitOff", "boilerInhibition"}
    card_rows = [("radiatore", item) for item in ref["checklist"]["radiator"]] + [("caldaia", item) for item in ref["checklist"]["boiler"]]
    released = [item["status"] for _, item in card_rows if item["id"] in merged]
    if released:
        mine = worst([lookup("inhibition"), lookup("pdc_released")])
        card_worst = worst(released)
        if mine is not None and card_worst != mine:
            diffs.append(f"rilascio radiatore (inibizione e PDC): card={card_worst} integrazione={mine}")
    for kind, item in card_rows:
        if item["id"] in merged:
            continue
        table = RADIATOR if kind == "radiatore" else BOILER
        if item["id"] in table:
            row_diff(kind, item["id"], item["status"], table[item["id"]])
    for item in ref["checklist"]["pump"] or []:
        if item["id"] in PUMP:
            row_diff("pompa", item["id"], item["status"], (PUMP[item["id"]],), "pump")

    explain = ref["explain"]
    if snake(explain["state"]) != ours.heat_state:
        diffs.append(f"stato: card={snake(explain['state'])} integrazione={ours.heat_state}")
    if snake(explain["reason"]) != ours.reason:
        diffs.append(f"motivo: card={snake(explain['reason'])} integrazione={ours.reason}")
    # ``pump_native_*`` are card-only presentation markers (observe/blocked banners), not reasons.
    details = sorted(set(snake(code) for code in explain["detailReasons"] if not snake(code).startswith("pump_native")))
    if details != sorted(ours.detail_reasons):
        diffs.append(f"dettagli: card={details} integrazione={sorted(ours.detail_reasons)}")
    if (explain["deadline"] or None) != (ours.deadline or None):
        diffs.append(f"scadenza: card={explain['deadline']} integrazione={ours.deadline}")

    card_stages = {stage["key"]: stage["status"] for stage in ref["groups"]["stages"]}
    our_stages = {stage.key: stage.status for stage in ours.stages}
    for key in ("ready", "demand", "boiler", "pump"):
        if key in card_stages and key in our_stages and card_stages[key] != our_stages[key]:
            diffs.append(f"fase {key}: card={card_stages[key]} integrazione={our_stages[key]}")
    card_blocker = ref["groups"]["blocker"]
    reverse = {}
    for table in (RADIATOR, BOILER):
        for card_id, ids in table.items():
            for i in ids:
                reverse.setdefault(card_id, set()).add(i)
    mine = None if ours.blocker is None else ours.blocker.id
    card_ids = None if card_blocker is None else reverse.get(card_blocker["id"], {card_blocker["id"]})
    if (card_ids is None) != (mine is None) or (card_ids is not None and mine not in card_ids):
        diffs.append(f"bloccante: card={None if card_blocker is None else card_blocker['id']} integrazione={mine}")
    return diffs


ALL = {name: compare(name) for name in STATUSES}


def test_the_scenario_inputs_are_current():
    assert json.loads(json.dumps(BUILT)) == INPUT


def test_every_scenario_has_a_card_reference_without_errors():
    assert set(REFERENCE) == {s["name"] for s in INPUT["scenarios"]}
    assert not [name for name, item in REFERENCE.items() if "error" in item]
    assert len(REFERENCE) >= 70


def test_the_reference_records_the_expected_card_files_and_hash_format():
    data = json.loads((FIXTURES / "reference_card.json").read_text(encoding="utf-8"))
    assert set(data["generated_from"]) >= {"heatingChecklist.js", "roomExplanation.js", "nativeBoilerControl.js"}
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in data["generated_from"].values())


@pytest.mark.skipif(not os.environ.get("PARITY_UPDATE"), reason="writes the differences file")
def test_update_the_known_differences():
    DIFFS_PATH.write_text(json.dumps({name: d for name, d in ALL.items() if d}, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


@pytest.mark.skipif(bool(os.environ.get("PARITY_UPDATE")), reason="updating")
@pytest.mark.parametrize("name", sorted(ALL))
def test_parity_with_the_card_except_documented_differences(name):
    known = json.loads(DIFFS_PATH.read_text(encoding="utf-8")) if DIFFS_PATH.exists() else {}
    assert ALL[name] == known.get(name, []), f"{name}\n  ora: {ALL[name]}\n  documentate: {known.get(name, [])}"
