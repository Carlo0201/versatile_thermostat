"""Runs the per-room status scenarios of the real orchestration manager against the hand-written fake Home Assistant.

No Home Assistant code is executed (``plans/heating-orchestration-2026-11-24/room-status/fake_ha_room_status.py``). The sensor
entity and the entity registry cleanup need real Home Assistant and are not covered. Run with only pytest installed::

    python -m pytest -q --noconftest --import-mode=importlib -p no:cacheprovider tests/test_heating_room_status_manager.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plans" / "heating-orchestration-2026-11-24" / "room-status" / "fake_ha_room_status.py"


def test_the_room_status_scenarios_pass_against_the_fake_home_assistant():
    runner = os.environ.get("VTHERM_OFFLINE_TEST_RUNNER")
    command = [sys.executable, runner, "--script", str(HARNESS)] if runner else [sys.executable, "-I", str(HARNESS)]
    done = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT, check=False, timeout=120)
    assert done.returncode == 0, (done.stdout + done.stderr)[-3000:]
    summary = json.loads(done.stdout.strip().splitlines()[-1])
    assert summary["failed"] == 0 and summary["checks"] >= 30
    assert "PASS observe: not one service call" in done.stdout
    assert "PASS disabled: not one service call" in done.stdout
    assert "PASS same boiler attributes (rooms, excluded_rooms, conditions, cycle)" in done.stdout
    assert "PASS the boiler decision is still published" in done.stdout
