"""Offline parity harness for the pure PDC policy (M3).

Renders the real Jinja templates and interprets the real native conditions of
the captured automations (``baseline/``, hash-verified, read-only) and of the
diagnostic template sensors (secondary source ``pdc/fonti``), then compares them
with ``heating_pdc_policy.py`` on boundary scenarios and on seeded random cases
for both rooms. No Home Assistant instance is used.

Home Assistant semantics emulated (checked against HA 2026.10.4 sources):
``states()``/``states[...]``, ``is_state``, ``state_attr``, ``has_value``,
``as_timestamp(value, default)``, ``now()``, filters ``float``, ``round`` (ceil),
``from_json``, ``to_json``; template condition ``render().strip().lower() == 'true'``;
state condition with ``for`` strict (``now - for > last_changed``); numeric_state
with ``below`` strict and unknown/unavailable -> False; and/or/not with HA
ConditionError propagation.

Usage (any Python with Jinja2)::

    python plans/heating-orchestration-2026-11-24/pdc/verify_m3.py [--random N] [--seed S]
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jinja2

PDC_DIR = Path(__file__).resolve().parent
PLAN_DIR = PDC_DIR.parent
REPO = PLAN_DIR.parents[1]
POLICY_PATH = Path(os.environ["PDC_POLICY_PATH"]) if os.environ.get("PDC_POLICY_PATH") else REPO / "custom_components" / "versatile_thermostat" / "heating_pdc_policy.py"
spec = importlib.util.spec_from_file_location("_m3_policy_under_verify", POLICY_PATH)
policy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = policy
spec.loader.exec_module(policy)

# --- sources ------------------------------------------------------------------------------------

INDEX = json.loads((PLAN_DIR / "baseline" / "index.json").read_text(encoding="utf-8-sig"))
for item in INDEX["files"]:
    digest = hashlib.sha256((PLAN_DIR / item["file"]).read_bytes()).hexdigest()
    assert digest == item["sha256"], item["file"]


def baseline(name: str) -> dict[str, Any]:
    return json.loads((PLAN_DIR / "baseline" / f"{name}.json").read_text(encoding="utf-8"))["config"]


DIAG_FILE = PDC_DIR / "fonti" / "pdc_diagnostica_2026-11-23.json"
DIAG = json.loads(DIAG_FILE.read_text(encoding="utf-8"))

ROOMS = {
    "camera_carlo": {
        "prefix": "camera_carlo",
        "split": "climate.camera_carlo_condizionatore",
        "window": "binary_sensor.finestra_interna_camera_carlo",
        "other_managed": "input_boolean.camera_diego_luisa_pdc_in_gestione",
        "other_split": "climate.camera_diego_luisa_condizionatore_aria_stanza",
        "config": policy.PdcPolicyConfig(setpoint_min_c=16.0, manual_preset_bypass=True),
    },
    "camera_diego_luisa": {
        "prefix": "camera_diego_luisa",
        "split": "climate.camera_diego_luisa_condizionatore_aria_stanza",
        "window": "binary_sensor.finestre_interne_camera_diego_luisa",
        "other_managed": "input_boolean.camera_carlo_pdc_in_gestione",
        "other_split": "climate.camera_carlo_condizionatore",
        "config": policy.PdcPolicyConfig(setpoint_min_c=18.0, manual_preset_bypass=False),
    },
}
for key, room in ROOMS.items():
    room["chooser"] = baseline(f"{key}_scelta_radiatore_o_pompa_di_calore")
    room["register"] = baseline(f"{key}_registro_ciclo_persistente_pdc")
    room["protection"] = baseline(f"{key}_protezioni_override_condizionatore")
    room["diag_template"] = DIAG["templates"][key]

# --- Home Assistant emulation -------------------------------------------------------------------


class ConditionError(Exception):
    """Equivalent of the HA ConditionError."""


class FakeState:
    def __init__(self, entity_id: str, state: str, attributes: dict[str, Any] | None, changed: float, reported: float | None = None) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes or {}
        self.last_changed = datetime.fromtimestamp(changed, timezone.utc)
        self.last_reported = datetime.fromtimestamp(changed if reported is None else reported, timezone.utc)


class World:
    def __init__(self, now: float) -> None:
        self.now = now
        self.items: dict[str, FakeState] = {}

    def set(self, entity_id: str, state: str, attributes: dict[str, Any] | None = None, changed: float | None = None, reported: float | None = None) -> None:
        changed = self.now - 100000.0 if changed is None else changed
        self.items[entity_id] = FakeState(entity_id, state, attributes, changed, reported)

    def get(self, entity_id: str) -> FakeState | None:
        return self.items.get(entity_id)


class StatesAccessor:
    def __init__(self, world: World) -> None:
        self.world = world

    def __call__(self, entity_id: str) -> str:
        st = self.world.get(entity_id)
        return "unknown" if st is None else st.state

    def __getitem__(self, entity_id: str) -> FakeState | None:
        return self.world.get(entity_id)


def _forgiving_float(value: Any, default: Any = None) -> Any:
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _forgiving_round(value: Any, precision: int = 0, method: str = "common", default: Any = None) -> Any:
    try:
        multiplier = float(10**precision)
        if method == "ceil":
            value = math.ceil(float(value) * multiplier) / multiplier
        elif method == "floor":
            value = math.floor(float(value) * multiplier) / multiplier
        else:
            value = round(float(value), precision)
        return int(value) if precision == 0 else value
    except (ValueError, TypeError):
        return default


def _reject_constant(value: str) -> Any:
    raise ValueError(value)  # orjson rejects NaN/Infinity


def _from_json(value: Any, default: Any = None) -> Any:
    try:
        return json.loads(value, parse_constant=_reject_constant)
    except (ValueError, TypeError):
        return default


def _to_json(value: Any, **_: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


ENV = jinja2.Environment()
ENV.filters.update({"float": _forgiving_float, "round": _forgiving_round, "from_json": _from_json, "to_json": _to_json})


_COMPILED: dict[str, Any] = {}
_EXPRESSIONS: dict[str, Any] = {}


def _template(source: str) -> Any:
    if source not in _COMPILED:
        _COMPILED[source] = ENV.from_string(source)
    return _COMPILED[source]


def context(world: World, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    def as_timestamp(value: Any, default: Any = None) -> Any:
        if isinstance(value, datetime):
            return value.timestamp()
        return default

    def is_state(entity_id: str, state: Any) -> bool:
        st = world.get(entity_id)
        return st is not None and (st.state == state or (isinstance(state, list) and st.state in state))

    def state_attr(entity_id: str, name: str) -> Any:
        st = world.get(entity_id)
        return None if st is None else st.attributes.get(name)

    def has_value(entity_id: str) -> bool:
        st = world.get(entity_id)
        return st is not None and st.state not in ("unknown", "unavailable")

    ctx = {
        "states": StatesAccessor(world),
        "is_state": is_state,
        "state_attr": state_attr,
        "has_value": has_value,
        "as_timestamp": as_timestamp,
        "now": lambda: datetime.fromtimestamp(world.now, timezone.utc),
    }
    ctx.update(extra or {})
    return ctx


def render(template: str, world: World, extra: dict[str, Any] | None = None) -> str:
    try:
        return _template(template).render(**context(world, extra)).strip()
    except Exception as err:  # noqa: BLE001 - HA wraps every render failure in TemplateError
        raise ConditionError(f"template error: {err}") from err


def expression(template: str, world: World, extra: dict[str, Any] | None = None) -> Any:
    """Native rendering of a single ``{{ expr }}`` variable."""
    inner = template.strip()
    assert inner.startswith("{{") and inner.endswith("}}"), template
    if inner not in _EXPRESSIONS:
        _EXPRESSIONS[inner] = ENV.compile_expression(inner[2:-2].strip())
    return _EXPRESSIONS[inner](**context(world, extra))


def check(cond: dict[str, Any], world: World, trigger_id: str | None = None) -> bool:
    kind = cond["condition"]
    if kind == "template":
        return render(cond["value_template"], world).lower() == "true"
    if kind == "state":
        st = world.get(cond["entity_id"])
        if st is None:
            raise ConditionError(f"unknown entity {cond['entity_id']}")
        wanted = cond["state"] if isinstance(cond["state"], list) else [cond["state"]]
        ok = st.state in wanted
        if not ok or "for" not in cond:
            return ok
        seconds = float(cond["for"]["seconds"])
        return world.now - seconds > st.last_changed.timestamp()
    if kind == "numeric_state":
        st = world.get(cond["entity_id"])
        if st is None:
            raise ConditionError("unknown entity")
        if st.state in ("unknown", "unavailable"):
            return False
        try:
            value = float(st.state)
        except ValueError as err:
            raise ConditionError("non numeric") from err
        if "below" in cond and value >= float(cond["below"]):
            return False
        if "above" in cond and value <= float(cond["above"]):
            return False
        return True
    if kind == "trigger":
        ids = cond["id"] if isinstance(cond["id"], list) else [cond["id"]]
        return trigger_id in ids
    subs = cond["conditions"]
    errors = []
    if kind == "and":
        for sub in subs:
            try:
                if not check(sub, world, trigger_id):
                    return False
            except ConditionError as err:
                errors.append(err)
        if errors:
            raise ConditionError("and")
        return True
    if kind == "or":
        for sub in subs:
            try:
                if check(sub, world, trigger_id):
                    return True
            except ConditionError as err:
                errors.append(err)
        if errors:
            raise ConditionError("or")
        return False
    if kind == "not":
        for sub in subs:
            try:
                if check(sub, world, trigger_id):
                    return False
            except ConditionError as err:
                errors.append(err)
        if errors:
            raise ConditionError("not")
        return True
    raise AssertionError(kind)


def test_conditions(conds: list[dict[str, Any]], world: World, trigger_id: str | None = None) -> bool | None:
    """Script ``_test_conditions``: False on first false, None on ConditionError."""
    try:
        for cond in conds:
            if check(cond, world, trigger_id) is False:
                return False
    except ConditionError:
        return None
    return True


# --- scenario model -----------------------------------------------------------------------------

NOMINAL = {
    "override": "off",
    "managed": "off",
    "managed_age": 5000.0,
    "automatic": "on",
    "central": "Auto",
    "radiator_mode": "heat",
    "requested": "heat",
    "preset": "comfort",
    "temperature": 18.5,
    "temp_age": 30.0,
    "target": 19.0,
    "window": "off",
    "window_age": 5000.0,
    "power": "400",
    "power_age": 5.0,
    "split": "off",
    "split_age": 5000.0,
    "pdc": "off",
    "pdc_setpoint": 20.0,
    "other_managed": "off",
    "other_split": "off",
    "schedule": "none",
    "cycle": {"m": "off", "on": 1795000000, "off": "AGE:3000", "target": 19.0},
    "cycle_state": None,
    "timer": "idle",
    "switches": ("on", "off"),
    "valve": "heat",
    "inhibited": None,
    "stage": "waiting_trv",
    "trigger": "start",
}
NOW_BASE = 1795500000.0


def resolve_cycle(cycle: Any, now: float) -> Any:
    if not isinstance(cycle, dict):
        return cycle
    out = {}
    for key, value in cycle.items():
        if isinstance(value, str) and value.startswith("AGE:"):
            value = int(now - float(value[4:]))
        out[key] = value
    return out


def build(room: dict[str, Any], case: dict[str, Any]) -> tuple[World, Any]:
    now = case["now"]
    w = World(now)
    p = room["prefix"]
    w.set(f"input_boolean.{p}_pdc_override", case["override"])
    w.set(f"input_boolean.{p}_pdc_in_gestione", case["managed"], changed=now - case["managed_age"])
    w.set(f"input_boolean.{p}_pdc_automatica", case["automatic"])
    w.set("select.central_mode", case["central"])
    attrs = {
        "requested_state": None if case["requested"] is None else {"hvac_mode": case["requested"]},
        "preset_mode": case["preset"],
        "temperature": case["target"],
        "specific_states": {"inhibition": {"inhibited": case["inhibited"]}},
    }
    w.set(f"climate.trv_vtherm_{p}", case["radiator_mode"], attrs)
    w.set(f"climate.trv_{p}", case["valve"])
    temp = "unknown" if case["temperature"] is None else repr(float(case["temperature"]))
    w.set(f"sensor.temperatura_{p}", temp, reported=now - case["temp_age"])
    w.set(room["window"], case["window"], changed=now - case["window_age"])
    w.set("sensor.energy_meter_power_a_casa", case["power"], reported=now - case["power_age"])
    w.set(room["split"], case["split"], changed=now - case["split_age"])
    w.set(f"climate.{p}_pompa_di_calore", case["pdc"], {"temperature": case["pdc_setpoint"]})
    w.set(room["other_managed"], case["other_managed"])
    w.set(room["other_split"], case["other_split"])
    w.set(f"timer.override_riscaldamento_{p}", case["timer"])
    w.set(f"switch.schedule_{p}_vtherm_feriale", case["switches"][0])
    w.set(f"switch.schedule_{p}_vtherm_weekend", case["switches"][1])
    schedule = case["schedule"]
    if isinstance(schedule, float):
        schedule = repr(now + schedule)
    w.set(f"sensor.{p}_pdc_prossimo_stop_programma", schedule)
    cycle_state = case["cycle_state"] if case["cycle_state"] is not None else json.dumps(resolve_cycle(case["cycle"], now))
    w.set(f"input_text.{p}_pdc_ciclo", cycle_state)
    w.set(f"input_text.{p}_pdc_transizione", f"{case['stage']}|x|1")
    diag = render(room["diag_template"], w)
    w.set(f"sensor.{p}_pdc_diagnostica", diag)

    decoded = _from_json(cycle_state, {})
    power = _forgiving_float(case["power"])
    snapshot = policy.PdcSnapshot(
        override=case["override"],
        managed=case["managed"],
        managed_changed_at=now - case["managed_age"],
        automatic=case["automatic"],
        central_mode=case["central"],
        radiator_mode=case["radiator_mode"],
        requested_hvac_mode=case["requested"],
        preset_mode=case["preset"],
        temperature=None if case["temperature"] is None else float(case["temperature"]),
        temperature_reported_at=now - case["temp_age"],
        target=case["target"],
        window=case["window"],
        window_changed_at=now - case["window_age"],
        power=power,
        power_reported_at=now - case["power_age"],
        split_mode=case["split"],
        split_changed_at=now - case["split_age"],
        pdc_mode=case["pdc"],
        pdc_setpoint=case["pdc_setpoint"],
        other_managed=case["other_managed"],
        other_split_mode=case["other_split"],
        schedule_stop=schedule,
        cycle=policy.parse_pdc_cycle(decoded),
        cycle_available=cycle_state not in ("unknown", "unavailable"),
        heating_override_timer=case["timer"],
        schedule_switches=tuple(case["switches"]),
        radiator_valve_mode=case["valve"],
        radiator_inhibited=case["inhibited"],
    )
    return w, snapshot, decoded, diag


def baseline_branch(room: dict[str, Any], w: World, trigger_id: str) -> str:
    cfg = room["chooser"]
    if test_conditions(cfg["conditions"], w, trigger_id) is not True:
        return "none"
    if test_conditions([cfg["actions"][1]], w, trigger_id) is not True:
        return "none"
    names = ["stop", "set_setpoint", "start"]
    for index, option in enumerate(cfg["actions"][2]["choose"]):
        if test_conditions(option["conditions"], w, trigger_id) is True:
            return names[index]
    return "none"


def compare(room_key: str, case: dict[str, Any], stats: dict[str, Any]) -> None:
    room = ROOMS[room_key]
    cfg = room["config"]
    now = case["now"]
    w, snap, decoded, diag = build(room, case)
    where = {"room": room_key, "case": case}

    # 1. legacy diagnostic string
    motor_diag = policy.diagnostic_state(policy.diagnostic_codes(snap, now, cfg), cfg)
    assert motor_diag == diag, {**where, "jinja": diag, "python": motor_diag}

    # 2. chooser branch (trigger 'start' = one-shot edge already fired, 'tick' = any other trigger)
    trigger_id = case["trigger"]
    ctx = policy.PdcStartContext(ready_since=now - 200.0, edge_consumed=False) if trigger_id == "start" else policy.PdcStartContext(ready_since=now - 200.0, edge_consumed=True)
    decision = policy.evaluate_pdc(snap, ctx, now, cfg)
    expected = baseline_branch(room, w, trigger_id)
    assert decision.action == expected, {**where, "jinja": expected, "python": decision.action, "reason": decision.reason, "diag": diag}
    stats["branches"][expected] = stats["branches"].get(expected, 0) + 1

    chooser = room["chooser"]["actions"][2]["choose"]
    # 3. journal reason of a stop (rendered whenever the motor would stop or the template can render)
    if snap.target is not None and snap.temperature is not None or decision.action == "stop":
        try:
            journal = render(chooser[0]["sequence"][0]["variables"]["journal_reason"], w)
        except ConditionError:
            journal = None
        if journal is not None:
            assert policy.stop_reason(snap, now, cfg) == journal, {**where, "jinja": journal, "python": policy.stop_reason(snap, now, cfg)}
            stats["journal"] += 1

    # 4. setpoint formula
    if snap.target is not None:
        sp = render(chooser[1]["sequence"][1]["data"]["temperature"], w)
        assert float(sp) == policy.split_setpoint(snap.target, cfg), {**where, "jinja": sp}
        stats["setpoint"] += 1

    start = chooser[2]["sequence"]
    # 5. wait template radiator stopped, recheck after radiator stop, recheck after setpoint, confirm running
    wait_ok = render(start[2]["wait_template"], w).lower() == "true"
    assert wait_ok == policy.radiator_stopped(snap), where
    pairs = (
        (start[3]["if"], policy.recheck_after_radiator_stop, "recheck_radiator"),
        (start[3]["then"][3]["if"], policy.recheck_after_setpoint, "recheck_setpoint"),
        (start[3]["then"][3]["then"][4]["if"], policy.keep_running_ok, "confirm_running"),
    )
    for conds, fn, name in pairs:
        got = test_conditions(conds, w)
        assert got is not None, {**where, "error": name}
        assert got == fn(snap, now, cfg), {**where, "check": name, "jinja": got}
        stats[name] += int(got)

    # 6. failure classification of the start fallback
    try:
        failure = render(start[4]["data"]["value"], w).split("|")[1]
    except ConditionError:
        failure = None
    assert policy.classify_start_failure(case["stage"], snap, cfg) == failure, {**where, "jinja": failure}

    # 7. override protections
    prot = test_conditions(room["protection"]["conditions"], w)
    assert prot is not None, where
    assert prot == policy.override_protection_stop(snap, now, cfg), {**where, "jinja": prot}
    stats["protection_stop"] += int(prot)

    # 8. persistent cycle register for the current split state
    reg = room["register"]["actions"]
    trig = {"platform": "state", "to_state": w.get(room["split"])}
    mode = expression(reg[0]["variables"]["mode"], w, {"trigger": trig})
    previous = expression(reg[0]["variables"]["previous"], w)
    extra = {"mode": mode, "previous": previous}
    write = render(reg[1]["value_template"], w, extra).lower() == "true"
    motor = policy.observe_split_mode(previous, mode if isinstance(mode, str) else None, snap.target, now)
    if write:
        value = json.loads(render(reg[2]["data"]["value"], w, extra))
        assert motor == value, {**where, "jinja": value, "python": motor}
        stats["register_writes"] += 1
    else:
        assert motor is None, {**where, "python": motor}


# --- scenarios ----------------------------------------------------------------------------------


def scenario(**changes: Any) -> dict[str, Any]:
    case = dict(NOMINAL)
    case["now"] = NOW_BASE
    case.update(changes)
    return case


RUN = {"managed": "on", "split": "heat", "pdc": "heat", "trigger": "tick", "cycle": {"m": "heat", "on": "AGE:700", "off": "AGE:5000", "target": 19.0}}


def run(**changes: Any) -> dict[str, Any]:
    return scenario(**{**RUN, **changes})


def boundary_scenarios() -> dict[str, dict[str, Any]]:
    s: dict[str, dict[str, Any]] = {}
    s["start_nominal"] = scenario()
    for gap in (0.4, 0.399999, 0.3999989, 0.399, 0.41):
        s[f"gap_{gap}"] = scenario(temperature=19.0 - gap)
    for power in ("1100", "1100.0", "1100.5", "1099.99", "0", "-0.1", "unknown", "unavailable"):
        s[f"start_power_{power}"] = scenario(power=power)
    for age in (45.0, 45.25, -5.0):
        s[f"start_power_age_{age}"] = scenario(power_age=age)
    for age in (3600.0, 3600.25, -60.0):
        s[f"start_temp_age_{age}"] = scenario(temp_age=age)
    for horizon in (600.0, 600.25, 599.75, -10.0):
        s[f"schedule_{horizon}"] = scenario(schedule=horizon)
    for state in ("invalid", "unknown", "0"):
        s[f"schedule_{state}"] = scenario(schedule=state)
    for off in (599.0, 600.0, 601.0):
        s[f"cooldown_{off}"] = scenario(cycle={"m": "off", "on": 1, "off": f"AGE:{off}", "target": 19.0})
    s["cooldown_future"] = scenario(cycle={"m": "off", "off": "AGE:-50"})
    s["cooldown_negative"] = scenario(cycle={"m": "off", "off": -5})
    s["cooldown_mode_heat"] = scenario(cycle={"m": "heat", "off": "AGE:3000"})
    s["cycle_unknown"] = scenario(cycle_state="unknown")
    s["cycle_list"] = scenario(cycle_state="[]")
    s["cycle_string_numbers"] = scenario(cycle={"m": "off", "off": "1795400000", "target": "19"})
    for age in (599.75, 600.0):
        s[f"raw_split_{age}"] = scenario(split_age=age)
        s[f"raw_managed_{age}"] = scenario(managed_age=age)
    s["override_on"] = scenario(override="on")
    s["override_unknown"] = scenario(override="unknown")
    s["other_managed"] = scenario(other_managed="on")
    s["other_split_heat"] = scenario(other_split="heat")
    s["other_split_unavailable"] = scenario(other_split="unavailable")
    s["window_open_start"] = scenario(window="on", window_age=5.0)
    s["window_unknown_start"] = scenario(window="unknown")
    s["preset_away"] = scenario(preset="away")
    s["preset_away_timer_active"] = scenario(preset="away", timer="active")
    s["preset_away_idle_switches_off"] = scenario(preset="away", timer="idle", switches=("off", "off"))
    s["preset_none_attr"] = scenario(preset=None)
    s["central_cool"] = scenario(central="Cool only")
    s["requested_none"] = scenario(requested=None)
    s["edge_consumed"] = scenario(trigger="tick")
    # running
    s["run_nominal"] = run()
    for power in ("2699.99", "2700", "2700.01", "-50", "unknown"):
        s[f"run_power_{power}"] = run(power=power)
    s["run_power_stale"] = run(power_age=45.25)
    s["run_power_future"] = run(power_age=-30.0)
    for age in (119.0, 120.0, 120.25, 121.0):
        s[f"run_window_{age}"] = run(window="on", window_age=age)
    s["run_window_unknown"] = run(window="unavailable", window_age=10.0)
    s["run_temp_future"] = run(temp_age=-30.0, temperature=19.6)
    s["run_temp_stale"] = run(temp_age=3600.25)
    for on_age in (599.0, 600.0):
        s[f"run_min_on_{on_age}"] = run(cycle={"m": "heat", "on": f"AGE:{on_age}", "target": 19.0}, temperature=19.2)
    s["run_ordinary_below"] = run(temperature=19.199)
    s["run_early_0.5"] = run(cycle={"m": "heat", "on": "AGE:100", "target": 19.0}, temperature=19.5)
    s["run_early_0.499"] = run(cycle={"m": "heat", "on": "AGE:100", "target": 19.0}, temperature=19.499)
    s["run_target_lowered"] = run(cycle={"m": "heat", "on": "AGE:100", "target": 20.0}, temperature=19.0)
    s["run_target_raised"] = run(cycle={"m": "heat", "on": "AGE:100", "target": 18.0}, temperature=18.6)
    s["run_setpoint_mismatch"] = run(pdc_setpoint=21.0)
    s["run_split_off"] = run(split="off")
    s["run_override_unknown"] = run(override="unknown", power="2900")
    s["low_target_setpoint_min"] = run(target=14.2, temperature=14.0, pdc_setpoint=16.0)
    s["high_target_setpoint_max"] = run(target=29.5, temperature=29.0, pdc_setpoint=30.0)
    # setpoint formula ceil(target + 1) clamped to the room limits (16/30 and 18/30), integers and fractions
    for target in (15.0, 16.0, 16.5, 17.0, 17.5, 17.99, 18.0, 19.01, 19.5, 20.3, 28.9, 29.0, 29.01, 30.0):
        s[f"setpoint_target_{target}"] = run(target=target, temperature=target - 0.1, pdc_setpoint=20.0)
    # gap bit-exactly equal to the threshold 0.4 - 1e-6 (target 0.5, 0.75): inclusive comparison
    s["gap_exact_threshold_0.5"] = scenario(target=0.5, temperature=0.10000099999999994, pdc_setpoint=16.0)
    s["gap_exact_threshold_0.75"] = scenario(target=0.75, temperature=0.35000099999999995, pdc_setpoint=16.0)
    # power high and window not closed at the same time: journal order (power first)
    s["stop_power_and_window_open"] = run(power="2800", window="on", window_age=500.0)
    s["stop_power_and_window_unknown"] = run(power="2800", window="unknown")
    # persistent cycle register: missing on/off, fractional now (the baseline truncates)
    s["register_heat_on_missing"] = run(cycle={"m": "heat", "off": "AGE:5000", "target": 19.0})
    s["register_off_off_missing"] = scenario(cycle={"m": "off", "on": 1795000000, "target": 19.0})
    s["register_fraction_truncated"] = run(now=NOW_BASE + 0.75, cycle={"m": "off", "on": 1795000000, "off": "AGE:3000", "target": 19.0})
    s["register_heat_confirmed_target_missing"] = run(target=None, cycle={"m": "off", "off": "AGE:3000"})  # override protections
    s["prot_power_2700"] = scenario(override="on", split="heat", power="2700")
    s["prot_power_2699"] = scenario(override="on", split="cool", power="2699.99")
    s["prot_window_120"] = scenario(override="on", split="heat", window="on", window_age=120.0)
    s["prot_window_121"] = scenario(override="on", split="heat", window="on", window_age=121.0)
    s["prot_window_unknown_29"] = scenario(override="on", split="heat", window="unknown", window_age=29.75)
    s["prot_window_unknown_30"] = scenario(override="on", split="heat", window="unknown", window_age=30.0)
    s["prot_central_stopped"] = scenario(override="on", split="dry", central="Stopped")
    # start sequence classification
    s["stage_trv_ok"] = scenario(stage="waiting_trv", valve="off", radiator_mode="off", inhibited=True)
    s["stage_trv_fail"] = scenario(stage="waiting_trv", valve="off", radiator_mode="off", inhibited=False)
    s["stage_target_fail"] = scenario(stage="waiting_target", pdc_setpoint=19.0)
    s["stage_target_missing"] = scenario(stage="waiting_target", target=None)
    s["stage_split_fail"] = scenario(stage="waiting_split", split="off")
    s["stage_split_ok"] = scenario(stage="waiting_split", split="heat")
    return s


def random_case(rng: random.Random) -> dict[str, Any]:
    case = scenario(**(RUN if rng.random() < 0.4 else {}))
    case["now"] = NOW_BASE + rng.choice([0.0, 0.25, 0.5])
    perturb = {
        "override": ["off", "on", "unknown"],
        "managed": ["off", "on", "unknown", "unavailable"],
        "managed_age": [100.0, 599.75, 600.0, 5000.0],
        "automatic": ["on", "off", "unknown"],
        "central": ["Auto", "Heat only", "Stopped", "Cool only", "unknown"],
        "radiator_mode": ["heat", "off", "unknown"],
        "requested": ["heat", "off", None],
        "preset": ["none", "eco", "comfort", "boost", "away", "frost", None],
        "temperature": [None, 17.0, 18.6, 18.600001, 18.599999, 19.0, 19.2, 19.199999, 19.5, 19.499999, 20.0],
        "temp_age": [0.0, 3600.0, 3600.25, -30.0, 30.0],
        "target": [None, 19.0, 18.0, 17.5, 20.3, 14.2, 29.5],
        "window": ["off", "on", "unknown", "unavailable"],
        "window_age": [0.0, 29.75, 30.0, 119.0, 120.0, 120.25, 5000.0],
        "power": ["unknown", "unavailable", "0", "400", "1100", "1100.25", "-5", "2699.99", "2700", "2700.01", "abc"],
        "power_age": [5.0, 45.0, 45.25, -10.0],
        "split": ["off", "heat", "cool", "unavailable"],
        "split_age": [10.0, 599.75, 600.0, 5000.0],
        "pdc": ["off", "heat", "unknown"],
        "pdc_setpoint": [None, 16.0, 18.0, 19.0, 20.0, 21.0, 30.0],
        "other_managed": ["off", "on", "unknown"],
        "other_split": ["off", "heat", "unavailable"],
        "schedule": ["none", "invalid", "unknown", "0", 600.0, 600.25, 599.75, -10.0, 3000.0],
        "timer": ["idle", "active", "paused", "unknown"],
        "switches": [("on", "off"), ("off", "off"), ("unavailable", "off")],
        "valve": ["heat", "off", "unknown"],
        "inhibited": [None, True, False],
        "stage": ["waiting_trv", "waiting_target", "waiting_split", "idle"],
        "trigger": ["start", "tick"],
    }
    keys = list(perturb)
    for key in rng.sample(keys, rng.randint(1, 6)):
        case[key] = rng.choice(perturb[key])
    if rng.random() < 0.3:
        case["cycle"] = rng.choice(
            [
                {
                    "m": "off",
                    "on": "AGE:4000",
                    "off": rng.choice(["AGE:599", "AGE:600", "AGE:601", "AGE:-20", None, "1795400000", "abc"]),
                    "target": rng.choice([19.0, 20.0, None, "19"]),
                },
                {"m": "heat", "on": rng.choice(["AGE:599", "AGE:600", "AGE:3000", None, "x"]), "off": "AGE:5000", "target": rng.choice([19.0, 20.0, 18.0, None])},
                {"m": rng.choice(["cool", None, 3]), "off": "AGE:3000"},
                {},
            ]
        )
    if rng.random() < 0.05:
        case["cycle_state"] = rng.choice(["unknown", "[]", '"x"', "{bad"])
    return case


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--random", type=int, default=6000)
    parser.add_argument("--seed", type=int, default=20261124)
    parser.add_argument("--no-report", action="store_true", help="do not rewrite M3-parita.json (mutation runs)")
    args = parser.parse_args()
    report: dict[str, Any] = {"policy_sha256": hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest(), "baseline_files_hash_verified": len(INDEX["files"])}
    report["diagnostic_source"] = {"file": DIAG_FILE.name, "sha256": hashlib.sha256(DIAG_FILE.read_bytes()).hexdigest(), "origin": DIAG["origin"]}
    for room_key in ROOMS:
        stats: dict[str, Any] = {
            "branches": {},
            "journal": 0,
            "setpoint": 0,
            "recheck_radiator": 0,
            "recheck_setpoint": 0,
            "confirm_running": 0,
            "protection_stop": 0,
            "register_writes": 0,
        }
        scenarios = boundary_scenarios()
        outcomes = {}
        for name, case in scenarios.items():
            compare(room_key, case, stats)
            w, snap, _, diag = build(ROOMS[room_key], case)
            outcomes[name] = {"branch": baseline_branch(ROOMS[room_key], w, case["trigger"]), "diag": diag}
        boundary_stats = json.loads(json.dumps(stats))
        rng = random.Random(args.seed + (0 if room_key == "camera_carlo" else 1))
        stats = {k: ({} if isinstance(v, dict) else 0) for k, v in stats.items()}
        for _ in range(args.random):
            compare(room_key, random_case(rng), stats)
        report[room_key] = {
            "boundary_cases": len(scenarios),
            "boundary_stats": boundary_stats,
            "boundary_outcomes": outcomes,
            "random_cases": args.random,
            "seed": args.seed + (0 if room_key == "camera_carlo" else 1),
            "random_stats": stats,
        }
    if not args.no_report:
        (PDC_DIR / "M3-parita.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {k: ({kk: vv for kk, vv in v.items() if kk != "boundary_outcomes"} if isinstance(v, dict) and "random_cases" in v else v) for k, v in report.items()},
            ensure_ascii=False,
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
