"""Verify the candidate bytes, baseline delta and closed PDC gate."""

import hashlib
import json
from pathlib import Path
import subprocess
import zipfile


REPO = Path(__file__).resolve().parents[3]
OUTPUT = REPO.parents[1] / "output/versatile_thermostat_boiler_control_2026-11-25"


def main():
    build = json.loads((OUTPUT / "BUILD.json").read_text(encoding="utf-8"))
    package = OUTPUT / f"versatile_thermostat-{build['version']}-boiler-control.zip"
    assert hashlib.sha256(package.read_bytes()).hexdigest() == build["package_sha256"]
    changed = set()
    with zipfile.ZipFile(package) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist()) == build["files"]
        for name in archive.namelist():
            path = OUTPUT / name
            data = path.read_bytes()
            assert archive.read(name) == data
            if name.endswith(".py"):
                compile(data, name, "exec")
            elif name.endswith(".json"):
                json.loads(data)
            previous = subprocess.check_output(["git", "show", f"{build['base_commit']}:{name}"], cwd=REPO)
            if data != previous:
                changed.add(Path(name).name)
    assert changed == {"manifest.json", "__init__.py", "heating_boiler_collector.py", "heating_boiler_command.py", "feature_heating_orchestration_manager.py"}, changed
    component = OUTPUT / "custom_components/versatile_thermostat"
    assert "BOILER_CONTROL_AVAILABLE: Final = True" in (component / "heating_boiler_collector.py").read_text(encoding="utf-8")
    assert "PDC_CONTROL_AVAILABLE = False" in (component / "feature_heating_pdc_manager.py").read_text(encoding="utf-8")
    assert "HO_PDC_SELECTABLE_MODES = [\"disabled\", \"observe\"]" in (component / "const.py").read_text(encoding="utf-8")
    print(json.dumps({"verified_files": build["files"], "changed_from_stable": sorted(changed), "boiler_control": True, "pdc_control": False}))


if __name__ == "__main__":
    main()
