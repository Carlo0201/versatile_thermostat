"""Runs the scenarios of the real PDC manager against the hand-written fake Home Assistant.

No Home Assistant code is executed (``plans/heating-orchestration-2026-11-24/pdc/fake_ha_pdc.py`` replaces the few APIs the
manager uses). It proves that in ``observe`` not one service is called, not one inhibition is touched and nothing is
written to the store, that ``control`` is refused with an error state, and that stop/reload leave nothing behind.
Run with only pytest installed::

    python -m pytest -q --noconftest --import-mode=importlib tests/test_heating_pdc_fake_ha.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plans" / "heating-orchestration-2026-11-24" / "pdc" / "fake_ha_pdc.py"


def test_the_manager_scenarios_pass_against_the_fake_home_assistant():
    done = subprocess.run([sys.executable, "-I", str(HARNESS)], capture_output=True, text=True, cwd=REPO_ROOT, check=False)
    assert done.returncode == 0, (done.stdout + done.stderr)[-3000:]
    summary = json.loads(done.stdout.strip().splitlines()[-1])
    assert summary["failed"] == 0 and summary["checks"] >= 35
    assert "PASS observe: not one service call" in done.stdout
    assert "PASS observe: not one inhibition call (not even a read of the start state)" in done.stdout
    assert "PASS observe: nothing written to the store" in done.stdout
    assert "PASS control: refused (error state, not running, no runtime)" in done.stdout
