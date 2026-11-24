"""Parity and edge-case tests for the pure boiler decision policy.

The baseline fixtures in ``plans/heating-orchestration-2026-11-24/baseline``
were produced by evaluating the current Home Assistant Jinja template. They are
read-only inputs: this test never calls Home Assistant to regenerate them.

Run the pure file locally with a minimal environment (the repository conftest
imports Home Assistant)::

    python -m pytest -q --noconftest tests/test_heating_boiler_policy.py
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import math
from pathlib import Path
from typing import Any

import pytest

from custom_components.versatile_thermostat import heating_boiler_policy as policy

BASELINE_DIR = Path(__file__).resolve().parents[1] / "plans" / "heating-orchestration-2026-11-24" / "baseline"
DECISION_CASES = json.loads((BASELINE_DIR / "boiler-decision-cases.json").read_text(encoding="utf-8"))
CASES = DECISION_CASES["cases"]
RESULTS = DECISION_CASES["results"]
CASE_BY_NAME = {case["name"]: case for case in CASES}
RESULT_BY_NAME = {result["name"]: result for result in RESULTS}


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


@pytest.mark.parametrize("name", sorted(set(RESULT_BY_NAME) - {"master_unknown"}), ids=sorted(set(RESULT_BY_NAME) - {"master_unknown"}))
def test_baseline_decision_matches_saved_result(name: str) -> None:
    """Compare action, reason, deadline and next cycle, not only the action."""
    case = CASE_BY_NAME[name]
    result = RESULT_BY_NAME[name]

    decision = policy.evaluate_boiler(
        snapshot_from_raw(case["raw"]),
        policy.parse_cycle_context(case["cycle"]),
        case["now_ts"],
    )

    assert decision.action == case["expected"]
    assert payload(decision) == result["result"]


def test_master_unknown_saved_result_disagrees_with_the_baseline_template() -> None:
    """``master_unknown`` stores ``r=1800`` but the current template clears ``r``.

    The complete automation configuration is normative: ``ready = c.get('r') if
    normal else none`` and ``normal`` requires an OFF/HEAT master. The saved
    result in the fixture keeps the previous stability anchor, so it cannot come
    from the template captured in the same baseline. The fixture is left
    untouched; the divergence is reported for the Codex review.
    """
    case = CASE_BY_NAME["master_unknown"]
    saved = RESULT_BY_NAME["master_unknown"]["result"]

    decision = policy.evaluate_boiler(
        snapshot_from_raw(case["raw"]),
        policy.parse_cycle_context(case["cycle"]),
        case["now_ts"],
    )

    assert saved["cycle"]["r"] == 1800
    assert decision.action == saved["action"]
    assert decision.reason == saved["reason"]
    assert decision.deadline == saved["deadline"]
    assert decision.cycle.mode == saved["cycle"]["m"]
    assert decision.cycle.off_since == saved["cycle"]["off"]
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
