"""Check four regression tests against deliberately broken, isolated copies."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[3]
COMPONENT = REPO / "custom_components/versatile_thermostat"
RUNNER = Path(__file__).with_name("offline_pytest.py")
CASES = [
    (
        "service_interlock",
        "if self._config.mode != MODE_CONTROL or self._stopped or self._interlock_lost(room):",
        "if self._config.mode != MODE_CONTROL or self._stopped:",
        "every_service_rechecks",
    ),
    (
        "release_confirmation",
        "released = read_inhibition(lookup(self._ports.states(), room.cfg.radiator_entity_id).attributes)[0] is False",
        "released = True",
        "release_service_success_without_state_change",
    ),
    (
        "stale_scheduled_mirror",
        "if epoch != self._epoch or not _still_desired() or self._interlock_lost(room):",
        "if epoch != self._epoch or self._interlock_lost(room):",
        "scheduled_mirror_drops_a_value",
    ),
    (
        "corrupt_store_blocks_control",
        "if self._load_errors:\n                self._publish_empty(room, STATE_ERROR, self._load_errors[0], now)",
        "if False:\n                self._publish_empty(room, STATE_ERROR, self._load_errors[0], now)",
        "corrupt_store_blocks_effects",
    ),
]


def main():
    results = []
    original = (COMPONENT / "heating_pdc_runtime.py").read_text(encoding="utf-8")
    scratch = REPO.parents[1] / "tmp" / "pdc-control-checks"
    scratch.mkdir(parents=True, exist_ok=True)
    scratch = scratch.resolve()
    for name, before, after, selection in CASES:
        assert original.count(before) == 1, (name, "mutation anchor must be unique")
        with tempfile.TemporaryDirectory(prefix="pdc-mutation-", dir=scratch) as temporary:
            directory = Path(temporary).resolve()
            assert directory.parent == scratch and directory.name.startswith("pdc-mutation-")
            target = directory / "component"
            target.mkdir()
            for source in COMPONENT.glob("heating_pdc_*.py"):
                shutil.copy2(source, target / source.name)
            (target / "heating_pdc_runtime.py").write_text(original.replace(before, after, 1), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "-q",
                    "--noconftest",
                    "--import-mode=importlib",
                    "-p",
                    "no:cacheprovider",
                    "tests/test_heating_pdc_control_completion.py",
                    "-k",
                    selection,
                ],
                cwd=REPO,
                env={**os.environ, "PDC_COMPONENT_DIR": str(target)},
                capture_output=True,
                text=True,
                timeout=60,
            )
            # Pytest exit 1 is an assertion failure; collection/import errors do not kill a mutation.
            killed = result.returncode == 1 and "FAILED tests/" in result.stdout.replace("\\", "/")
            results.append({"case": name, "detected": killed, "exit_code": result.returncode})
            print(json.dumps(results[-1]))
            if not killed:
                print((result.stdout + result.stderr)[-4000:])
    print(json.dumps({"checks": len(results), "detected": sum(item["detected"] for item in results)}))
    return 0 if all(item["detected"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
