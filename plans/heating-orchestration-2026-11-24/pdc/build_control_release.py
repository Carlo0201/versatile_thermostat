"""Package the PDC-control candidate over the known boiler-control release."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile

REPO = Path(__file__).resolve().parents[3]
PROJECT = REPO.parents[1]
COMPONENT = PurePosixPath("custom_components/versatile_thermostat")
BASE = PROJECT / "output/versatile_thermostat_boiler_control_2026-11-25/versatile_thermostat-10.6.3+oikos.2-boiler-control.zip"
BASE_SHA256 = "03a6d93418ea470fd010ad97908f0a1b3a7b20b5efc4abe329eae7565f56646b"
OUTPUT = PROJECT / "output/versatile_thermostat_pdc_control_2026-11-25"
VERSION = "10.6.3+oikos.3"
OVERLAYS = (
    "__init__.py",
    "config_flow.py",
    "config_schema.py",
    "const.py",
    "feature_heating_pdc_manager.py",
    "heating_pdc_collector.py",
    "heating_pdc_policy.py",
    "heating_pdc_runtime.py",
    "heating_pdc_transaction.py",
    "manifest.json",
    "room_override_policy.py",
    "room_override_runtime.py",
    "sensor.py",
    "services.yaml",
    "translations/en.json",
    "translations/it.json",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def enabled(source: bytes, name: str) -> bool:
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return isinstance(node.value, ast.Constant) and node.value.value is True
    return False


def main() -> None:
    if digest(BASE.read_bytes()) != BASE_SHA256:
        raise ValueError("The boiler-control base ZIP does not match its recorded SHA-256")

    with ZipFile(BASE) as archive:
        if archive.testzip() is not None:
            raise ValueError("Invalid boiler-control ZIP")
        names = [member.filename for member in archive.infolist() if not member.is_dir()]
        if len(names) != len(set(names)) or any(not PurePosixPath(name).is_relative_to(COMPONENT) or ".." in PurePosixPath(name).parts for name in names):
            raise ValueError("Unexpected or duplicate path in the base ZIP")
        base_files = {name: archive.read(name) for name in names}

    overlays = {str(COMPONENT / name): (REPO / COMPONENT / name).read_bytes() for name in OVERLAYS}
    files = {**base_files, **overlays}
    for name, data in files.items():
        if name.endswith(".py"):
            compile(data, name, "exec")
        elif name.endswith(".json"):
            json.loads(data)
    for name, gate in (
        ("heating_boiler_collector.py", "BOILER_CONTROL_AVAILABLE"),
        ("const.py", "HO_PDC_CONTROL_ENABLED"),
        ("feature_heating_pdc_manager.py", "PDC_CONTROL_AVAILABLE"),
    ):
        if not enabled(files[str(COMPONENT / name)], gate):
            raise ValueError(f"Required release gate is closed: {gate}")
    if json.loads(files[str(COMPONENT / "manifest.json")])["version"] != VERSION:
        raise ValueError("Manifest and package versions differ")

    OUTPUT.mkdir(parents=True, exist_ok=False)
    package = OUTPUT / f"versatile_thermostat-{VERSION}-pdc-control.zip"
    for name, data in files.items():
        destination = OUTPUT / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    with ZipFile(package, "x", compression=ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    with ZipFile(package) as archive:
        if archive.testzip() is not None or set(archive.namelist()) != set(files):
            raise ValueError("Incomplete or corrupt PDC control ZIP")
        for name, data in files.items():
            if archive.read(name) != data or (OUTPUT / name).read_bytes() != data:
                raise ValueError(f"Content mismatch for {name}")

    (OUTPUT / "SHA256SUMS-contenuto.txt").write_text("".join(f"{digest(data)}  {name}\n" for name, data in sorted(files.items())), encoding="utf-8")
    metadata = {
        "version": VERSION,
        "package_sha256": digest(package.read_bytes()),
        "base_zip": BASE.name,
        "base_sha256": BASE_SHA256,
        "files": len(files),
        "changed_from_base": sorted(name for name, data in files.items() if data != base_files.get(name)),
        "boiler_control_available": True,
        "pdc_control_available": True,
        "ha_runtime_tests": "not run, per request",
    }
    (OUTPUT / "BUILD.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
