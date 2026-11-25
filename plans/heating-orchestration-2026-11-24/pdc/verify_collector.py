"""Offline parity of the PDC collector (W2) with the real Jinja of the baseline automations.

It reuses the Home Assistant emulation, the scenarios and the checks of ``verify_m3.py`` but
replaces the snapshot that harness builds from its own case values with the one the **collector**
builds by reading the emulated entity states (``collect_pdc_snapshot``). For every case:

1. the collector snapshot must be equal, field by field, to the harness snapshot (so the collector
   reads the same entities, with the same ``float(none)``/``last_reported``/``last_changed``
   semantics, as the templates);
2. all the M3 checks of ``verify_m3.compare`` run again on the collector snapshot (legacy diagnostic
   string, chooser branch, stop reason, setpoint, radiator-stopped wait, the three re-checks of the
   start sequence, failure classification, override protections, cycle register);
3. ``build_observation`` must agree with the Jinja conditions of the sequence: ``conditions_ok`` for
   each phase equals the baseline ``if`` of that step, ``radiator_stopped`` the ``wait_template``, and
   ``target_confirmed`` is the negation of the "setpoint differs" template.

No Home Assistant instance is used. Usage (any Python with Jinja2)::

    python plans/heating-orchestration-2026-11-24/pdc/verify_collector.py [--random N] [--seed S]
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import itertools
import json
import os
import random
import sys
import types
from pathlib import Path
from typing import Any

PDC_DIR = Path(__file__).resolve().parent
PLAN_DIR = PDC_DIR.parent
REPO = PLAN_DIR.parents[1]
COMPONENT_DIR = Path(os.environ["PDC_COMPONENT_DIR"]) if os.environ.get("PDC_COMPONENT_DIR") else REPO / "custom_components" / "versatile_thermostat"

PKG = "_vtherm_pdc_collector_parity_pkg"
package = types.ModuleType(PKG)
package.__path__ = [str(COMPONENT_DIR)]
sys.modules[PKG] = package
pkg_policy = importlib.import_module(f"{PKG}.heating_pdc_policy")
pkg_collector = importlib.import_module(f"{PKG}.heating_pdc_collector")

spec = importlib.util.spec_from_file_location("_verify_m3_for_collector", PDC_DIR / "verify_m3.py")
v = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = v
spec.loader.exec_module(v)

# The harness must judge the very classes the collector produces.
v.policy = pkg_policy
for room in v.ROOMS.values():
    old = room["config"]
    room["config"] = pkg_policy.PdcPolicyConfig(setpoint_min_c=old.setpoint_min_c, manual_preset_bypass=old.manual_preset_bypass)


def room_config(room: dict[str, Any], other: dict[str, Any]) -> tuple[Any, Any]:
    """The collector configuration equivalent to the harness room (entity ids of the baseline)."""
    p = room["prefix"]
    op = other["prefix"]
    carlo = room["config"].manual_preset_bypass
    entry = lambda r: {  # noqa: E731
        "room_key": r["prefix"],
        "radiator_entity_id": f"climate.trv_vtherm_{r['prefix']}",
        "radiator_valve_entity_id": f"climate.trv_{r['prefix']}",
        "temperature_entity_id": f"sensor.temperatura_{r['prefix']}",
        "window_entity_id": r["window"],
        "split_entity_id": r["split"],
        "pdc_entity_id": f"climate.{r['prefix']}_pompa_di_calore",
        "automatic_entity_id": f"input_boolean.{r['prefix']}_pdc_automatica",
        "schedule_stop_entity_id": f"sensor.{r['prefix']}_pdc_prossimo_stop_programma",
        "override_timer_entity_id": f"timer.override_riscaldamento_{r['prefix']}",
        "schedule_switch_entity_ids": [f"switch.schedule_{r['prefix']}_vtherm_feriale", f"switch.schedule_{r['prefix']}_vtherm_weekend"],
        "managed_entity_id": f"input_boolean.{r['prefix']}_pdc_in_gestione",
        "override_entity_id": f"input_boolean.{r['prefix']}_pdc_override",
        "cycle_entity_id": f"input_text.{r['prefix']}_pdc_ciclo",
        "setpoint_min_c": r["config"].setpoint_min_c,
        "manual_preset_bypass": r["config"].manual_preset_bypass,
    }
    section = {
        "mode": "observe",
        "power_entity_id": "sensor.energy_meter_power_a_casa",
        "central_mode_entity_id": "select.central_mode",
        "rooms": [entry(room), entry(other)],
    }
    config = pkg_collector.parse_pdc_config(section)
    assert carlo == config.room(p).policy.manual_preset_bypass and config.room(op) is not None
    return config, config.room(p)


stats = {"snapshots": 0, "conditions": 0, "target_confirmed": 0, "radiator_true": 0}
CURRENT: dict[str, Any] = {}
original_build = v.build


def build_with_collector(room: dict[str, Any], case: dict[str, Any]):
    w, reference, decoded, diag = original_build(room, case)
    other = next(r for r in v.ROOMS.values() if r is not room)
    config, room_cfg = room_config(room, other)
    collection = pkg_collector.collect_pdc_snapshot(w, room_cfg, config)
    got = collection.snapshot
    assert got == reference, {"room": room["prefix"], "case": case, "collector": got, "reference": reference}
    stats["snapshots"] += 1
    check_observation(room, case, w, got, room_cfg, config)
    return w, got, decoded, diag


def check_observation(room: dict[str, Any], case: dict[str, Any], w: Any, snap: Any, room_cfg: Any, config: Any) -> None:
    now = case["now"]
    policy_cfg = room["config"]
    chooser = room["chooser"]["actions"][2]["choose"]
    start = chooser[2]["sequence"]
    jinja = {
        "waiting_radiator": v.test_conditions(start[3]["if"], w),
        "waiting_target": v.test_conditions(start[3]["then"][3]["if"], w),
        "waiting_heat": v.test_conditions(start[3]["then"][3]["then"][4]["if"], w),
    }
    for phase, expected in jinja.items():
        assert expected is not None, (phase, case)
        obs = pkg_collector.build_observation(1, snap, now, policy_cfg, phase=phase, override_off=True)
        assert obs.conditions_ok is expected, {"phase": phase, "case": case, "jinja": expected, "obs": obs}
        stats["conditions"] += 1
    obs = pkg_collector.build_observation(1, snap, now, policy_cfg, phase="running", override_off=None)
    assert obs.conditions_ok is None and obs.override_off is None
    wait_ok = v.render(start[2]["wait_template"], w).lower() == "true"
    assert (obs.radiator_stopped is True) == wait_ok, {"case": case, "obs": obs}
    stats["radiator_true"] += int(wait_ok)
    if wait_ok is False:
        assert obs.radiator_stopped in (False, None)
    if snap.target is not None:
        differs = v.render(chooser[1]["conditions"][1]["value_template"], w).lower() == "true"
        assert (obs.target_confirmed is not True) == differs, {"case": case, "obs": obs, "differs": differs}
        stats["target_confirmed"] += 1
    assert obs.split_mode == snap.split_mode and obs.pdc_mode == snap.pdc_mode and obs.radiator_inhibited == snap.radiator_inhibited


v.build = build_with_collector


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--random", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=20261126)
    parser.add_argument("--out", type=str, default=None, help="write the result as JSON")
    args = parser.parse_args()
    result: dict[str, Any] = {"collector_sha256": __import__("hashlib").sha256((COMPONENT_DIR / "heating_pdc_collector.py").read_bytes()).hexdigest()}
    for room_key in v.ROOMS:
        before = dict(stats)
        run_stats: dict[str, Any] = {
            "branches": {},
            "journal": 0,
            "setpoint": 0,
            "recheck_radiator": 0,
            "recheck_setpoint": 0,
            "confirm_running": 0,
            "protection_stop": 0,
            "register_writes": 0,
        }
        scenarios = v.boundary_scenarios()
        for case in scenarios.values():
            v.compare(room_key, case, run_stats)
        # the radiator-stop reading and the three sequence re-checks are rarely true in the random cases: a grid of their inputs
        grid = 0
        for valve, mode, inhibited, power, window, setpoint in itertools.product(
            ("heat", "off", "unknown"), ("heat", "off", "unknown"), (None, True, False), ("400", "1100", "1101"), ("off", "on"), (20.0, 19.0, None)
        ):
            v.compare(room_key, v.scenario(valve=valve, radiator_mode=mode, inhibited=inhibited, power=power, window=window, pdc_setpoint=setpoint, trigger="tick"), run_stats)
            grid += 1
        rng = random.Random(args.seed + (0 if room_key == "camera_carlo" else 1))
        for _ in range(args.random):
            v.compare(room_key, v.random_case(rng), run_stats)
        result[room_key] = {
            "boundary_cases": len(scenarios),
            "grid_cases": grid,
            "random_cases": args.random,
            "seed": args.seed + (0 if room_key == "camera_carlo" else 1),
            "checked": {k: stats[k] - before[k] for k in stats},
            "branches": run_stats["branches"],
        }
    print(json.dumps(result, indent=1, ensure_ascii=False))
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
