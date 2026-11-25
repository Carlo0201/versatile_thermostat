"""Build the installable package 10.6.3+oikos.5 from the working tree of the fork (read only on the repository).

Run with a Python 3.12+ from anywhere:  python -I plans/heating-orchestration-2026-11-24/release/build_release_oikos5.py

Checks before writing: version in manifest.json, every .py compiles, every .json parses, the release gates are
the ones of the known control build (boiler control, PDC control available), the M1 policy file is byte for byte
the one of the commit recorded below, no cache or test file is packed. The ZIP is deterministic (sorted, fixed time).
Nothing is installed and Home Assistant is not contacted.
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

REPO = Path(__file__).resolve().parents[3]
PROJECT = REPO.parents[1]
COMPONENT = PurePosixPath("custom_components/versatile_thermostat")
VERSION = "10.6.3+oikos.5"
OUTPUT = PROJECT / "output" / "versatile-thermostat-10.6.3-oikos.5"
ARCHIVE = OUTPUT / "versatile-thermostat-10.6.3-oikos.5.zip"
POLICY_COMMIT = "a8b62ab"
GATES = (("heating_boiler_collector.py", "BOILER_CONTROL_AVAILABLE"), ("heating_pdc_collector.py", "PDC_CONTROL_AVAILABLE"))  # HO_PDC_CONTROL_ENABLED is an alias of the second
FIXED_TIME = (2026, 11, 25, 0, 0, 0)


def is_true(source: bytes, name: str) -> bool:
    for node in ast.parse(source).body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return isinstance(node.value, ast.Constant) and node.value.value is True
    return False


def main() -> None:
    root = REPO / COMPONENT
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            files[str(COMPONENT / path.relative_to(root).as_posix())] = path.read_bytes().replace(b"\r\n", b"\n")
    manifest = json.loads(files[str(COMPONENT / "manifest.json")])
    if manifest.get("version") != VERSION:
        raise ValueError(f"manifest version is {manifest.get('version')!r}, expected {VERSION!r}")
    for name, data in files.items():
        if name.endswith(".py"):
            compile(data, name, "exec")
        elif name.endswith(".json"):
            json.loads(data)
    for module, gate in GATES:
        if not is_true(files[str(COMPONENT / module)], gate):
            raise ValueError(f"release gate closed: {gate}")
    policy = str(COMPONENT / "heating_boiler_policy.py")
    committed = subprocess.run(["git", "show", f"{POLICY_COMMIT}:{policy}"], cwd=REPO, capture_output=True, check=True).stdout.replace(b"\r\n", b"\n")
    if committed != files[policy]:
        raise ValueError("heating_boiler_policy.py differs from the frozen M1 policy")
    if any("/tests/" in name or name.endswith((".pyc", ".orig", ".rej")) for name in files):
        raise ValueError("unexpected file in the package")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    with ZipFile(ARCHIVE, "w", ZIP_DEFLATED) as archive:
        for name, data in files.items():
            info = ZipInfo(name, FIXED_TIME)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    with ZipFile(ARCHIVE) as archive:
        if archive.testzip() is not None or {n: archive.read(n) for n in archive.namelist()} != files:
            raise ValueError("the written archive does not match the verified files")
    digest = hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()
    (OUTPUT / "SHA256SUMS.txt").write_text(f"{digest}  {ARCHIVE.name}\n", encoding="utf-8", newline="\n")
    print(json.dumps({"archive": str(ARCHIVE), "files": len(files), "sha256": digest, "version": VERSION}))


if __name__ == "__main__":
    main()
