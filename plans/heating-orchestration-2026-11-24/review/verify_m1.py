"""Independent offline review: load only the pure module and replay the Jinja plan."""
import ast
import dataclasses
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import sys

import jinja2

PLAN_DIR = Path(__file__).resolve().parents[1]
ROOT = PLAN_DIR.parents[1]
module_path = ROOT / "custom_components/versatile_thermostat/heating_boiler_policy.py"
spec = importlib.util.spec_from_file_location("review_boiler_policy", module_path)
policy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = policy
spec.loader.exec_module(policy)
baseline = json.loads((PLAN_DIR / "baseline/caldaia_controllo_stabile_delle_richieste.json").read_text(encoding="utf-8"))
template = jinja2.Environment().from_string(baseline["config"]["actions"][1]["variables"]["plan"])
cases = json.loads((PLAN_DIR / "baseline/boiler-decision-cases.json").read_text(encoding="utf-8"))

def evaluate(raw, cycle, now):
    snapshot = policy.BoilerSnapshot(**{**raw, "rooms": tuple(policy.QualifiedBoilerRoom(**room) for room in raw["rooms"])})
    decision = policy.evaluate_boiler(snapshot, policy.parse_cycle_context(cycle), now)
    single = decision.cycle.single
    actual = {"action": decision.action, "reason": decision.reason, "deadline": decision.deadline,
              "cycle": {"m": decision.cycle.mode, "off": decision.cycle.off_since, "r": decision.cycle.normal_since,
                        "s": [] if single is None else [single.slug, single.target, single.since]}}
    return decision, actual

def compare(raw, cycle, now):
    _, actual = evaluate(raw, cycle, now)
    expected = ast.literal_eval(template.render(raw=raw, cycle=cycle, now_ts=now).strip())
    assert actual == expected, {"raw": raw, "cycle": cycle, "now": now, "python": actual, "jinja": expected}
    return actual

saved = {entry["name"]: entry["result"] for entry in cases["results"]}
differences = []
for case in cases["cases"]:
    actual = compare(case["raw"], case["cycle"], case["now_ts"])
    if actual != saved[case["name"]]:
        differences.append({"name": case["name"], "saved": saved[case["name"]], "current_template_and_python": actual})

rng = random.Random(104729)
for _ in range(5000):
    count = rng.choice([0, 1, 2, 3])
    rooms = [{"slug": f"room_{i}", "target": rng.choice([18, 20]), "gap": rng.choice([-.2, .299, .3, .5]), "pilot_ok": rng.choice([False, True])}
             for i in range(rng.randint(1, count) if count else 0)]
    raw = {"ok": rng.choice([True, False]), "master": rng.choice(["off", "heat", "unknown", "unavailable"]),
           "count": count, "rooms": rooms, "manual": rng.choice(["on", "off", "unknown", None]),
           "pilot": rng.choice(["on", "off", "unknown", None]), "pilot_ready": rng.choice([True, False, None]), "consent": rng.choice([True, False, None])}
    now = rng.choice([2000, 2000.75, 0, 9000])
    cycle = {"m": rng.choice(["off", "heat", None]), "off": rng.choice([None, 0, 1000, 1400.5, 3000]),
             "r": rng.choice([None, 0, 1880, 1881, 1999.5, 3000]), "s": rng.choice([[], ["room_0", 18, 1100], ["room_0", 20, 1101]])}
    compare(raw, cycle, now)

def diagnostic_case(**changes):
    raw = {"ok": True, "master": "off", "count": 1, "rooms": [{"slug": "room_0", "target": 18, "gap": .1, "pilot_ok": False}],
           "manual": "on", "pilot": "on", "pilot_ready": False, "consent": False, **changes}
    result, _ = evaluate(raw, {"m": "off", "off": 1000}, 2000)
    return {"action": result.action, "reason": result.reason, "conditions": [dataclasses.asdict(c) for c in result.conditions]}

index = json.loads((PLAN_DIR / "baseline/index.json").read_text(encoding="utf-8-sig"))
for item in index["files"]:
    assert hashlib.sha256((PLAN_DIR / item["file"]).read_bytes()).hexdigest() == item["sha256"]
report = {"baseline_files_hash_verified": len(index["files"]), "cases_matching_current_jinja": len(cases["cases"]),
          "random_cases_matching_current_jinja": 5000, "seed": 104729, "saved_fixture_differences": differences,
          "diagnostic_reproductions": {"manual_bypasses_pilot_and_gap": diagnostic_case(),
                                        "maintenance_ignores_pilot": diagnostic_case(master="heat", manual="off")}}
(PLAN_DIR / "review/M1-verifica.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
print(json.dumps({"hashes": 17, "baseline_parity": 27, "random_parity": 5000, "fixture_differences": [d["name"] for d in differences],
                  "diagnostic_reproductions": {k: {"action": v["action"], "reason": v["reason"], "statuses": {c["id"]: c["status"] for c in v["conditions"]}} for k, v in report["diagnostic_reproductions"].items()}}, indent=2))
