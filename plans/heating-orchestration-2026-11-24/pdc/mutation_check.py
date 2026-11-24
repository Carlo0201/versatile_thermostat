"""Mutation check of the PDC parity harness (M3).

Each mutation is a deliberate defect injected into a scratch copy of
``heating_pdc_policy.py`` (the real file is never touched). The parity harness
``verify_m3.py`` is run against the copy through ``PDC_POLICY_PATH``: a mutation is
*killed* when the harness fails, *survived* when the parity still passes.

A survivor means that either the mutation is behaviourally equivalent or the
harness does not discriminate that rule: survivors are reported, never hidden.

Usage::

    python plans/heating-orchestration-2026-11-24/pdc/mutation_check.py [--random N] [--seed S] [--out FILE]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PDC_DIR = Path(__file__).resolve().parent
REPO = PDC_DIR.parents[2]
POLICY = REPO / "custom_components" / "versatile_thermostat" / "heating_pdc_policy.py"
HARNESS = PDC_DIR / "verify_m3.py"
UNIT_TESTS = REPO / "tests" / "test_heating_pdc_policy.py"

# (name, original text, replacement). The original must occur exactly once.
MUTATIONS: list[tuple[str, str, str]] = [
    ("start_gap_0.4_to_0.5", "start_gap_c: float = 0.4", "start_gap_c: float = 0.5"),
    ("start_gap_0.4_to_0.3", "start_gap_c: float = 0.4", "start_gap_c: float = 0.3"),
    (
        "gap_ge_to_gt",
        "(snapshot.target - snapshot.temperature) >= config.start_gap_c - config.comparison_epsilon_c",
        "(snapshot.target - snapshot.temperature) > config.start_gap_c - config.comparison_epsilon_c",
    ),
    (
        "epsilon_removed_in_gap",
        "(snapshot.target - snapshot.temperature) >= config.start_gap_c - config.comparison_epsilon_c",
        "(snapshot.target - snapshot.temperature) >= config.start_gap_c",
    ),
    ("stability_120_to_121", "stability_s: float = 120.0", "stability_s: float = 121.0"),
    ("power_reserve_1500_to_1400", "power_reserve_w: float = 1500.0", "power_reserve_w: float = 1400.0"),
    ("power_budget_2600_to_2650", "power_budget_w: float = 2600.0", "power_budget_w: float = 2650.0"),
    ("capacity_le_to_lt", "snapshot.power + config.power_reserve_w <= config.power_budget_w", "snapshot.power + config.power_reserve_w < config.power_budget_w"),
    ("capacity_accepts_negative_power", "return 0 <= snapshot.power and snapshot.power + config.power_reserve_w", "return snapshot.power + config.power_reserve_w"),
    ("diag_power_limit_1100_to_1101", "diagnostic_power_limit_w: float = 1100.0", "diagnostic_power_limit_w: float = 1101.0"),
    ("stop_power_2700_to_2701", "stop_power_w: float = 2700.0", "stop_power_w: float = 2701.0"),
    (
        "keep_running_power_le",
        "and snapshot.power < config.stop_power_w\n        and snapshot.pdc_mode == STATE_HEAT",
        "and snapshot.power <= config.stop_power_w\n        and snapshot.pdc_mode == STATE_HEAT",
    ),
    ("power_age_45_to_46", "power_max_age_s: float = 45.0", "power_max_age_s: float = 46.0"),
    (
        "power_age_lt",
        "return snapshot.power is not None and _age(now, snapshot.power_reported_at) <= config.power_max_age_s",
        "return snapshot.power is not None and _age(now, snapshot.power_reported_at) < config.power_max_age_s",
    ),
    (
        "power_unknown_as_zero",
        "return snapshot.power is not None and _age(now, snapshot.power_reported_at) <= config.power_max_age_s",
        "return _age(now, snapshot.power_reported_at) <= config.power_max_age_s",
    ),
    ("temperature_age_3600_to_3601", "temperature_max_age_s: float = 3600.0", "temperature_max_age_s: float = 3601.0"),
    (
        "temperature_age_lt",
        "and _age(now, snapshot.temperature_reported_at) <= config.temperature_max_age_s\n\n\ndef _power_fresh",
        "and _age(now, snapshot.temperature_reported_at) < config.temperature_max_age_s\n\n\ndef _power_fresh",
    ),
    ("schedule_horizon_600_to_599", "schedule_horizon_s: float = 600.0", "schedule_horizon_s: float = 599.0"),
    (
        "schedule_gt_to_ge",
        "return (0.0 if value is None else value) > now + config.schedule_horizon_s",
        "return (0.0 if value is None else value) >= now + config.schedule_horizon_s",
    ),
    ("schedule_invalid_ok", "return (0.0 if value is None else value) > now + config.schedule_horizon_s", "return value is None or value > now + config.schedule_horizon_s"),
    ("cooldown_600_to_599", "off_cooldown_s: float = 600.0", "off_cooldown_s: float = 599.0"),
    ("cooldown_le_to_lt", "0 <= stopped <= now - config.off_cooldown_s", "0 <= stopped < now - config.off_cooldown_s"),
    ("cooldown_future_ok", "0 <= stopped <= now - config.off_cooldown_s", "stopped <= now - config.off_cooldown_s"),
    ("cooldown_ignores_mode", "return snapshot.cycle.mode == STATE_OFF and stopped is not None and 0 <= stopped", "return stopped is not None and 0 <= stopped"),
    ("raw_guard_600_to_599", "raw_guard_s: float = 600.0", "raw_guard_s: float = 599.0"),
    ("raw_guard_split_gt", "and _age(now, snapshot.split_changed_at) >= config.raw_guard_s", "and _age(now, snapshot.split_changed_at) > config.raw_guard_s"),
    ("raw_guard_managed_gt", "and _age(now, snapshot.managed_changed_at) >= config.raw_guard_s", "and _age(now, snapshot.managed_changed_at) > config.raw_guard_s"),
    ("min_on_600_to_599", "min_on_s: float = 600.0", "min_on_s: float = 599.0"),
    ("min_on_ge_to_gt", "now - started >= config.min_on_s", "now - started > config.min_on_s"),
    ("stop_margin_0.2_to_0.3", "stop_margin_c: float = 0.2", "stop_margin_c: float = 0.3"),
    ("early_margin_0.5_to_0.6", "early_stop_margin_c: float = 0.5", "early_stop_margin_c: float = 0.6"),
    ("thermal_stop_no_min_on", "or (minimum_on_done(snapshot, now, config) and t >= target + config.stop_margin_c - eps)", "or (t >= target + config.stop_margin_c - eps)"),
    ("thermal_stop_ignores_target_lowered", "- eps) or target_lowered(snapshot)", "- eps)"),
    ("thermal_stop_accepts_future_sample", "not 0 <= _age(now, snapshot.temperature_reported_at) <=", "not _age(now, snapshot.temperature_reported_at) <="),
    (
        "target_lowered_le",
        "return target is not None and start is not None and target < start and t is not None and t >= target",
        "return target is not None and start is not None and target <= start and t is not None and t >= target",
    ),
    (
        "target_lowered_gt",
        "return target is not None and start is not None and target < start and t is not None and t >= target",
        "return target is not None and start is not None and target < start and t is not None and t > target",
    ),
    ("window_grace_120_to_121", "window_running_grace_s: float = 120.0", "window_running_grace_s: float = 121.0"),
    ("window_grace_strict_to_inclusive", "return not (now - config.window_running_grace_s > changed)", "return not (now - config.window_running_grace_s >= changed)"),
    (
        "window_unknown_as_closed_running",
        "    if snapshot.window != STATE_ON:\n        return False\n    changed = 0.0 if snapshot.window_changed_at is None else snapshot.window_changed_at\n    return not (now - config.window_running_grace_s > changed)",
        "    if snapshot.window != STATE_ON:\n        return True\n    changed = 0.0 if snapshot.window_changed_at is None else snapshot.window_changed_at\n    return not (now - config.window_running_grace_s > changed)",
    ),
    (
        "start_requires_window_not_on_only",
        "automatic_ok(snapshot, config)\n        and snapshot.window == STATE_OFF",
        "automatic_ok(snapshot, config)\n        and snapshot.window != STATE_ON",
    ),
    ("setpoint_offset_1_to_0", "setpoint_offset_c: float = 1.0", "setpoint_offset_c: float = 0.0"),
    ("setpoint_min_16_to_17", "setpoint_min_c: float = 16.0", "setpoint_min_c: float = 17.0"),
    ("setpoint_max_30_to_29", "setpoint_max_c: float = 30.0", "setpoint_max_c: float = 29.0"),
    (
        "setpoint_floor_instead_of_ceil",
        "int(math.ceil(float(target + config.setpoint_offset_c) * 1.0) / 1.0)",
        "int(math.floor(float(target + config.setpoint_offset_c) * 1.0) / 1.0)",
    ),
    ("override_window_120_to_121", "override_window_open_s: float = 120.0", "override_window_open_s: float = 121.0"),
    ("override_unknown_30_to_31", "override_window_unknown_s: float = 30.0", "override_window_unknown_s: float = 31.0"),
    ("override_protection_power_gt", "p is None or p < 0 or p >= config.stop_power_w", "p is None or p < 0 or p > config.stop_power_w"),
    ("override_protection_central_stopped_ignored", "or snapshot.central_mode == config.central_stopped_mode:", ":"),
    ("preset_bypass_always", "if not config.manual_preset_bypass:\n        return False", "if False:\n        return False"),
    ("preset_bypass_ignores_switches", "all(switch == STATE_OFF for switch in snapshot.schedule_switches)", "True"),
    ("central_mode_allows_stopped", 'allowed_central_modes: tuple[str, ...] = ("Auto", "Heat only")', 'allowed_central_modes: tuple[str, ...] = ("Auto", "Heat only", "Stopped")'),
    ("other_pdc_ignores_managed", "return snapshot.other_managed == STATE_OFF and snapshot.other_split_mode == STATE_OFF", "return snapshot.other_split_mode == STATE_OFF"),
    ("other_pdc_ignores_split", "return snapshot.other_managed == STATE_OFF and snapshot.other_split_mode == STATE_OFF", "return snapshot.other_managed == STATE_OFF"),
    (
        "other_pdc_unknown_as_free",
        "return snapshot.other_managed == STATE_OFF and snapshot.other_split_mode == STATE_OFF",
        "return snapshot.other_managed != STATE_ON and snapshot.other_split_mode in (STATE_OFF, STATE_UNKNOWN, STATE_UNAVAILABLE, None)",
    ),
    ("evaluate_ignores_override", "if snapshot.override != STATE_OFF:", "if snapshot.override == STATE_ON and False:"),
    ("evaluate_unknown_override_as_off", "if snapshot.override != STATE_OFF:", "if snapshot.override == STATE_ON:"),
    (
        "stop_reason_window_before_power",
        "    if p is not None and p >= config.stop_power_w:\n        return STOP_POWER_LIMIT\n    if snapshot.window != STATE_OFF:\n        return STOP_WINDOW",
        "    if snapshot.window != STATE_OFF:\n        return STOP_WINDOW\n    if p is not None and p >= config.stop_power_w:\n        return STOP_POWER_LIMIT",
    ),
    (
        "diag_cooldown_dropped",
        "(CODE_COOLDOWN, snapshot.cycle.mode != STATE_OFF or stopped is None or stopped > now - config.off_cooldown_s or stopped < 0),",
        "(CODE_COOLDOWN, False),",
    ),
    (
        "diag_small_gap_gt",
        "(CODE_SMALL_GAP, t is not None and target is not None and target - t < config.start_gap_c - eps),",
        "(CODE_SMALL_GAP, t is not None and target is not None and target - t <= config.start_gap_c - eps),",
    ),
    (
        "diag_power_high_ge",
        "(CODE_POWER_HIGH, p is not None and p > config.diagnostic_power_limit_w),",
        "(CODE_POWER_HIGH, p is not None and p >= config.diagnostic_power_limit_w),",
    ),
    ("diag_schedule_lt", "schedule is not None and schedule <= now + config.schedule_horizon_s", "schedule is not None and schedule < now + config.schedule_horizon_s"),
    ("diag_at_target_gt", "t is not None and target is not None and t >= target),", "t is not None and target is not None and t > target),"),
    ("observe_register_no_missing_on", 'missing_on = mode == STATE_HEAT and _coerce_float(previous.get("on")) is None', "missing_on = False"),
    ("observe_register_truncation_to_round", "    ts = int(n)\n", "    ts = int(round(n))\n"),
    ("classify_failure_trv_swapped", "return FAILURE_TRV_UNCONFIRMED", "return FAILURE_SPLIT_UNCONFIRMED"),
    ("recheck_setpoint_ignores_setpoint", "and snapshot.pdc_setpoint == split_setpoint(snapshot.target, config) and schedule_ok", "and schedule_ok"),
    ("radiator_stopped_ignores_inhibition", "and snapshot.radiator_inhibited is True", ""),
]


def run_case(name: str, old: str, new: str, source: str, random_cases: int, seed: int, workdir: Path) -> dict[str, object]:
    count = source.count(old)
    if count != 1:
        return {"name": name, "status": "invalid", "detail": f"original text found {count} times"}
    mutant = workdir / f"mutant_{name}.py"
    mutant.write_text(source.replace(old, new), encoding="utf-8")
    env = dict(os.environ, PDC_POLICY_PATH=str(mutant))
    done = subprocess.run([sys.executable, str(HARNESS), "--random", str(random_cases), "--seed", str(seed), "--no-report"], capture_output=True, text=True, env=env, check=False)
    if done.returncode != 0:
        last = (done.stderr.strip().splitlines() or ["?"])[-1][:200]
        return {"name": name, "status": "killed", "by": "parity_harness", "detail": last}
    # Parity survived: the mutant may still be caught by the unit tests (rules the harness does not exercise).
    unit = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--noconftest", "--import-mode=importlib", "-p", "no:cacheprovider", "-x", "-k", "not parity", str(UNIT_TESTS)],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
        check=False,
    )
    if unit.returncode != 0:
        failed = [line for line in unit.stdout.splitlines() if line.startswith("FAILED")]
        return {"name": name, "status": "killed", "by": "unit_tests", "detail": (failed or ["?"])[0][:200]}
    return {"name": name, "status": "survived"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--random", type=int, default=0, help="random cases per room used to kill (0 = boundary scenarios only)")
    parser.add_argument("--seed", type=int, default=20261124)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    source = POLICY.read_text(encoding="utf-8")
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, old, new in MUTATIONS:
            results.append(run_case(name, old, new, source, args.random, args.seed, Path(tmp)))
            print(f"{results[-1]['status']:9} {name}", flush=True)
    summary = {
        "policy_sha256": hashlib.sha256(POLICY.read_bytes()).hexdigest(),
        "random_cases_per_room": args.random,
        "seed": args.seed,
        "total": len(results),
        "killed": sum(r["status"] == "killed" for r in results),
        "killed_by_parity_harness": sum(r.get("by") == "parity_harness" for r in results),
        "killed_by_unit_tests_only": [r["name"] for r in results if r.get("by") == "unit_tests"],
        "survived": [r["name"] for r in results if r["status"] == "survived"],
        "invalid": [r for r in results if r["status"] == "invalid"],
        "results": results,
    }
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, indent=1))
    if args.out:
        args.out.write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    return 0 if not summary["survived"] and not summary["invalid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
