"""Parity and edge-case tests for the pure boiler decision policy.

The baseline fixtures in ``plans/heating-orchestration-2026-11-24/baseline``
were produced by evaluating the current Home Assistant Jinja template. They are
read-only inputs: this test never calls Home Assistant to regenerate them.

The pure module is loaded by file path under a private module name, so the
component package ``custom_components.versatile_thermostat`` (whose
``__init__`` imports Home Assistant) is never executed by this file and no
package is replaced in ``sys.modules``. From the repository root, with only
pytest installed and no special PYTHONPATH (the repository conftest imports
Home Assistant, hence ``--noconftest``)::

    python -m pytest -q --noconftest --import-mode=importlib tests/test_heating_boiler_policy.py

In the complete Home Assistant test environment the file also runs with the
repository conftest, e.g. ``python -m pytest -q tests/test_heating_boiler_policy.py``;
there ``test_component_package_exposes_the_same_source`` checks that the
package import resolves to the same source file.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import functools
import hashlib
import importlib
import importlib.util
import inspect
import itertools
import json
import math
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = REPO_ROOT / "custom_components" / "versatile_thermostat" / "heating_boiler_policy.py"
POLICY_MODULE_NAME = "_vtherm_pure_heating_boiler_policy_under_test"


def _load_pure_policy() -> ModuleType:
    """Load only ``heating_boiler_policy.py``, without the component package."""
    existing = sys.modules.get(POLICY_MODULE_NAME)
    if existing is not None and Path(getattr(existing, "__file__", "")).resolve() == POLICY_PATH:
        return existing
    spec = importlib.util.spec_from_file_location(POLICY_MODULE_NAME, POLICY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their module through sys.modules while the class is built.
    sys.modules[POLICY_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(POLICY_MODULE_NAME, None)
        raise
    return module


policy = _load_pure_policy()

BASELINE_DIR = REPO_ROOT / "plans" / "heating-orchestration-2026-11-24" / "baseline"
DECISION_CASES_FILE = BASELINE_DIR / "boiler-decision-cases.json"
ERRATA_FILE = BASELINE_DIR / "boiler-decision-errata.json"
INDEX_FILE = BASELINE_DIR / "index.json"
DECISION_CASES = json.loads(DECISION_CASES_FILE.read_text(encoding="utf-8"))
CASES = DECISION_CASES["cases"]
RESULTS = DECISION_CASES["results"]
CASE_BY_NAME = {case["name"]: case for case in CASES}
RESULT_BY_NAME = {result["name"]: result for result in RESULTS}

# Only the errata entries listed here are authorized by the M1 review.
AUTHORIZED_ERRATA = {("master_unknown", ("cycle", "r"))}


@functools.cache
def corrected_result_by_name() -> dict[str, dict[str, Any]]:
    """Return a corrected in-memory copy of the saved results.

    The original fixture file is never written. Each errata entry must target
    the exact fixture bytes (SHA-256), be authorized, and find the documented
    original value before the corrected value is applied to the copy.
    """
    errata = json.loads(ERRATA_FILE.read_text(encoding="utf-8"))
    assert errata["schema_version"] == 1
    assert errata["source_file"] == DECISION_CASES_FILE.name
    assert hashlib.sha256(DECISION_CASES_FILE.read_bytes()).hexdigest() == errata["source_sha256"]

    corrected = copy.deepcopy(RESULT_BY_NAME)
    overrides = errata["approved_expected_result_overrides"]
    assert {(entry["case_name"], tuple(entry["result_path"])) for entry in overrides} == AUTHORIZED_ERRATA
    for entry in overrides:
        target = corrected[entry["case_name"]]["result"]
        *parents, leaf = entry["result_path"]
        for key in parents:
            target = target[key]
        assert target[leaf] == entry["original_value"], entry
        target[leaf] = entry["corrected_value"]
    return corrected


def room(slug: str = "cucina", gap: float = 0.5, target: float = 18, pilot_ok: bool = True) -> policy.QualifiedBoilerRoom:
    """Build a normalized room entry."""
    return policy.QualifiedBoilerRoom(slug=slug, target=target, gap=gap, pilot_ok=pilot_ok)


def snapshot_from_raw(raw: dict[str, Any]) -> policy.BoilerSnapshot:
    """Convert one baseline ``raw`` object into the typed snapshot."""
    return policy.BoilerSnapshot(
        ok=raw["ok"],
        master=raw.get("master"),
        count=raw["count"],
        rooms=tuple(room(**entry) for entry in raw.get("rooms", [])),
        manual=raw.get("manual"),
        pilot=raw.get("pilot"),
        pilot_ready=raw.get("pilot_ready"),
        consent=raw.get("consent"),
    )


def snap(
    *,
    master: str | None = "off",
    ok: bool = True,
    count: int = 2,
    rooms: tuple[policy.QualifiedBoilerRoom, ...] | None = None,
    manual: str | None = "off",
    pilot: str | None = "off",
    pilot_ready: bool | None = None,
    consent: bool | None = True,
) -> policy.BoilerSnapshot:
    """Build a snapshot with the baseline defaults used by the decision table."""
    if rooms is None:
        rooms = (room(), room(slug="bagno_blu"))
    return policy.BoilerSnapshot(
        ok=ok,
        master=master,
        count=count,
        rooms=rooms,
        manual=manual,
        pilot=pilot,
        pilot_ready=pilot_ready,
        consent=consent,
    )


def payload(decision: policy.BoilerDecision) -> dict[str, Any]:
    """Serialize the stable part of a decision for exact comparison."""
    single = decision.cycle.single
    return {
        "action": decision.action,
        "reason": decision.reason,
        "deadline": decision.deadline,
        "cycle": {
            "m": decision.cycle.mode,
            "off": decision.cycle.off_since,
            "r": decision.cycle.normal_since,
            "s": [] if single is None else [single.slug, single.target, single.since],
        },
    }


def evaluate(
    *,
    cycle: policy.BoilerCycle | None = None,
    now: float = 2000,
    config: policy.BoilerPolicyConfig | None = None,
    **snapshot_kwargs: Any,
) -> policy.BoilerDecision:
    """Evaluate with a common default context."""
    return policy.evaluate_boiler(snap(**snapshot_kwargs), cycle, now, config)


def test_baseline_fixture_sets_are_complete_and_consistent() -> None:
    """Every saved case has exactly one saved result."""
    assert len(CASES) == 27
    assert set(CASE_BY_NAME) == set(RESULT_BY_NAME)


@pytest.mark.parametrize("name", sorted(RESULT_BY_NAME), ids=sorted(RESULT_BY_NAME))
def test_baseline_decision_matches_saved_result(name: str) -> None:
    """Compare action, reason, deadline and next cycle of all 27 cases, not only the action."""
    case = CASE_BY_NAME[name]
    result = corrected_result_by_name()[name]

    decision = policy.evaluate_boiler(
        snapshot_from_raw(case["raw"]),
        policy.parse_cycle_context(case["cycle"]),
        case["now_ts"],
    )

    assert decision.action == case["expected"]
    assert payload(decision) == result["result"]


def test_errata_targets_the_indexed_fixture_and_leaves_it_untouched() -> None:
    """The errata hash is the one recorded in the baseline index, and the file still has it."""
    index = json.loads(INDEX_FILE.read_text(encoding="utf-8-sig"))
    indexed = {entry["file"]: entry["sha256"] for entry in index["files"]}
    errata = json.loads(ERRATA_FILE.read_text(encoding="utf-8"))
    fixture_hash = hashlib.sha256(DECISION_CASES_FILE.read_bytes()).hexdigest()

    assert indexed["baseline/boiler-decision-cases.json"] == errata["source_sha256"] == fixture_hash
    corrected_result_by_name()
    assert RESULT_BY_NAME["master_unknown"]["result"]["cycle"]["r"] == 1800


def test_master_unknown_errata_is_the_only_authorized_correction() -> None:
    """Why the errata exists: ``master_unknown`` stores ``r=1800``, the template clears ``r``.

    The captured template is normative: ``ready = c.get('r') if normal else
    none`` and ``normal`` requires ``usable``, which requires an OFF/HEAT
    master. With ``master = "unavailable"`` the next cycle therefore has
    ``r = None``; the saved result kept the previous anchor and cannot come
    from that template. The review authorized replacing only this field in an
    in-memory copy; every other field of every case stays as saved.
    """
    case = CASE_BY_NAME["master_unknown"]
    original = RESULT_BY_NAME["master_unknown"]["result"]
    corrected = corrected_result_by_name()["master_unknown"]["result"]

    assert case["raw"]["master"] == "unavailable"
    assert case["cycle"]["r"] == 1800
    assert original["cycle"]["r"] == 1800
    assert corrected["cycle"]["r"] is None
    assert {**corrected, "cycle": {**corrected["cycle"], "r": original["cycle"]["r"]}} == original

    changed = [name for name, entry in corrected_result_by_name().items() if entry != RESULT_BY_NAME[name]]
    assert changed == ["master_unknown"]

    decision = policy.evaluate_boiler(
        snapshot_from_raw(case["raw"]),
        policy.parse_cycle_context(case["cycle"]),
        case["now_ts"],
    )
    assert (decision.action, decision.reason) == ("none", "master_unavailable")
    assert decision.cycle.normal_since is None


@pytest.mark.parametrize(
    ("gap", "count", "off_since", "ready", "now", "consent", "expected_action", "expected_reason", "expected_deadline"),
    [
        (0.299, 2, 1000, 1800, 2000, True, "none", "thermal_wait", None),
        (0.3, 2, 1000, 1880, 2000, True, "heat", "normal", 2000),
        (0.3, 2, 1000, 1881, 2000, True, "none", "normal_wait", 2001),
        (0.3, 2, 1401, 1800, 2000, True, "none", "normal_wait", 2001),
        (0.3, 2, 1400, 1800, 2000, True, "heat", "normal", 2000),
        (0.3, 2, 1950, 1800, 2000, True, "none", "normal_wait", 2550),
        (0.3, 2, 1000, 1990, 2000, True, "none", "normal_wait", 2110),
        (0.3, 2, 1000, 1880, 2000, False, "none", "awaiting_consent", None),
        (0.3, 2, 1000, 1880, 2000, None, "none", "awaiting_consent", None),
        (0.3, 0, 1000, None, 2000, True, "none", "no_demand", None),
        (0.3, 1, 1000, None, 2000, True, "none", "single_wait", 2900),
    ],
)
def test_normal_path_boundaries(
    gap: float,
    count: int,
    off_since: float,
    ready: float | None,
    now: float,
    consent: bool | None,
    expected_action: str,
    expected_reason: str,
    expected_deadline: float | None,
) -> None:
    """Gap, stability and cooldown boundaries use the exact comparison of the baseline."""
    rooms = (room(gap=gap), room(slug="bagno_blu", gap=gap)) if count >= 2 else (room(gap=gap),) if count == 1 else ()
    cycle = policy.BoilerCycle(mode="off", off_since=off_since, normal_since=ready)
    decision = evaluate(cycle=cycle, now=now, count=count, rooms=rooms, consent=consent)

    assert decision.action == expected_action
    assert decision.reason == expected_reason
    assert decision.deadline == expected_deadline


def test_two_valves_in_the_same_room_use_the_normal_path() -> None:
    """``count`` is the number of valves, not the number of rooms."""
    decision = evaluate(
        cycle=policy.BoilerCycle(mode="off", off_since=1000, normal_since=1880),
        now=2000,
        count=2,
        rooms=(room(gap=0.5),),
    )

    assert decision.action == "heat"
    assert decision.reason == "normal"


@pytest.mark.parametrize(
    (
        "anchor_slug",
        "anchor_target",
        "slug",
        "target",
        "gap",
        "count",
        "since",
        "pilot",
        "pilot_ready",
        "pilot_ok",
        "consent",
        "expected_action",
        "expected_reason",
        "expected_deadline",
        "expected_single",
    ),
    [
        ("diego", 18, "diego", 18, 0.3, 1, 1101, "off", None, True, True, "none", "single_wait", 2001, ["diego", 18, 1101]),
        ("diego", 18, "diego", 18, 0.3, 1, 1100, "off", None, True, False, "heat", "single", 2000, ["diego", 18, 1100]),
        ("diego", 18, "diego", 19, 0.3, 1, 1100, "off", None, True, True, "none", "single_wait", 2900, ["diego", 19, 2000]),
        ("diego", 18, "carlo", 18, 0.3, 1, 1100, "off", None, True, True, "none", "single_wait", 2900, ["carlo", 18, 2000]),
        ("diego", 18, "diego", 18, 0.2, 1, 1100, "off", None, True, True, "none", "thermal_wait", None, []),
        ("diego", 18, "diego", 18, 0.3, 0, 1100, "off", None, True, True, "none", "no_demand", None, []),
        ("diego", 18, "diego", 18, 0.3, 1, 1100, "on", True, False, True, "none", "thermal_wait", None, []),
        ("diego", 18, "diego", 18, 0.3, 1, 1100, "on", False, True, True, "none", "data_missing", None, []),
        ("diego", 18, "diego", 18, 0.3, 1, -5, "off", None, True, True, "none", "single_wait", 2900, ["diego", 18, 2000]),
        ("diego", 18, "diego", 18, 0.3, 1, 5000, "off", None, True, True, "none", "single_wait", 2900, ["diego", 18, 2000]),
    ],
)
def test_single_path_boundaries(
    anchor_slug: str,
    anchor_target: float,
    slug: str,
    target: float,
    gap: float,
    count: int,
    since: float,
    pilot: str,
    pilot_ready: bool | None,
    pilot_ok: bool,
    consent: bool | None,
    expected_action: str,
    expected_reason: str,
    expected_deadline: float | None,
    expected_single: list[Any],
) -> None:
    """The single path keeps room and target identity and restarts invalid anchors."""
    cycle = policy.BoilerCycle(
        mode="off",
        off_since=1000,
        single=policy.BoilerSingleWait(slug=anchor_slug, target=anchor_target, since=since),
    )
    decision = evaluate(
        cycle=cycle,
        now=2000,
        count=count,
        rooms=(room(slug=slug, target=target, gap=gap, pilot_ok=pilot_ok),),
        pilot=pilot,
        pilot_ready=pilot_ready,
        consent=consent,
    )

    assert decision.action == expected_action
    assert decision.reason == expected_reason
    assert decision.deadline == expected_deadline
    single = decision.cycle.single
    assert ([] if single is None else [single.slug, single.target, single.since]) == expected_single


def test_single_path_rejects_two_qualified_rooms() -> None:
    """One valve but two qualified rooms is inconsistent and must not start."""
    decision = evaluate(
        cycle=policy.BoilerCycle(mode="off", off_since=1000, single=policy.BoilerSingleWait("diego", 18, 1100)),
        now=2000,
        count=1,
        rooms=(room("diego", gap=0.3), room("carlo", gap=0.5)),
    )

    assert decision.action == "none"
    assert decision.reason == "thermal_wait"
    assert decision.cycle.single is None


def test_pilot_veto_is_ignored_by_the_normal_path() -> None:
    """Baseline quirk preserved: ``normal`` only checks pilot readiness, not ``pilot_ok``.

    This is documented as an anomaly for the M2 review: the single path vetoes a
    non-pilot room, the normal path currently does not.
    """
    decision = evaluate(
        cycle=policy.BoilerCycle(mode="off", off_since=1000, normal_since=1880),
        now=2000,
        count=2,
        rooms=(room("carlo", pilot_ok=False), room("bagno_blu", pilot_ok=False)),
        pilot="on",
        pilot_ready=True,
    )

    assert decision.action == "heat"
    assert decision.reason == "normal"


def test_pilot_on_without_readiness_does_not_authorize_automatic_start() -> None:
    """A pilot ON without its readiness flag is missing data, not approval."""
    decision = evaluate(
        cycle=policy.BoilerCycle(mode="off", off_since=1000, normal_since=1880),
        now=2000,
        count=2,
        pilot="on",
        pilot_ready=None,
    )

    assert decision.action == "none"
    assert decision.reason == "data_missing"
    assert decision.cycle.normal_since is None


@pytest.mark.parametrize(
    ("master", "ok", "count", "manual", "consent", "expected_action", "expected_reason"),
    [
        ("off", True, 1, "on", False, "heat", "manual"),
        ("off", True, 2, "on", False, "heat", "manual"),
        ("off", True, 0, "on", True, "none", "no_demand"),
        ("off", False, 2, "on", True, "none", "data_or_central"),
        ("off", True, 2, "off", True, "none", "normal_wait"),
        ("off", True, 2, "unknown", True, "none", "data_missing"),
        ("off", True, 2, None, True, "none", "data_missing"),
        ("heat", True, 0, "on", True, "off", "no_valid_radiator"),
        ("heat", True, 1, "on", True, "none", "maintain"),
        ("unavailable", True, 2, "on", True, "none", "master_unavailable"),
    ],
)
def test_manual_and_missing_values(
    master: str,
    ok: bool,
    count: int,
    manual: str | None,
    consent: bool,
    expected_action: str,
    expected_reason: str,
) -> None:
    """Manual is immediate only with usable inputs; missing values never approve."""
    rooms = (room(gap=0.1),) if count == 1 else (room(), room("bagno_blu")) if count >= 2 else ()
    decision = evaluate(
        cycle=policy.BoilerCycle(mode="off", off_since=1000, normal_since=1880),
        now=1990,
        master=master,
        ok=ok,
        count=count,
        rooms=rooms,
        manual=manual,
        consent=consent,
    )

    assert decision.action == expected_action
    assert decision.reason == expected_reason
    if expected_reason == "manual":
        assert decision.deadline is None


@pytest.mark.parametrize(
    ("master", "ok", "count", "rooms", "consent", "normal_since", "expected_action", "expected_reason", "expected_normal"),
    [
        ("heat", True, 1, (room(gap=0.1),), False, None, "none", "maintain", None),
        ("heat", True, 2, (room(), room("bagno_blu")), False, 1800, "none", "maintain", 1800),
        ("heat", True, 0, (), True, 1800, "off", "no_valid_radiator", None),
        ("heat", False, 2, (room(), room("bagno_blu")), True, 1800, "off", "no_valid_radiator", None),
    ],
)
def test_maintenance_does_not_reapply_start_conditions(
    master: str,
    ok: bool,
    count: int,
    rooms: tuple[policy.QualifiedBoilerRoom, ...],
    consent: bool,
    normal_since: float | None,
    expected_action: str,
    expected_reason: str,
    expected_normal: float | None,
) -> None:
    """Maintenance keeps HEAT with one valve; zero/invalid inputs force OFF."""
    decision = evaluate(
        cycle=policy.BoilerCycle(mode="heat", off_since=1000, normal_since=normal_since),
        now=2000,
        master=master,
        ok=ok,
        count=count,
        rooms=rooms,
        consent=consent,
    )

    assert decision.action == expected_action
    assert decision.reason == expected_reason
    assert decision.deadline is None
    assert decision.cycle.mode == "heat"
    assert decision.cycle.normal_since == expected_normal


@pytest.mark.parametrize("master", ["auto", "cool", "unavailable", "unknown", None])
def test_master_unavailable_never_commands(master: str | None) -> None:
    """An unknown or non OFF/HEAT master is not an OFF confirmation."""
    cycle = policy.BoilerCycle(mode="off", off_since=1000, normal_since=1800, single=policy.BoilerSingleWait("diego", 18, 1100))
    decision = evaluate(cycle=cycle, now=2000, master=master)

    assert decision.action == "none"
    assert decision.reason == "master_unavailable"
    assert decision.deadline is None
    assert decision.cycle.mode == "off"
    assert decision.cycle.off_since == 1000
    assert decision.cycle.normal_since is None
    assert decision.cycle.single is None


@pytest.mark.parametrize("value", [None, [], "not-a-cycle", 42, (), ["diego", 18, 1100]])
def test_non_mapping_context_is_empty(value: Any) -> None:
    """Missing or malformed contexts degrade to an empty cycle."""
    assert policy.parse_cycle_context(value) == policy.EMPTY_CYCLE


def test_context_with_invalid_entries_is_partially_ignored() -> None:
    """Invalid stored fields are treated as absent, never as zero or OFF."""
    cycle = policy.parse_cycle_context(
        {
            "m": 5,
            "off": True,
            "r": "not-a-number",
            "s": "diego",
        }
    )
    assert cycle.mode is None
    assert cycle.off_since is None
    assert cycle.normal_since is None
    assert cycle.single is None


def test_numeric_strings_are_accepted_like_the_jinja_float_filter() -> None:
    """Stored JSON numbers written as strings remain readable."""
    cycle = policy.parse_cycle_context({"m": "off", "off": "1400", "r": "1800", "s": ["diego", 18, "1100"]})

    assert cycle.mode == "off"
    assert cycle.off_since == 1400
    assert cycle.normal_since == 1800
    assert cycle.single == policy.BoilerSingleWait("diego", 18, 1100)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", float("nan"), float("inf")])
def test_non_finite_anchors_are_treated_as_missing(value: Any) -> None:
    """Non-finite anchors must not leak NaN or Infinity into the decision."""
    cycle = policy.parse_cycle_context({"m": "off", "off": value, "r": value})

    assert cycle.off_since is None
    assert cycle.normal_since is None
    assert math.isfinite(policy.evaluate_boiler(snap(), cycle, 2000).evaluated_at)


def test_single_anchor_with_non_numeric_target_is_dropped() -> None:
    """A string target did not match in the baseline, so it must restart."""
    cycle = policy.parse_cycle_context({"m": "off", "off": 1000, "s": ["diego", "18", 1100]})

    assert cycle.single is None


def test_truncated_off_anchor_reproduces_the_one_second_effect() -> None:
    """The stored OFF anchor is truncated, while the current deadline uses the raw value."""
    cycle = policy.parse_cycle_context({"m": "off", "off": 1400.9, "r": 1800})
    first = evaluate(cycle=cycle, now=2000)

    assert first.action == "none"
    assert first.reason == "normal_wait"
    assert first.deadline == pytest.approx(2000.9)
    assert first.cycle.off_since == 1400

    second = evaluate(cycle=first.cycle, now=2000)
    assert second.action == "heat"
    assert second.reason == "normal"


def test_transition_to_off_starts_a_new_cooldown() -> None:
    """A previous HEAT anchor does not keep an old OFF cooldown while confirming OFF."""
    cycle = policy.BoilerCycle(mode="heat", off_since=500)
    decision = evaluate(cycle=cycle, now=2000)

    assert decision.reason == "normal_wait"
    assert decision.cycle.off_since == 2000
    assert decision.deadline == 2600


def test_timestamp_limits_are_deterministic() -> None:
    """Epoch zero and a very large finite timestamp remain usable."""
    at_zero = evaluate(cycle=None, now=0)
    assert at_zero.cycle.off_since == 0
    assert at_zero.cycle.normal_since == 0
    assert at_zero.deadline == 600

    huge = 2**53
    at_huge = evaluate(cycle=None, now=huge)
    assert at_huge.cycle.off_since == huge
    assert at_huge.deadline == huge + 600


def test_duplicate_evaluation_does_not_move_the_anchors() -> None:
    """Polling the same instant is idempotent for anchors and deadline."""
    cycle = policy.BoilerCycle(mode="off", off_since=1000, normal_since=1900)
    first = evaluate(cycle=cycle, now=2005)
    second = evaluate(cycle=first.cycle, now=2005)

    assert first.reason == "normal_wait"
    assert second.reason == "normal_wait"
    assert second.deadline == first.deadline
    assert second.cycle == first.cycle


def test_losing_and_regaining_requirements_restarts_normal_stability() -> None:
    """Losing demand clears ``r``; regaining it starts a new stability anchor."""
    cycle = policy.BoilerCycle(mode="off", off_since=1000, normal_since=1800)
    lost = evaluate(cycle=cycle, now=2000, count=2, rooms=())
    assert lost.reason == "thermal_wait"
    assert lost.cycle.normal_since is None

    regained = evaluate(cycle=lost.cycle, now=2100)
    assert regained.reason == "normal_wait"
    assert regained.cycle.normal_since == 2100
    assert regained.deadline == 2220


def test_normal_stability_survives_a_change_of_room() -> None:
    """The normal path depends on validity, not on room identity."""
    cycle = policy.BoilerCycle(mode="off", off_since=1000, normal_since=1880)
    decision = evaluate(
        cycle=cycle,
        now=2000,
        rooms=(room("carlo"), room("ingresso")),
    )

    assert decision.action == "heat"
    assert decision.reason == "normal"
    assert decision.cycle.normal_since == 1880


def test_heat_proposal_does_not_confirm_the_master_or_start_on_time() -> None:
    """A proposed HEAT leaves the observed mode OFF and is re-proposed until confirmed."""
    cycle = policy.BoilerCycle(mode="off", off_since=1000, normal_since=1880)
    first = evaluate(cycle=cycle, now=2000)
    assert first.action == "heat"
    assert first.cycle.mode == "off"

    still_off = evaluate(cycle=first.cycle, now=2000)
    assert still_off.action == "heat"
    assert still_off.reason == "normal"
    assert still_off.cycle.mode == "off"

    no_radiator = evaluate(cycle=still_off.cycle, now=2000, count=0, rooms=())
    assert no_radiator.action == "none"
    assert no_radiator.reason == "no_demand"


def test_full_sequence_off_request_expiry_heat_single_zero_off_restart() -> None:
    """Walk a complete natural cycle with explicit anchors."""
    cycle = None

    initial = evaluate(cycle=cycle, now=1000)
    assert initial.action == "none"
    assert initial.reason == "normal_wait"
    assert initial.cycle == policy.BoilerCycle(mode="off", off_since=1000, normal_since=1000)
    assert initial.deadline == 1600

    waiting = evaluate(cycle=initial.cycle, now=1599)
    assert waiting.action == "none"
    assert waiting.deadline == 1600

    started = evaluate(cycle=waiting.cycle, now=1600)
    assert started.action == "heat"
    assert started.reason == "normal"
    assert started.cycle.mode == "off"

    confirmed = evaluate(cycle=started.cycle, now=1601, master="heat")
    assert confirmed.action == "none"
    assert confirmed.reason == "maintain"
    assert confirmed.cycle.mode == "heat"
    assert confirmed.cycle.off_since == 1000
    assert confirmed.cycle.normal_since == 1000

    one_valve = evaluate(
        cycle=confirmed.cycle,
        now=1700,
        master="heat",
        count=1,
        rooms=(room(gap=0.1),),
    )
    assert one_valve.reason == "maintain"
    assert one_valve.cycle.normal_since is None
    assert one_valve.cycle.single is None

    zero_valves = evaluate(cycle=one_valve.cycle, now=1800, master="heat", count=0, rooms=())
    assert zero_valves.action == "off"
    assert zero_valves.reason == "no_valid_radiator"
    assert zero_valves.cycle.mode == "heat"

    off_confirmed = evaluate(cycle=zero_valves.cycle, now=1801, count=0, rooms=())
    assert off_confirmed.action == "none"
    assert off_confirmed.reason == "no_demand"
    assert off_confirmed.cycle.off_since == 1801

    new_request = evaluate(cycle=off_confirmed.cycle, now=1850)
    assert new_request.reason == "normal_wait"
    assert new_request.cycle.normal_since == 1850
    assert new_request.deadline == 2401

    still_cooling = evaluate(cycle=new_request.cycle, now=2400)
    assert still_cooling.action == "none"
    assert still_cooling.deadline == 2401

    restarted = evaluate(cycle=still_cooling.cycle, now=2401)
    assert restarted.action == "heat"
    assert restarted.reason == "normal"


def test_inputs_are_not_mutated_and_results_are_new_objects() -> None:
    """The engine is pure and the models are frozen."""
    cycle = policy.BoilerCycle(mode="off", off_since=1000, normal_since=1800)
    snapshots = snap()
    config = policy.BoilerPolicyConfig()
    cycle_before = dataclasses.replace(cycle)
    snapshot_before = dataclasses.replace(snapshots)
    config_before = dataclasses.replace(config)

    decision = policy.evaluate_boiler(snapshots, cycle, 2000, config)

    assert snapshots == snapshot_before
    assert cycle == cycle_before
    assert config == config_before
    assert decision.cycle is not cycle
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshots.count = 5  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        cycle.off_since = 0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"thermal_gap_c": -0.1}, ValueError),
        ({"thermal_gap_c": "0.3"}, TypeError),
        ({"thermal_gap_c": float("nan")}, ValueError),
        ({"normal_valve_count": 0}, ValueError),
        ({"normal_valve_count": 1}, ValueError),
        ({"normal_valve_count": True}, TypeError),
        ({"normal_stability_s": -1}, ValueError),
        ({"single_stability_s": float("inf")}, ValueError),
        ({"off_cooldown_s": float("nan")}, ValueError),
    ],
)
def test_invalid_config_is_rejected(kwargs: dict[str, Any], error: type[Exception]) -> None:
    """Invalid parameters fail at construction instead of producing odd decisions."""
    with pytest.raises(error):
        policy.BoilerPolicyConfig(**kwargs)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"count": -1}, ValueError),
        ({"count": True}, TypeError),
        ({"count": 2.0}, TypeError),
        ({"ok": 1}, TypeError),
        ({"master": 5}, TypeError),
        ({"rooms": "cucina"}, TypeError),
        ({"rooms": ({},)}, TypeError),
        ({"pilot_ready": "yes"}, TypeError),
        ({"consent": 1}, TypeError),
    ],
)
def test_invalid_snapshot_is_rejected(kwargs: dict[str, Any], error: type[Exception]) -> None:
    """A collector bug must be visible, not silently converted into a state."""
    base = {"ok": True, "master": "off", "count": 2, "rooms": (room(), room("bagno_blu"))}
    base.update(kwargs)
    with pytest.raises(error):
        policy.BoilerSnapshot(**base)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "2000"])
def test_invalid_now_is_rejected(value: Any) -> None:
    """The explicit clock must be a finite number, not a boolean or string."""
    with pytest.raises((TypeError, ValueError)):
        policy.evaluate_boiler(snap(), None, value)


def test_evaluate_rejects_wrong_model_types() -> None:
    """The public function does not duck-type arbitrary mappings."""
    with pytest.raises(TypeError):
        policy.evaluate_boiler({}, None, 2000)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        policy.evaluate_boiler(snap(), {}, 2000)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        policy.evaluate_boiler(snap(), None, 2000, {})  # type: ignore[arg-type]


def test_config_parameters_are_applied() -> None:
    """The validated parameters really drive the policy, not hard-coded values."""
    gap_only = evaluate(
        config=policy.BoilerPolicyConfig(thermal_gap_c=0.9),
        cycle=policy.BoilerCycle(mode="off", off_since=1000),
        now=2000,
    )
    assert gap_only.reason == "thermal_wait"

    three_valves = evaluate(
        config=policy.BoilerPolicyConfig(normal_valve_count=3),
        cycle=policy.BoilerCycle(mode="off", off_since=1000, normal_since=1800),
        now=2000,
    )
    assert three_valves.reason == "thermal_wait"

    short_stability = evaluate(
        config=policy.BoilerPolicyConfig(normal_stability_s=60),
        cycle=policy.BoilerCycle(mode="off", off_since=1000, normal_since=1900),
        now=1960,
    )
    assert short_stability.action == "heat"

    short_single = evaluate(
        config=policy.BoilerPolicyConfig(single_stability_s=10),
        cycle=policy.BoilerCycle(mode="off", off_since=1000, single=policy.BoilerSingleWait("diego", 18, 1990)),
        now=2000,
        count=1,
        rooms=(room("diego", gap=0.3),),
    )
    assert short_single.action == "heat"

    no_cooldown = evaluate(
        config=policy.BoilerPolicyConfig(off_cooldown_s=0),
        cycle=policy.BoilerCycle(mode="off", off_since=1999, normal_since=1700),
        now=2000,
    )
    assert no_cooldown.action == "heat"


def test_diagnostic_conditions_are_structured_and_json_safe() -> None:
    """The M1 result already exposes the M2 condition shape without driving policy."""
    decision = evaluate(
        cycle=policy.BoilerCycle(mode="off", off_since=1000, normal_since=1880),
        now=2000,
        consent=False,
    )

    conditions = {condition.id: condition for condition in decision.conditions}
    assert set(conditions) == {
        "master",
        "inputs",
        "valve_count",
        "thermal_gap",
        "pilot",
        "single_room_pilot",
        "manual_request",
        "path",
        "stability",
        "cooldown",
        "consent",
    }
    assert conditions["consent"].status == "fail"
    assert conditions["stability"].status == "pass"
    assert conditions["path"].actual == "normal_wait"

    serialized = json.dumps(dataclasses.asdict(decision), allow_nan=False)
    assert "NaN" not in serialized and "Infinity" not in serialized


def test_single_path_diagnostic_marks_consent_not_applicable() -> None:
    """Consent is not part of the baseline single path; diagnostics say so."""
    decision = evaluate(
        cycle=policy.BoilerCycle(mode="off", off_since=1000, single=policy.BoilerSingleWait("diego", 18, 1100)),
        now=2000,
        count=1,
        rooms=(room("diego", gap=0.3),),
        consent=False,
    )

    conditions = {condition.id: condition for condition in decision.conditions}
    assert decision.action == "heat"
    assert conditions["consent"].status == "na"


BLOCKING_STATUSES = {"fail", "wait", "unknown"}
START_CONDITIONS = ("thermal_gap", "pilot", "single_room_pilot", "stability", "cooldown", "consent")


def statuses(decision: policy.BoilerDecision) -> dict[str, str]:
    """Map condition ids to statuses; ids must be unique."""
    result = {condition.id: condition.status for condition in decision.conditions}
    assert len(result) == len(decision.conditions)
    return result


def blockers(decision: policy.BoilerDecision) -> set[str]:
    """Return the ids of the conditions that a checklist would show as blocking."""
    return {condition_id for condition_id, status in statuses(decision).items() if status in BLOCKING_STATUSES}


@pytest.mark.parametrize(
    ("pilot", "pilot_ready", "pilot_ok"),
    [("on", False, False), ("on", None, False), ("on", False, True), (None, None, False), ("unknown", None, False)],
)
def test_manual_start_diagnostics_ignore_gap_and_pilot(pilot: str | None, pilot_ready: bool | None, pilot_ok: bool) -> None:
    """R1: manual start with insufficient gap and denied/unknown pilot shows no false blocker."""
    decision = evaluate(
        cycle=policy.BoilerCycle(mode="off", off_since=1990),
        now=2000,
        count=1,
        rooms=(room("diego", gap=0.1, pilot_ok=pilot_ok),),
        manual="on",
        pilot=pilot,
        pilot_ready=pilot_ready,
        consent=False,
    )

    assert (decision.action, decision.reason) == ("heat", "manual")
    status = statuses(decision)
    assert {condition_id: status[condition_id] for condition_id in START_CONDITIONS} == dict.fromkeys(START_CONDITIONS, "na")
    assert status["master"] == status["inputs"] == status["valve_count"] == "pass"
    assert status["manual_request"] == status["path"] == "pass"
    assert blockers(decision) == set()


@pytest.mark.parametrize(
    ("manual", "pilot", "pilot_ready"),
    [("off", "on", False), ("off", "on", None), ("off", None, None), ("on", "unknown", None), (None, "on", False)],
)
def test_maintenance_diagnostics_ignore_pilot_and_start_requirements(manual: str | None, pilot: str | None, pilot_ready: bool | None) -> None:
    """R1: maintenance with a denied/unknown pilot keeps only inputs and valve count applicable."""
    decision = evaluate(
        cycle=policy.BoilerCycle(mode="heat", off_since=1000),
        now=2000,
        master="heat",
        count=1,
        rooms=(room("diego", gap=0.1, pilot_ok=False),),
        manual=manual,
        pilot=pilot,
        pilot_ready=pilot_ready,
        consent=False,
    )

    assert (decision.action, decision.reason) == ("none", "maintain")
    status = statuses(decision)
    assert {condition_id: status[condition_id] for condition_id in START_CONDITIONS} == dict.fromkeys(START_CONDITIONS, "na")
    assert status["manual_request"] == status["path"] == "na"
    assert blockers(decision) == set()


@pytest.mark.parametrize(("ok", "count", "blocker"), [(True, 0, "valve_count"), (False, 1, "inputs")])
def test_maintenance_diagnostics_keep_inputs_and_valve_count(ok: bool, count: int, blocker: str) -> None:
    """R1: the conditions that force OFF in maintenance stay applicable and are the only blockers."""
    decision = evaluate(
        cycle=policy.BoilerCycle(mode="heat", off_since=1000),
        now=2000,
        master="heat",
        ok=ok,
        count=count,
        rooms=(room("diego", gap=0.1),) if count else (),
        pilot="on",
        pilot_ready=False,
    )

    assert (decision.action, decision.reason) == ("off", "no_valid_radiator")
    assert blockers(decision) == {blocker}
    assert statuses(decision)["pilot"] == "na"


def test_single_room_pilot_veto_is_distinct_from_global_readiness() -> None:
    """R1: pilot ready, but the single room is not pilot-qualified: the room veto is the blocker."""
    decision = evaluate(
        cycle=policy.BoilerCycle(mode="off", off_since=1000, single=policy.BoilerSingleWait("diego", 18, 1100)),
        now=2000,
        count=1,
        rooms=(room("diego", gap=0.5, pilot_ok=False),),
        pilot="on",
        pilot_ready=True,
    )

    assert (decision.action, decision.reason, decision.deadline) == ("none", "thermal_wait", None)
    assert decision.cycle.single is None
    conditions = {condition.id: condition for condition in decision.conditions}
    assert conditions["pilot"].status == "pass"
    assert conditions["thermal_gap"].status == "pass"
    assert conditions["single_room_pilot"].status == "fail"
    assert conditions["single_room_pilot"].reason_code == policy.CONDITION_REASON_SINGLE_ROOM_PILOT_VETO
    assert conditions["single_room_pilot"].actual is False
    assert blockers(decision) == {"single_room_pilot", "path"}

    qualified = evaluate(
        cycle=policy.BoilerCycle(mode="off", off_since=1000, single=policy.BoilerSingleWait("diego", 18, 1100)),
        now=2000,
        count=1,
        rooms=(room("diego", gap=0.5, pilot_ok=True),),
        pilot="on",
        pilot_ready=True,
    )
    assert (qualified.action, qualified.reason) == ("heat", "single")
    assert statuses(qualified)["single_room_pilot"] == "pass"
    assert blockers(qualified) == set()


@pytest.mark.parametrize(("pilot", "pilot_ready"), [("on", True), ("off", None)])
def test_normal_path_diagnostics_do_not_require_room_pilot_qualification(pilot: str, pilot_ready: bool | None) -> None:
    """R1: the normal path starts without ``pilot_ok`` and the diagnostic does not invent that veto."""
    decision = evaluate(
        cycle=policy.BoilerCycle(mode="off", off_since=1000, normal_since=1880),
        now=2000,
        count=2,
        rooms=(room("carlo", pilot_ok=False), room("bagno_blu", pilot_ok=False)),
        pilot=pilot,
        pilot_ready=pilot_ready,
    )

    assert (decision.action, decision.reason) == ("heat", "normal")
    status = statuses(decision)
    assert status["single_room_pilot"] == "na"
    assert status["pilot"] == ("pass" if pilot == "on" else "na")
    assert blockers(decision) == set()


def test_master_unavailable_diagnostics_expose_only_the_master() -> None:
    """With a master other than OFF/HEAT no start requirement applies; the master is the blocker."""
    decision = evaluate(
        cycle=policy.BoilerCycle(mode="off", off_since=1000, normal_since=1800),
        now=2000,
        master="unavailable",
        rooms=(room(gap=0.1), room("bagno_blu", gap=0.1)),
        pilot="on",
        pilot_ready=False,
    )

    assert (decision.action, decision.reason) == ("none", "master_unavailable")
    assert blockers(decision) == {"master", "path"}
    assert all(statuses(decision)[condition_id] == "na" for condition_id in START_CONDITIONS)


PROGRESSING_REASONS = {"manual", "normal", "single", "maintain"}


def test_diagnostics_agree_with_the_decision_on_a_deterministic_grid() -> None:
    """R1 invariant, checked without changing the policy.

    A decision that starts or maintains heating has no blocking condition; any
    other decision exposes at least one. Requirements of the manual and
    maintenance routes never include the automatic start conditions.
    """
    cycles = (
        None,
        policy.BoilerCycle(mode="off", off_since=1000, normal_since=1800, single=policy.BoilerSingleWait("r0", 18, 1100)),
        policy.BoilerCycle(mode="off", off_since=1950, normal_since=1990, single=policy.BoilerSingleWait("r0", 18, 1990)),
        policy.BoilerCycle(mode="heat", off_since=1000, normal_since=1800),
    )
    checked = 0
    for master, ok, count, gap, pilot_ok, manual, pilot, pilot_ready, consent, cycle in itertools.product(
        ("off", "heat", "unavailable"),
        (True, False),
        (0, 1, 2),
        (0.1, 0.5),
        (True, False),
        ("on", "off", None),
        ("on", "off", None),
        (True, False, None),
        (True, False, None),
        cycles,
    ):
        rooms = tuple(room(f"r{index}", gap=gap, pilot_ok=pilot_ok) for index in range(count))
        decision = evaluate(
            cycle=cycle,
            now=2000,
            master=master,
            ok=ok,
            count=count,
            rooms=rooms,
            manual=manual,
            pilot=pilot,
            pilot_ready=pilot_ready,
            consent=consent,
        )
        context = (master, ok, count, gap, pilot_ok, manual, pilot, pilot_ready, consent, cycle, decision.reason)
        if decision.reason in PROGRESSING_REASONS:
            assert blockers(decision) == set(), context
        else:
            assert blockers(decision), context
        if master == "heat" or (master == "off" and manual == "on"):
            assert all(statuses(decision)[condition_id] == "na" for condition_id in START_CONDITIONS), context
        checked += 1

    assert checked == 3 * 2 * 3 * 2 * 2 * 3 * 3 * 3 * 3 * 4


def test_engine_has_no_home_assistant_or_io_imports() -> None:
    """M1 must stay a pure module: no HA, I/O, asyncio, clock or services."""
    tree = ast.parse(inspect.getsource(policy))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported <= {"__future__", "math", "dataclasses", "typing"}


def test_pure_module_loads_in_an_isolated_interpreter_without_the_component_package() -> None:
    """R2: a fresh ``python -I`` (no PYTHONPATH, no user site) loads and evaluates the module alone."""
    code = "\n".join(
        [
            "import importlib.util",
            "import sys",
            "spec = importlib.util.spec_from_file_location('isolated_boiler_policy', sys.argv[1])",
            "module = importlib.util.module_from_spec(spec)",
            "sys.modules[spec.name] = module",
            "spec.loader.exec_module(module)",
            "leaked = sorted(name for name in sys.modules if name.split('.')[0] in {'homeassistant', 'custom_components', 'vtherm_api'})",
            "assert not leaked, leaked",
            "print(module.evaluate_boiler(module.BoilerSnapshot(ok=True, master='off', count=0), None, 0).reason)",
        ]
    )
    completed = subprocess.run([sys.executable, "-I", "-c", code, str(POLICY_PATH)], capture_output=True, text=True, timeout=120, check=False)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "no_demand"


def test_policy_under_test_is_loaded_from_the_component_source() -> None:
    """R2: the isolated loader targets the real component file and does not shadow the package."""
    assert Path(policy.__file__).resolve() == POLICY_PATH
    assert policy.__name__ == POLICY_MODULE_NAME
    package = sys.modules.get("custom_components.versatile_thermostat")
    assert package is None or getattr(package, "heating_boiler_policy", None) is not policy


def test_component_package_exposes_the_same_source() -> None:
    """Complete HA environment only: the regular package import resolves to the same file."""
    pytest.importorskip("homeassistant")
    module = importlib.import_module("custom_components.versatile_thermostat.heating_boiler_policy")

    assert Path(module.__file__).resolve() == POLICY_PATH
