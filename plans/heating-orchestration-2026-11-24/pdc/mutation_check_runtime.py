"""Mutation check of the PDC runtime work (W2): every mutant must be killed by the pure tests.

Each mutant is a one-line change of a safety rule of the M4 machine (``heating_pdc_transaction.py``), of the
collector (``heating_pdc_collector.py``) or of the engine (``heating_pdc_runtime.py``). The modules are copied to a
scratch directory, one mutant at a time, and the pure PDC tests (including the Jinja parity of the collector, when
Jinja2 is installed) are run against the copy with ``PDC_COMPONENT_DIR``. A surviving mutant is a rule the tests do
not protect.

Usage, from the repository root, with an interpreter that has pytest (and Jinja2 for the parity)::

    python plans/heating-orchestration-2026-11-24/pdc/mutation_check_runtime.py [--out result.json] [--only ID ...]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
COMPONENT = REPO / "custom_components" / "versatile_thermostat"
MODULES = ("heating_pdc_policy", "heating_pdc_provenance", "heating_pdc_transaction", "heating_pdc_collector", "heating_pdc_runtime")
TESTS = (
    "tests/test_heating_pdc_transaction.py",
    "tests/test_heating_pdc_collector.py",
    "tests/test_heating_pdc_runtime.py",
    "tests/test_heating_pdc_integration.py",
    "tests/test_heating_pdc_fake_ha.py",
)

T = "heating_pdc_transaction"
C = "heating_pdc_collector"
R = "heating_pdc_runtime"

# (id, module, old text, new text)
MUTANTS = [
    # --- ownership of the inhibition ---
    ("own_foreign_start_allowed", T, "    if radiator_inhibited_before is True:\n", "    if False:\n"),
    ("own_unknown_start_allowed", T, "    if radiator_inhibited_before is not False:\n", "    if False:\n"),
    ("own_claim_dropped_not_applied", T, "    if inhibited_after is True and transitioned is not False:\n", "    if inhibited_after is True:\n"),
    ("own_release_without_ownership", T, "    if tx.inhibition_owned:\n        releasing = replace(", "    if True:\n        releasing = replace("),
    ("own_manual_always_owned", T, "    owned = radiator_inhibited_before is False\n", "    owned = True\n"),
    ("own_foreign_stop_accepted", T, "        if stopped and not tx.inhibition_owned:\n", "        if False:\n"),
    ("own_release_ignores_ownership_runtime", R, "        if tx.phase != PHASE_RELEASING or not tx.inhibition_owned:\n", "        if tx.phase != PHASE_RELEASING:\n"),
    ("own_inhibit_noop_looks_ours", R, "        if inhibited_now is not True:", "        if True:"),
    ("own_marker_not_compared", R, "        if inhibited_now is True:\n            transitioned = False\n", "        if False:\n            transitioned = False\n"),
    # --- never PDC without a confirmed radiator stop ---
    (
        "heat_without_radiator_stop_at_target",
        T,
        "        if confirmed and stopped and obs.conditions_ok is True and tx.inhibition_owned:",
        "        if confirmed and obs.conditions_ok is True and tx.inhibition_owned:",
    ),
    (
        "heat_without_target_confirmed",
        T,
        "        if confirmed and stopped and obs.conditions_ok is True and tx.inhibition_owned:",
        "        if stopped and obs.conditions_ok is True and tx.inhibition_owned:",
    ),
    (
        "target_step_without_radiator_stop",
        T,
        "        if stopped and obs.conditions_ok is True:\n            waiting = _enter(tx, PHASE_WAITING_TARGET",
        "        if obs.conditions_ok is True:\n            waiting = _enter(tx, PHASE_WAITING_TARGET",
    ),
    ("running_without_radiator_stop", T, "        if heated and stopped and obs.conditions_ok is True:", "        if heated and obs.conditions_ok is True:"),
    ("heat_not_revalidated", R, "            and obs.radiator_stopped is True\n            and obs.target_confirmed is True", "            and obs.target_confirmed is True"),
    ("heat_revalidation_skipped", R, "        if not safe:\n", "        if False:\n"),
    ("start_phase_protection_off", C, "    if not automatic_ok(snapshot, config):\n        return True\n", "    if False:\n        return True\n"),
    ("start_phase_power_limit_off", C, "    if power is None or power >= config.stop_power_w", "    if power is None"),
    # --- never the radiator back without PDC OFF confirmed ---
    ("off_without_pdc_mode", T, "obs.split_mode == STATE_OFF and obs.pdc_mode == STATE_OFF and heat_settled", "obs.split_mode == STATE_OFF and heat_settled"),
    ("off_without_split_mode", T, "obs.split_mode == STATE_OFF and obs.pdc_mode == STATE_OFF and heat_settled", "obs.pdc_mode == STATE_OFF and heat_settled"),
    (
        "off_ignores_heat_in_flight",
        T,
        "obs.split_mode == STATE_OFF and obs.pdc_mode == STATE_OFF and heat_settled(tx, now, config)",
        "obs.split_mode == STATE_OFF and obs.pdc_mode == STATE_OFF",
    ),
    ("release_without_fresh_read", R, "        if snapshot.split_mode != STATE_OFF or snapshot.pdc_mode != STATE_OFF:\n", "        if False:\n"),
    ("release_while_suspended", R, "        if self._suspension.active and self._suspension.hold_radiator_inhibition:\n", "        if False:\n"),
    ("release_back_to_return_dropped", T, "        if obs.split_mode in ACTIVE_MODES or obs.pdc_mode in ACTIVE_MODES:\n", "        if False:\n"),
    ("release_unconfirmed_counts", T, "    if released is True:\n", "    if released is not False:\n"),
    (
        "late_off_confirmation_ignored",
        T,
        "        if off_confirmed(tx, obs, now, config):\n            # A late confirmation completes",
        "        if False:\n            # A late confirmation completes",
    ),
    (
        "return_unconfirmed_never_retried",
        T,
        "        if tx.deadline is not None and expired:\n            return _begin_return(tx, reservation, obs, now, FAILURE_RETURN_UNCONFIRMED, config)",
        "        if False:\n            return _begin_return(tx, reservation, obs, now, FAILURE_RETURN_UNCONFIRMED, config)",
    ),
    ("radiator_inhibited_false_shortcut", T, "        if obs.radiator_inhibited is False:\n", "        if obs.radiator_inhibited is not True:\n"),
    # --- override, ownership of the plant, persistence ---
    ("override_exempt_ignored", T, "    if not override_exempt and (obs is None or obs.override_off is not True):", "    if (obs is None or obs.override_off is not True):"),
    (
        "interlock_missing",
        R,
        "        return tuple(entity_id for entity_id in room.cfg.external_owner_entity_ids if lookup(states, entity_id).state != STATE_OFF)",
        "        return ()",
    ),
    (
        "interlock_not_rechecked_before_command",
        R,
        "            room.blocked_by = self._blocked_by(room)\n            if room.blocked_by:\n                return  #",
        "            room.blocked_by = ()\n            if room.blocked_by:\n                return  #",
    ),
    ("no_write_ahead", R, "        if step.commands or changed:\n            await self._save()", "        if False:\n            await self._save()"),
    (
        "start_without_durable_claim",
        R,
        "        if not await self._save():\n            room.tx, self._reservation = before_tx, before_res",
        "        if False:\n            room.tx, self._reservation = before_tx, before_res",
    ),
    ("start_without_inhibition_started", R, "        if self._ports.inhibition_started(room.cfg.radiator_entity_id) is not True:", "        if False:"),
    ("no_classification_deferral", R, "        if room.classify_pending:\n", "        if False:\n"),
    ("override_protection_off", R, "        if override_protection_stop(snapshot, now, room.cfg.policy):\n", "        if False:\n"),
    ("user_command_never_override", R, "            if is_user_command(user_id, service, targets, (room.cfg.split_entity_id, room.cfg.pdc_entity_id)):", "            if False:"),
    ("recovery_not_waiting", R, "        return unreadable and now - room.recovery_since < RECOVERY_WAIT_S", "        return False"),
    ("orphan_reservation_kept", R, "        self._reconcile_reservation()\n", "        pass\n"),
    (
        "suspend_allowed_outside_control",
        R,
        "        if not self.is_controlling:\n            return PdcSuspendResult(False, SUSPENSION_NONE, REFUSED_NOT_CONTROL)\n        if any(",
        "        if any(",
    ),
    # --- collector reading ---
    ("inhibition_missing_is_unknown", C, "    if section is None:\n        return False, None", "    if section is None:\n        return None, None"),
    (
        "inhibition_unreadable_is_false",
        C,
        "    if not isinstance(specific, Mapping):\n        return None, None",
        "    if not isinstance(specific, Mapping):\n        return False, None",
    ),
    ("stop_reading_unknown_is_false", C, "    if snapshot.radiator_inhibited is None or any(", "    if False or any("),
    (
        "unknown_power_is_zero",
        C,
        "    return None if view.state in (None, STATE_UNKNOWN, STATE_UNAVAILABLE) else as_number(view.state)",
        "    return 0.0 if view.state in (None, STATE_UNKNOWN, STATE_UNAVAILABLE) else as_number(view.state)",
    ),
    ("window_reported_instead_of_changed", C, "        window_changed_at=window.changed_at,", "        window_changed_at=window.reported_at,"),
    ("others_unknown_is_off", C, "        return next((value for value in values if value != STATE_OFF), None)", "        return STATE_OFF"),
    # --- observe sends nothing, control is not reachable (integration of the first installable release) ---
    (
        "observe_sends_a_command",
        R,
        "        room.generation += 1\n        self._publish(room, decision.reason, decision, owner=OWNER_OBSERVER, now=now)",
        '        room.generation += 1\n        self._ports.spawn(self._ports.call_service("climate", "set_hvac_mode", {"entity_id": room.cfg.pdc_entity_id, "hvac_mode": "off"}), "x")\n        self._publish(room, decision.reason, decision, owner=OWNER_OBSERVER, now=now)',
    ),
    (
        "observe_persists",
        R,
        "        room.generation += 1\n        self._publish(room, decision.reason, decision, owner=OWNER_OBSERVER, now=now)",
        '        room.generation += 1\n        self._ports.spawn(self._ports.save(self._store_data()), "x")\n        self._publish(room, decision.reason, decision, owner=OWNER_OBSERVER, now=now)',
    ),
    (
        "observe_touches_the_inhibition",
        R,
        "        room.generation += 1\n        self._publish(room, decision.reason, decision, owner=OWNER_OBSERVER, now=now)",
        "        room.generation += 1\n        self._ports.inhibition_started(room.cfg.radiator_entity_id)\n        self._publish(room, decision.reason, decision, owner=OWNER_OBSERVER, now=now)",
    ),
    ("control_gate_parser_open", C, "    if mode == MODE_CONTROL and not allow_control:", "    if False:"),
    ("control_gate_engine_open", R, "        if config.mode == MODE_CONTROL and not allow_control:", "        if False:"),
    ("control_gate_manager_flag_on", "feature_heating_pdc_manager", "PDC_CONTROL_AVAILABLE = False", "PDC_CONTROL_AVAILABLE = True"),
    ("control_offered_by_the_flow", "const", 'HO_PDC_SELECTABLE_MODES = ["disabled", "observe"]', 'HO_PDC_SELECTABLE_MODES = ["disabled", "observe", "control"]'),
    (
        "flow_drops_the_pdc_keys",
        "config_flow",
        "            for key in (CONF_HO_PDC_MODE, CONF_HO_PDC_RUNTIME):\n                if key in section:\n                    candidate[key] = section[key]\n",
        "            pass\n",
    ),
    ("pdc_manager_not_started", "__init__", "            await pdc.async_start()\n", "            pass\n"),
    (
        "pdc_manager_not_removed_on_unload",
        "__init__",
        "    if had_pdc:\n        await api.async_remove_heating_pdc_manager(entry.entry_id)\n",
        "    if False:\n        await api.async_remove_heating_pdc_manager(entry.entry_id)\n",
    ),
    ("pdc_sensor_recorded", "sensor", '            "conditions",\n            "evaluated_at",\n            "generation",\n            "deadline",', '            "deadline",'),
]


def run(mutant: tuple[str, str, str, str], python: str, keep: bool) -> dict[str, object]:
    mutant_id, module, old, new = mutant
    with tempfile.TemporaryDirectory(prefix="pdc_mutant_") as tmp:
        shutil.copytree(COMPONENT, tmp, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
        target = Path(tmp) / f"{module}.py"
        text = target.read_text(encoding="utf-8")
        if text.count(old) != 1:
            return {"id": mutant_id, "status": "invalid", "detail": f"snippet found {text.count(old)} times"}
        target.write_text(text.replace(old, new, 1), encoding="utf-8")
        env = dict(os.environ, PDC_COMPONENT_DIR=tmp)
        done = subprocess.run(
            [python, "-m", "pytest", "-q", "-x", "--noconftest", "--import-mode=importlib", "-p", "no:cacheprovider", *TESTS],
            capture_output=True,
            text=True,
            cwd=REPO,
            env=env,
            check=False,
        )
        failed = [line for line in done.stdout.splitlines() if line.startswith("FAILED")]
        return {"id": mutant_id, "status": "killed" if done.returncode != 0 else "SURVIVED", "by": failed[:1]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=str, default=None)
    parser.add_argument("--only", nargs="*", default=None)
    args = parser.parse_args()
    results = []
    for mutant in MUTANTS:
        if args.only and mutant[0] not in args.only:
            continue
        result = run(mutant, sys.executable, False)
        results.append(result)
        print(result["status"], mutant[0], result.get("by", result.get("detail", "")), flush=True)
    summary = {
        "total": len(results),
        "killed": sum(r["status"] == "killed" for r in results),
        "survived": [r["id"] for r in results if r["status"] == "SURVIVED"],
        "invalid": [r["id"] for r in results if r["status"] == "invalid"],
    }
    print(json.dumps(summary))
    if args.out:
        Path(args.out).write_text(json.dumps({"summary": summary, "results": results}, indent=1) + "\n", encoding="utf-8")
    return 0 if not summary["survived"] and not summary["invalid"] else 1


if __name__ == "__main__":
    sys.exit(main())
