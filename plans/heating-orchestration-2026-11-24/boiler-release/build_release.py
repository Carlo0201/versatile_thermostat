"""Build a boiler-only candidate without shipping the working-tree PDC changes."""

import ast
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

REPO = Path(__file__).resolve().parents[3]
PROJECT = REPO.parents[1]
VERSION = "10.6.3+oikos.2"
OUTPUT = PROJECT / "output/versatile_thermostat_boiler_control_2026-11-25"
COMPONENT_REL = Path("custom_components/versatile_thermostat")


def replace_once(text, before, after):
    assert text.count(before) == 1, before
    return text.replace(before, after, 1)


def main():
    if OUTPUT.exists() and any(OUTPUT.iterdir()):
        raise FileExistsError(f"Refusing to overwrite an existing release: {OUTPUT}")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    base_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    archive = subprocess.check_output(["git", "archive", "--format=zip", "HEAD", COMPONENT_REL.as_posix()], cwd=REPO)
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        for member in source.namelist():
            assert (member == "custom_components/" or member.startswith(COMPONENT_REL.as_posix() + "/")) and ".." not in Path(member).parts
        source.extractall(OUTPUT)
    component = OUTPUT / COMPONENT_REL
    # Only these boiler modules are taken from the working tree. Everything PDC
    # and every shared flow/schema/translation remains at the known stable commit.
    for name in ("heating_boiler_command.py", "feature_heating_orchestration_manager.py"):
        shutil.copyfile(REPO / COMPONENT_REL / name, component / name)
    collector = component / "heating_boiler_collector.py"
    collector.write_text(replace_once(collector.read_text(encoding="utf-8"), "BOILER_CONTROL_AVAILABLE: Final = False", "BOILER_CONTROL_AVAILABLE: Final = True"), encoding="utf-8")
    init = component / "__init__.py"
    text = init.read_text(encoding="utf-8")
    line = "    had_orchestration = bool(api and entry.data.get(CONF_THERMOSTAT_TYPE) == CONF_THERMOSTAT_CENTRAL_CONFIG and api.heating_orchestration_manager is not None)"
    text = replace_once(text, line, line + "\n    previous_orchestration = api.heating_orchestration_manager if had_orchestration else None")
    line = "        restored = await api.async_create_heating_orchestration_manager(entry, entry.data.get(CONF_HEATING_ORCHESTRATION))"
    text = replace_once(text, line, line + "\n        restored.adopt_publishers(previous_orchestration)")
    init.write_text(text, encoding="utf-8")
    manifest_path = component / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = VERSION
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # PDC remains byte-identical to the stable baseline and its control gate stays closed.
    pdc = (component / "feature_heating_pdc_manager.py").read_text(encoding="utf-8")
    assert "PDC_CONTROL_AVAILABLE = False" in pdc
    for path in component.rglob("*.py"):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    for path in component.rglob("*.json"):
        json.loads(path.read_text(encoding="utf-8"))
    package = OUTPUT / f"versatile_thermostat-{VERSION}-boiler-control.zip"
    files = sorted(path for path in component.rglob("*") if path.is_file())
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for path in files:
            target.write(path, path.relative_to(OUTPUT).as_posix())
    hashes = {path.relative_to(OUTPUT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    (OUTPUT / "SHA256SUMS-contenuto.txt").write_text("".join(f"{digest}  {name}\n" for name, digest in hashes.items()), encoding="utf-8")
    package_hash = hashlib.sha256(package.read_bytes()).hexdigest()
    (OUTPUT / "BUILD.json").write_text(
        json.dumps(
            {
                "version": VERSION,
                "base_commit": base_commit,
                "files": len(files),
                "package_sha256": package_hash,
                "boiler_control_available": True,
                "pdc_control_available": False,
                "working_tree_overlays": ["heating_boiler_command.py", "feature_heating_orchestration_manager.py"],
                "additional_changes": ["boiler release gate", "boiler publisher reattachment", "manifest version"],
                "ha_runtime_tests": "not run: user explicitly excluded an isolated Home Assistant environment",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(OUTPUT), "version": VERSION, "files": len(files), "sha256": package_hash}))


if __name__ == "__main__":
    main()
