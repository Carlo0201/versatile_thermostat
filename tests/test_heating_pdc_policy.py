"""Edge-case tests for the pure PDC (heat pump) decision policy (M3).

The module under test is ``heating_pdc_policy.py``. It is loaded by file path under a
private name, so the component package (whose ``__init__`` imports Home Assistant) is
never executed. From the repository root, with only pytest installed::

    python -m pytest -q --noconftest --import-mode=importlib tests/test_heating_pdc_policy.py

The expected values come from the baseline automations ``*_scelta_radiatore_o_pompa_di_calore``
(Camera Carlo and Camera Diego Luisa) and their companions. The exhaustive comparison with the
real Jinja templates lives in ``plans/heating-orchestration-2026-11-24/pdc/verify_m3.py``; one
test below runs it on a small seeded sample when Jinja2 is available.

All times are absolute Unix seconds with an explicit clock: no test sleeps.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import math
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
# ``PDC_POLICY_PATH`` points the suite at a mutated copy (``pdc/mutation_check.py``); unset in normal runs.
POLICY_PATH = Path(os.environ["PDC_POLICY_PATH"]) if os.environ.get("PDC_POLICY_PATH") else REPO_ROOT / "custom_components" / "versatile_thermostat" / "heating_pdc_policy.py"
HARNESS_PATH = REPO_ROOT / "plans" / "heating-orchestration-2026-11-24" / "pdc" / "verify_m3.py"
POLICY_MODULE_NAME = "_vtherm_pure_heating_pdc_policy_under_test"


def _load_pure_policy() -> ModuleType:
    """Load only ``heating_pdc_policy.py``, without the component package."""
    existing = sys.modules.get(POLICY_MODULE_NAME)
    if existing is not None and Path(getattr(existing, "__file__", "")).resolve() == POLICY_PATH:
        return existing
    spec = importlib.util.spec_from_file_location(POLICY_MODULE_NAME, POLICY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[POLICY_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(POLICY_MODULE_NAME, None)
        raise
    return module


p = _load_pure_policy()

NOW = 1_795_500_000.0

# The two captured rooms differ only in these two parameters (see M3-ESITO.md).
CARLO = p.PdcPolicyConfig(setpoint_min_c=16.0, manual_preset_bypass=True)
DIEGO = p.PdcPolicyConfig(setpoint_min_c=18.0, manual_preset_bypass=False)
ROOMS = pytest.mark.parametrize("config", [CARLO, DIEGO], ids=["camera_carlo", "camera_diego_luisa"])


def cycle_off(age: float = 3000.0, target: float | None = 19.0, now: float = NOW) -> object:
    return p.PdcCycle(mode="off", on=now - 4000.0, off=now - age, target=target)


def cycle_heat(on_age: float = 700.0, target: float | None = 19.0, now: float = NOW) -> object:
    return p.PdcCycle(mode="heat", on=now - on_age, off=now - 5000.0, target=target)


def snap(now: float = NOW, **changes: object) -> object:
    """Nominal snapshot ready to start: gap 0.5 C, 400 W, everything closed and idle."""
    values = dict(
        override="off",
        managed="off",
        managed_changed_at=now - 5000.0,
        automatic="on",
        central_mode="Auto",
        radiator_mode="heat",
        requested_hvac_mode="heat",
        preset_mode="comfort",
        temperature=18.5,
        temperature_reported_at=now - 30.0,
        target=19.0,
        window="off",
        window_changed_at=now - 5000.0,
        power=400.0,
        power_reported_at=now - 5.0,
        split_mode="off",
        split_changed_at=now - 5000.0,
        pdc_mode="off",
        pdc_setpoint=20.0,
        other_managed="off",
        other_split_mode="off",
        schedule_stop="none",
        cycle=cycle_off(now=now),
        cycle_available=True,
        heating_override_timer="idle",
        schedule_switches=("on", "off"),
        radiator_valve_mode="heat",
        radiator_inhibited=None,
    )
    values.update(changes)
    return p.PdcSnapshot(**values)


def running(now: float = NOW, **changes: object) -> object:
    """Nominal snapshot of a PDC running for 700 s (HEAT confirmed, nothing to stop)."""
    values = dict(managed="on", split_mode="heat", pdc_mode="heat", cycle=cycle_heat(now=now))
    values.update(changes)
    return snap(now, **values)


def ready_ctx(since: float = NOW - 200.0, consumed: bool = False) -> object:
    return p.PdcStartContext(ready_since=since, edge_consumed=consumed)


def decide(snapshot: object, config: object = CARLO, ctx: object = None, now: float = NOW) -> object:
    return p.evaluate_pdc(snapshot, ctx if ctx is not None else ready_ctx(now - 200.0), now, config)


def cond(decision: object, cid: str) -> object:
    return next(c for c in decision.conditions if c.id == cid)


# --- electric thresholds ------------------------------------------------------------------------


@ROOMS
@pytest.mark.parametrize(
    ("power", "action", "code"),
    [
        (0.0, "start", None),
        (1099.99, "start", None),
        (1100.0, "start", None),
        (1100.01, "none", "power_high"),
        (2699.0, "none", "power_high"),
        (-0.1, "none", "power_missing"),
        (None, "none", "power_missing"),
    ],
)
def test_start_power_threshold_is_1100_w_inclusive(config, power, action, code):
    decision = decide(snap(power=power), config)
    assert decision.action == action
    assert (code in decision.diagnostic_codes) if code else decision.diagnostic_codes == ()


@ROOMS
def test_start_capacity_limit_is_budget_minus_reserve_exactly(config):
    """Capacity predicate: power + 1500 W <= 2600 W; the diagnostic limit 1100 W is the same figure."""
    assert p.DEFAULT_CONFIG.power_budget_w - p.DEFAULT_CONFIG.power_reserve_w == p.DEFAULT_CONFIG.diagnostic_power_limit_w == 1100.0
    assert p._capacity_ok(snap(power=1100.0), NOW, config)
    assert not p._capacity_ok(snap(power=1100.000001), NOW, config)
    assert not p._capacity_ok(snap(power=-0.000001), NOW, config)


@ROOMS
@pytest.mark.parametrize(
    ("power", "action"),
    [(0.0, "none"), (2699.99, "none"), (2700.0, "stop"), (2700.01, "stop"), (5000.0, "stop"), (-50.0, "none")],
)
def test_running_stop_power_is_2700_w_exclusive_below(config, power, action):
    decision = decide(running(power=power), config, p.EMPTY_CONTEXT)
    assert decision.action in (action, "set_setpoint") if action == "none" else decision.action == action
    if action == "stop":
        assert decision.stop_reason == p.STOP_POWER_LIMIT
    else:
        assert decision.action != "stop"


@ROOMS
def test_running_negative_power_is_accepted_but_start_rejects_it(config):
    """Baseline quirk preserved: keep-running accepts a negative power, start rejects it."""
    assert p.keep_running_ok(running(power=-50.0), NOW, config)
    assert not p._capacity_ok(snap(power=-50.0), NOW, config)


@ROOMS
@pytest.mark.parametrize("power", [None])
def test_power_absent_blocks_start_and_stops_a_running_pdc(config, power):
    start = decide(snap(power=power, power_reported_at=None), config)
    assert start.action == "none" and p.CODE_POWER_MISSING in start.diagnostic_codes
    assert cond(start, "power_capacity").status == p.CONDITION_UNKNOWN
    stop = decide(running(power=power, power_reported_at=None), config, p.EMPTY_CONTEXT)
    assert stop.action == "stop" and stop.stop_reason == p.STOP_DATA_MISSING


@ROOMS
@pytest.mark.parametrize(("age", "fresh"), [(0.0, True), (45.0, True), (45.25, False), (300.0, False), (-30.0, True)])
def test_power_sample_age_limit_is_45_s_and_future_samples_pass(config, age, fresh):
    """The age limit is inclusive; a sample from the future is accepted (baseline quirk)."""
    assert p._power_fresh(snap(power_reported_at=NOW - age), NOW, config) is fresh
    decision = decide(snap(power_reported_at=NOW - age), config)
    assert (decision.action == "start") is fresh


# --- thermal thresholds -------------------------------------------------------------------------


@ROOMS
@pytest.mark.parametrize(
    ("temperature", "action"),
    [(18.6, "start"), (18.600001, "start"), (18.5999989, "start"), (18.6000011, "none"), (18.601, "none"), (18.599, "start"), (18.0, "start"), (19.0, "none"), (19.5, "none")],
)
def test_start_gap_threshold_is_0_4_c_with_micro_tolerance(config, temperature, action):
    decision = decide(snap(temperature=temperature), config)
    assert decision.action == action
    if action == "none":
        assert (p.CODE_SMALL_GAP in decision.diagnostic_codes) or (p.CODE_AT_TARGET in decision.diagnostic_codes)


def test_gap_bit_exactly_at_the_tolerated_limit_starts():
    """Bit-exact boundary: target - temperature equals 0.4 - 1e-6 in floating point."""
    snapshot = snap(target=0.5, temperature=0.10000099999999994, pdc_setpoint=16.0)
    assert snapshot.target - snapshot.temperature == p.DEFAULT_CONFIG.start_gap_c - p.DEFAULT_CONFIG.comparison_epsilon_c
    assert decide(snapshot).action == "start"
    assert decide(snap(target=0.5, temperature=0.1000011, pdc_setpoint=16.0)).action == "none"


@ROOMS
@pytest.mark.parametrize(
    ("temperature", "on_age", "stops", "reason"),
    [
        (19.1999989, 700.0, False, None),
        (19.2, 599.0, False, None),
        (19.2, 600.0, True, p.STOP_TARGET_REACHED),
        (19.2, 5000.0, True, p.STOP_TARGET_REACHED),
        (19.4999989, 100.0, False, None),
        (19.5, 100.0, True, p.STOP_TEMPERATURE_LIMIT),
        (19.5, 599.0, True, p.STOP_TEMPERATURE_LIMIT),
        (19.0, 600.0, False, None),
    ],
)
def test_ordinary_stop_at_plus_0_2_after_600_s_and_early_stop_at_plus_0_5(config, temperature, on_age, stops, reason):
    snapshot = running(temperature=temperature, cycle=cycle_heat(on_age, 19.0))
    decision = decide(snapshot, config, p.EMPTY_CONTEXT)
    assert (decision.action == "stop") is stops
    assert decision.stop_reason == reason


@ROOMS
def test_minimum_on_599_and_600_seconds(config):
    assert not p.minimum_on_done(running(cycle=cycle_heat(599.0)), NOW, config)
    assert p.minimum_on_done(running(cycle=cycle_heat(600.0)), NOW, config)
    assert not p.minimum_on_done(running(cycle=p.PdcCycle(mode="off", on=NOW - 5000.0)), NOW, config)
    assert not p.minimum_on_done(running(cycle=p.PdcCycle(mode="heat", on=None)), NOW, config)


@ROOMS
def test_protections_take_precedence_over_the_minimum_on_time(config):
    """Power, missing data, window, central mode and program stop even before 600 s of HEAT."""
    young = cycle_heat(30.0)
    for changes, reason in (
        ({"power": 2700.0}, p.STOP_POWER_LIMIT),
        ({"window": "unknown"}, p.STOP_WINDOW),
        ({"central_mode": "Stopped"}, p.STOP_CENTRAL),
        ({"automatic": "off"}, p.STOP_AUTOMATIC_DISABLED),
        ({"requested_hvac_mode": "off"}, p.STOP_PROGRAM_OFF),
        ({"temperature": None}, p.STOP_DATA_MISSING),
        ({"split_mode": "off"}, p.STOP_SPLIT_MODE),
    ):
        decision = decide(running(cycle=young, **changes), config, p.EMPTY_CONTEXT)
        assert decision.action == "stop", changes
        assert decision.stop_reason == reason, changes


# --- next program band --------------------------------------------------------------------------


@ROOMS
@pytest.mark.parametrize(
    ("offset", "action"),
    [("none", "start"), (600.25, "start"), (600.0, "none"), (599.75, "none"), (30.0, "none"), (-10.0, "none"), ("invalid", "none"), ("0", "none")],
)
def test_start_is_refused_when_the_next_program_change_is_within_600_s(config, offset, action):
    schedule = offset if isinstance(offset, str) else repr(NOW + offset)
    decision = decide(snap(schedule_stop=schedule), config)
    assert decision.action == action
    if action == "none":
        assert decision.diagnostic_codes[:1] in ((p.CODE_SCHEDULE_CHANGE,), (p.CODE_DATA_MISSING,))


@ROOMS
def test_program_horizon_is_rechecked_by_the_sequence_predicates(config):
    """After the radiator stops and after the setpoint, the same horizon applies (``float(0)`` semantics)."""
    stopped = dict(radiator_valve_mode="off", radiator_mode="off", radiator_inhibited=True)
    assert p.recheck_after_radiator_stop(snap(**stopped), NOW, config)
    assert not p.recheck_after_radiator_stop(snap(schedule_stop=repr(NOW + 599.0), **stopped), NOW, config)
    assert p.recheck_after_radiator_stop(snap(schedule_stop=repr(NOW + 601.0), **stopped), NOW, config)
    assert not p.recheck_after_radiator_stop(snap(schedule_stop="garbage", **stopped), NOW, config)
    assert p.schedule_ok(snap(schedule_stop="none"), NOW, config)


# --- 599/600 s guards on the start path ---------------------------------------------------------


@ROOMS
@pytest.mark.parametrize(("off_age", "action"), [(599.0, "none"), (599.75, "none"), (600.0, "start"), (601.0, "start")])
def test_new_cycle_requires_600_s_since_confirmed_off(config, off_age, action):
    decision = decide(snap(cycle=cycle_off(off_age)), config)
    assert decision.action == action
    if action == "none":
        assert p.CODE_COOLDOWN in decision.diagnostic_codes
        assert cond(decision, "cooldown").status == p.CONDITION_WAIT
        assert cond(decision, "cooldown").deadline == NOW - off_age + 600.0


@ROOMS
@pytest.mark.parametrize(
    "cycle",
    [
        p.PdcCycle(mode="off", on=1.0, off=None),
        p.PdcCycle(mode="off", on=1.0, off=NOW + 50.0),
        p.PdcCycle(mode="off", on=1.0, off=-5.0),
        p.PdcCycle(mode="heat", on=NOW - 5000.0, off=NOW - 3000.0),
        p.PdcCycle(mode="cool", off=NOW - 3000.0),
        p.PdcCycle(),
    ],
)
def test_missing_future_negative_or_inconsistent_cycle_never_counts_as_a_confirmed_off(config, cycle):
    decision = decide(snap(cycle=cycle), config)
    assert decision.action == "none"
    assert p.CODE_COOLDOWN in decision.diagnostic_codes
    assert not p.cooldown_ok(snap(cycle=cycle), NOW, config)


@ROOMS
def test_cooldown_predicate_checks_mode_and_sign_directly(config):
    assert p.cooldown_ok(snap(cycle=cycle_off(600.0)), NOW, config)
    assert not p.cooldown_ok(snap(cycle=p.PdcCycle(mode="heat", off=NOW - 3000.0)), NOW, config)
    assert not p.cooldown_ok(snap(cycle=p.PdcCycle(mode="off", off=-1.0)), NOW, config)


@ROOMS
def test_unavailable_cycle_register_is_data_missing(config):
    decision = decide(snap(cycle=p.EMPTY_CYCLE, cycle_available=False), config)
    assert decision.action == "none" and p.CODE_DATA_MISSING in decision.diagnostic_codes


@ROOMS
@pytest.mark.parametrize("field", ["split_changed_at", "managed_changed_at"])
@pytest.mark.parametrize(("age", "ok"), [(599.0, False), (599.75, False), (600.0, True)])
def test_raw_last_changed_guards_are_600_s_and_not_part_of_the_diagnostic(config, field, age, ok):
    """Baseline quirk: a recent split/managed change blocks the start while the diagnostic stays ``ready``."""
    snapshot = snap(**{field: NOW - age})
    assert p.raw_guards_ok(snapshot, NOW, config) is ok
    decision = decide(snapshot, config)
    assert decision.diagnostic_codes == ()
    assert (decision.action == "start") is ok
    if not ok:
        assert decision.reason == p.PDC_REASON_START_BLOCKED
        assert cond(decision, "raw_guards").status == p.CONDITION_WAIT


# --- stability 119/120 s and the one-shot edge --------------------------------------------------


@ROOMS
def test_stability_requires_120_s_continuously_ready(config):
    t0 = NOW
    first = p.evaluate_pdc(snap(t0), None, t0, config)
    assert first.action == "none" and first.reason == p.PDC_REASON_STABILITY_WAIT
    assert first.context.ready_since == t0
    assert cond(first, "stability").deadline == t0 + 120.0
    ctx = first.context
    for elapsed, action in ((60.0, "none"), (119.0, "none"), (119.999, "none"), (120.0, "start"), (121.0, "none")):
        t = t0 + elapsed
        decision = p.evaluate_pdc(snap(t), ctx, t, config)
        assert decision.action == action, elapsed
        ctx = decision.context
    # the edge is consumed by the evaluation that saw it, whatever the outcome
    assert ctx.edge_consumed is True


@ROOMS
def test_duplicate_events_do_not_start_twice(config):
    t = NOW
    first = p.evaluate_pdc(snap(t), ready_ctx(t - 120.0), t, config)
    assert first.action == "start"
    again = p.evaluate_pdc(snap(t), first.context, t, config)
    assert again.action == "none" and again.reason == p.PDC_REASON_EDGE_CONSUMED
    # the same input twice is the same decision (pure function)
    assert p.evaluate_pdc(snap(t), ready_ctx(t - 120.0), t, config) == first


@ROOMS
def test_a_blocked_start_still_consumes_the_edge_until_the_diagnostic_leaves_ready(config):
    """Baseline quirk (start_edge_one_shot): a raw guard that fails at the edge defers to the next ready period."""
    t = NOW
    blocked = p.evaluate_pdc(snap(t, split_changed_at=t - 100.0), ready_ctx(t - 130.0), t, config)
    assert blocked.reason == p.PDC_REASON_START_BLOCKED and blocked.context.edge_consumed
    later = t + 700.0
    stuck = p.evaluate_pdc(snap(later), blocked.context, later, config)
    assert stuck.action == "none" and stuck.reason == p.PDC_REASON_EDGE_CONSUMED
    left = p.evaluate_pdc(snap(later, window="on"), blocked.context, later, config)
    assert left.context.ready_since is None and not left.context.edge_consumed
    fresh = p.evaluate_pdc(snap(later + 1.0), left.context, later + 1.0, config)
    assert fresh.reason == p.PDC_REASON_STABILITY_WAIT


@ROOMS
def test_edge_flag_can_be_disabled_for_a_level_triggered_runtime(config):
    level = dataclasses.replace(config, start_edge_one_shot=False)
    ctx = ready_ctx(NOW - 130.0)
    first = p.evaluate_pdc(snap(), ctx, NOW, level)
    second = p.evaluate_pdc(snap(), first.context, NOW + 1.0, level)
    assert first.action == "start"
    assert second.action == "start" and second.context.edge_consumed is False


@ROOMS
@pytest.mark.parametrize("since", [NOW + 50.0, -5.0])
def test_future_or_negative_ready_since_restarts_the_wait(config, since):
    decision = p.evaluate_pdc(snap(), p.PdcStartContext(ready_since=since), NOW, config)
    assert decision.context.ready_since == NOW and decision.action == "none"


# --- variable target ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "carlo", "diego"),
    [
        (14.2, 16.0, 18.0),
        (15.0, 16.0, 18.0),
        (16.0, 17.0, 18.0),
        (16.5, 18.0, 18.0),
        (17.5, 19.0, 19.0),
        (19.0, 20.0, 20.0),
        (19.01, 21.0, 21.0),
        (19.5, 21.0, 21.0),
        (28.9, 30.0, 30.0),
        (29.0, 30.0, 30.0),
        (29.5, 30.0, 30.0),
    ],
)
def test_split_setpoint_is_ceil_target_plus_1_clamped(target, carlo, diego):
    assert p.split_setpoint(target, CARLO) == carlo
    assert p.split_setpoint(target, DIEGO) == diego
    assert p.split_setpoint(None, CARLO) is None


@ROOMS
def test_running_updates_the_setpoint_when_the_target_changes(config):
    """A raised target moves the split setpoint; the sequence is not restarted."""
    steady = decide(running(), config, p.EMPTY_CONTEXT)
    assert steady.action == "none" and steady.reason == p.PDC_REASON_RUNNING
    raised = decide(running(target=19.5, temperature=19.0), config, p.EMPTY_CONTEXT)
    assert raised.action == "set_setpoint" and raised.setpoint == 21.0
    assert decide(running(target=19.5, temperature=19.0, pdc_setpoint=21.0), config, p.EMPTY_CONTEXT).action == "none"


@ROOMS
def test_lowered_target_already_reached_stops_even_before_600_s(config):
    snapshot = running(target=19.0, temperature=19.0, cycle=cycle_heat(60.0, target=20.0))
    decision = decide(snapshot, config, p.EMPTY_CONTEXT)
    assert decision.action == "stop" and decision.stop_reason == p.STOP_TARGET_LOWERED
    # lowered but not reached yet: keeps running
    assert decide(running(target=19.0, temperature=18.5, cycle=cycle_heat(60.0, target=20.0)), config, p.EMPTY_CONTEXT).action != "stop"
    # raised target: not a lowered-target stop
    assert not p.target_lowered(running(target=19.0, temperature=19.0, cycle=cycle_heat(60.0, target=18.0)))
    # initial target missing: no lowered-target stop
    assert not p.target_lowered(running(cycle=cycle_heat(60.0, target=None)))


@ROOMS
def test_target_changed_between_waits_changes_the_expected_setpoint(config):
    stopped = dict(radiator_valve_mode="off", radiator_mode="off", radiator_inhibited=True)
    base = snap(pdc_setpoint=20.0, **stopped)
    assert p.recheck_after_setpoint(base, NOW, config)
    moved = snap(target=19.5, pdc_setpoint=20.0, **stopped)
    assert not p.recheck_after_setpoint(moved, NOW, config)
    assert p.classify_start_failure(p.STAGE_WAITING_TARGET, moved, config) == p.FAILURE_TARGET_UNCONFIRMED
    assert p.classify_start_failure(p.STAGE_WAITING_TARGET, snap(target=None), config) is None


@ROOMS
def test_missing_target_never_starts_and_is_not_zero(config):
    decision = decide(snap(target=None), config)
    assert decision.action == "none" and p.CODE_TEMPERATURE_MISSING in decision.diagnostic_codes
    assert decision.setpoint is None


# --- window 119/120 s, closing and reopening ----------------------------------------------------


@ROOMS
@pytest.mark.parametrize(("age", "keeps"), [(0.0, True), (119.0, True), (120.0, True), (120.25, False), (121.0, False), (600.0, False)])
def test_running_tolerates_an_open_window_for_120_s_strict(config, age, keeps):
    snapshot = running(window="on", window_changed_at=NOW - age)
    assert p.window_running_ok(snapshot, NOW, config) is keeps
    decision = decide(snapshot, config, p.EMPTY_CONTEXT)
    assert (decision.action != "stop") is keeps
    if not keeps:
        assert decision.stop_reason == p.STOP_WINDOW


@ROOMS
def test_window_reopened_after_closing_restarts_the_120_s_count(config):
    """Open for 100 s, closed 10 s, reopened: the count restarts at the new ``last_changed``."""
    t_open = NOW
    t_closed = t_open + 100.0
    t_reopen = t_closed + 10.0
    assert p.window_running_ok(running(t_open + 100.0, window="on", window_changed_at=t_open), t_open + 100.0, config)
    assert p.window_running_ok(running(t_closed + 5.0, window="off", window_changed_at=t_closed), t_closed + 5.0, config)
    for elapsed, keeps in ((0.0, True), (40.0, True), (120.0, True), (120.5, False)):
        t = t_reopen + elapsed
        snapshot = running(t, window="on", window_changed_at=t_reopen)
        assert p.window_running_ok(snapshot, t, config) is keeps, elapsed
    # without the interruption the same instant would already be over the limit
    t = t_reopen + 40.0
    assert not p.window_running_ok(running(t, window="on", window_changed_at=t_open), t, config)


@ROOMS
@pytest.mark.parametrize("state", ["unknown", "unavailable", None])
def test_unknown_window_is_not_a_closed_window(config, state):
    """While running: protection stop at once. Before starting: not ready, no start."""
    assert not p.window_running_ok(running(window=state), NOW, config)
    assert decide(running(window=state), config, p.EMPTY_CONTEXT).action == "stop"
    start = decide(snap(window=state), config)
    assert start.action == "none" and p.CODE_DATA_MISSING in start.diagnostic_codes
    assert cond(start, "window_closed").status == p.CONDITION_UNKNOWN


@ROOMS
def test_start_requires_a_closed_window_with_no_grace(config):
    decision = decide(snap(window="on", window_changed_at=NOW - 5.0), config)
    assert decision.action == "none" and p.CODE_WINDOW in decision.diagnostic_codes
    assert decide(snap(window="on", window_changed_at=NOW - 5000.0), config).action == "none"


@ROOMS
@pytest.mark.parametrize(
    ("window", "age", "stops"),
    [("on", 120.0, False), ("on", 120.25, True), ("on", 121.0, True), ("unknown", 29.75, False), ("unknown", 30.0, True), ("unavailable", 100.0, True), ("off", 9999.0, False)],
)
def test_override_protection_window_120_s_and_unknown_30_s(config, window, age, stops):
    snapshot = snap(override="on", split_mode="heat", window=window, window_changed_at=NOW - age)
    assert p.override_protection_stop(snapshot, NOW, config) is stops


# --- manual override while waiting --------------------------------------------------------------


@ROOMS
def test_override_during_the_stability_wait_cancels_it_and_it_restarts_from_zero(config):
    t0 = NOW
    waiting = p.evaluate_pdc(snap(t0), None, t0, config)
    assert waiting.reason == p.PDC_REASON_STABILITY_WAIT
    t1 = t0 + 100.0
    overridden = p.evaluate_pdc(snap(t1, override="on"), waiting.context, t1, config)
    assert overridden.action == "none" and overridden.reason == p.PDC_REASON_OVERRIDE
    assert overridden.context.ready_since is None and not overridden.context.edge_consumed
    assert p.CODE_MANUAL_OVERRIDE in overridden.diagnostic_codes
    # the override ends: the 120 s count starts again, the 100 s already waited do not count
    t2 = t1 + 10.0
    back = p.evaluate_pdc(snap(t2), overridden.context, t2, config)
    assert back.reason == p.PDC_REASON_STABILITY_WAIT and back.context.ready_since == t2
    assert p.evaluate_pdc(snap(t2 + 119.0), back.context, t2 + 119.0, config).action == "none"
    assert p.evaluate_pdc(snap(t2 + 120.0), back.context, t2 + 120.0, config).action == "start"


@ROOMS
def test_override_after_the_edge_but_before_the_command_blocks_the_start(config):
    """The edge fired with the override off; if it is on when the start is re-evaluated nothing is sent."""
    decision = p.evaluate_pdc(snap(override="on"), ready_ctx(NOW - 130.0), NOW, config)
    assert decision.action == "none" and decision.reason == p.PDC_REASON_OVERRIDE


@ROOMS
@pytest.mark.parametrize("override", ["on", "unknown", "unavailable", None])
def test_with_a_non_off_override_the_chooser_does_nothing_even_running_over_power(config, override):
    """Baseline quirk: not even a protective stop. The stop is the job of the override protections."""
    decision = decide(running(override=override, power=2900.0), config, p.EMPTY_CONTEXT)
    assert decision.action == "none"
    assert decision.reason == (p.PDC_REASON_OVERRIDE if override == "on" else p.PDC_REASON_OVERRIDE_UNKNOWN)


@ROOMS
@pytest.mark.parametrize(
    ("changes", "stops"),
    [
        ({"power": 2699.99}, False),
        ({"power": 2700.0}, True),
        ({"power": None}, True),
        ({"power": -1.0}, True),
        ({"power_reported_at": NOW - 45.25}, True),
        ({"central_mode": "Stopped"}, True),
        ({"split_mode": "off"}, False),
        ({"split_mode": "cool", "power": 2800.0}, True),
        ({"override": "off", "power": 2800.0}, False),
        ({"override": "unknown", "power": 2800.0}, False),
    ],
)
def test_override_protection_stop(config, changes, stops):
    values = {"override": "on", "split_mode": "heat"}
    values.update(changes)
    assert p.override_protection_stop(snap(**values), NOW, config) is stops


# --- unknown, stale and future data -------------------------------------------------------------


@ROOMS
@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"temperature": None}, p.CODE_TEMPERATURE_MISSING),
        ({"temperature_reported_at": NOW - 3600.25}, p.CODE_TEMPERATURE_MISSING),
        ({"temperature_reported_at": None}, p.CODE_TEMPERATURE_MISSING),
        ({"automatic": "unknown"}, p.CODE_DATA_MISSING),
        ({"central_mode": "unknown"}, p.CODE_CENTRAL_MODE),
        ({"managed": "unavailable"}, p.CODE_DATA_MISSING),
        ({"radiator_mode": "unavailable"}, p.CODE_DATA_MISSING),
        ({"split_mode": "unknown"}, p.CODE_DATA_MISSING),
        ({"pdc_mode": "unavailable"}, p.CODE_DATA_MISSING),
        ({"other_managed": "unknown"}, p.CODE_DATA_MISSING),
        ({"other_split_mode": "unavailable"}, p.CODE_DATA_MISSING),
        ({"other_split_mode": None}, p.CODE_DATA_MISSING),
        ({"schedule_stop": "unknown"}, p.CODE_DATA_MISSING),
    ],
)
def test_unknown_or_stale_inputs_never_start_and_never_count_as_nominal(config, changes, code):
    decision = decide(snap(**changes), config)
    assert decision.action == "none"
    assert code in decision.diagnostic_codes
    assert decision.diagnostic_state.startswith(",".join(decision.diagnostic_codes) + "|")


@ROOMS
def test_temperature_age_limit_is_3600_s_inclusive_and_future_passes_the_start(config):
    assert decide(snap(temperature_reported_at=NOW - 3600.0), config).action == "start"
    assert decide(snap(temperature_reported_at=NOW - 3600.25), config).action == "none"
    assert decide(snap(temperature_reported_at=NOW + 60.0), config).action == "start"


@ROOMS
def test_future_temperature_sample_never_triggers_a_thermal_stop(config):
    """Baseline quirk: the thermal stop needs ``0 <= age``; a future sample above target+0.5 keeps running."""
    hot_future = running(temperature=19.6, temperature_reported_at=NOW + 30.0)
    assert not p.thermal_stop(hot_future, NOW, config)
    assert decide(hot_future, config, p.EMPTY_CONTEXT).action != "stop"
    hot_now = running(temperature=19.6, temperature_reported_at=NOW - 30.0)
    assert p.thermal_stop(hot_now, NOW, config)


@ROOMS
def test_running_with_stale_temperature_or_power_stops_with_data_missing(config):
    for changes in ({"temperature_reported_at": NOW - 3600.25}, {"power_reported_at": NOW - 45.25}, {"power": None}):
        decision = decide(running(**changes), config, p.EMPTY_CONTEXT)
        assert decision.action == "stop" and decision.stop_reason == p.STOP_DATA_MISSING, changes


@ROOMS
def test_managed_flag_unknown_never_starts_and_never_stops(config):
    """``managed`` unknown is neither off nor on: no command and a dedicated reason."""
    for state in ("unknown", "unavailable", None):
        decision = decide(snap(managed=state, split_mode="heat", pdc_mode="heat"), config)
        assert decision.action == "none" and decision.reason == p.PDC_REASON_MANAGED_UNKNOWN


@ROOMS
@pytest.mark.parametrize("value", ["", "abc", None, 5, True, [], {}])
def test_parse_pdc_cycle_treats_bad_stored_values_as_missing(config, value):
    assert p.parse_pdc_cycle(value) == p.EMPTY_CYCLE


def test_parse_pdc_cycle_coerces_like_the_template_and_hardens_booleans():
    cycle = p.parse_pdc_cycle({"m": "off", "on": "1795400000", "off": 12, "target": "19.5"})
    assert (cycle.mode, cycle.on, cycle.off, cycle.target) == ("off", 1795400000.0, 12.0, 19.5)
    bad = p.parse_pdc_cycle({"m": 3, "on": True, "off": "nan", "target": float("inf")})
    assert (bad.mode, bad.on, bad.off, bad.target) == (None, None, None, None)


# --- persistent cycle register ------------------------------------------------------------------


def test_register_writes_once_per_change_and_truncates_the_clock():
    previous = {"m": "off", "on": 100, "off": 200, "target": 19.0}
    written = p.observe_split_mode(previous, "heat", 19.5, 1000.75)
    assert written == {"m": "heat", "on": 1000, "off": 200, "target": 19.5}
    assert p.observe_split_mode(written, "heat", 19.5, 1001.0) is None  # duplicate event: no new write
    back = p.observe_split_mode(written, "off", None, 1700.9)
    assert back == {"m": "off", "on": 1000, "off": 1700, "target": 19.5}
    assert p.observe_split_mode(back, "off", None, 1701.0) is None


def test_register_fills_a_missing_on_or_off_even_without_a_change():
    assert p.observe_split_mode({"m": "heat", "off": 5}, "heat", 18.0, 900.0) == {"m": "heat", "on": 900, "off": 5, "target": 18.0}
    assert p.observe_split_mode({"m": "off", "on": 5}, "off", None, 900.0) == {"m": "off", "on": 5, "off": 900, "target": None}


def test_register_ignores_unrecordable_modes_and_unreadable_previous_values():
    for mode in ("unknown", "unavailable", None, "weird"):
        assert p.observe_split_mode({"m": "off"}, mode, 19.0, 1000.0) is None
    for previous in (None, [], "x", 3):
        assert p.observe_split_mode(previous, "heat", 19.0, 1000.0) is None
    # unreadable stored text decodes to {} (``from_json({})``): the register is rebuilt
    assert p.observe_split_mode({}, "off", None, 1000.0) == {"m": "off", "on": None, "off": 1000, "target": None}


# --- mutual exclusion between the two PDCs: no new priority ------------------------------------


@ROOMS
@pytest.mark.parametrize(
    ("other_managed", "other_split", "free"),
    [("off", "off", True), ("on", "off", False), ("off", "heat", False), ("off", "cool", False), ("unknown", "off", False), ("off", "unavailable", False), (None, None, False)],
)
def test_other_pdc_must_be_off_and_unmanaged(config, other_managed, other_split, free):
    snapshot = snap(other_managed=other_managed, other_split_mode=other_split)
    assert p.other_pdc_free(snapshot) is free
    assert (decide(snapshot, config).action == "start") is free


def test_no_priority_between_pdcs_and_no_single_room_in_demand_rule():
    """The model has no priority, ranking, tie-break or ``only room in demand`` input at all."""
    names = {f.name for f in dataclasses.fields(p.PdcSnapshot)} | {f.name for f in dataclasses.fields(p.PdcPolicyConfig)}
    assert not {n for n in names if any(word in n for word in ("priority", "rank", "tie", "only", "unique", "single", "demand", "other_request"))}
    # symmetric: with the other free both rooms start; the evaluation never looks at the other room's demand
    assert decide(snap(), CARLO).action == "start" and decide(snap(), DIEGO).action == "start"
    a = decide(snap(other_managed="on"), CARLO)
    b = decide(snap(other_managed="on"), DIEGO)
    assert a.action == b.action == "none" and p.CODE_OTHER_PDC in a.diagnostic_codes


# --- differences between the two rooms (explicit parameters) -----------------------------------


def test_only_two_parameters_differ_between_the_captured_rooms():
    differing = {f.name for f in dataclasses.fields(p.PdcPolicyConfig) if getattr(CARLO, f.name) != getattr(DIEGO, f.name)}
    assert differing == {"setpoint_min_c", "manual_preset_bypass"}
    assert p.DEFAULT_CONFIG.setpoint_min_c == 16.0 and p.DEFAULT_CONFIG.manual_preset_bypass is False


@pytest.mark.parametrize(
    ("timer", "switches", "carlo", "diego"),
    [
        ("active", ("on", "on"), True, False),
        ("idle", ("off", "off"), True, False),
        ("idle", ("on", "off"), False, False),
        ("idle", ("unavailable", "off"), False, False),
        ("paused", ("off", "off"), False, False),
        ("unknown", ("off", "off"), False, False),
    ],
)
def test_manual_regulation_preset_bypass_is_a_carlo_only_parameter(timer, switches, carlo, diego):
    """With a preset outside the allowed list (e.g. ``away``) only Camera Carlo accepts the manual Oikos regulation."""
    snapshot = snap(preset_mode="away", heating_override_timer=timer, schedule_switches=switches)
    assert p.preset_ok(snapshot, CARLO) is carlo
    assert p.preset_ok(snapshot, DIEGO) is diego
    assert (decide(snapshot, CARLO).action == "start") is carlo
    assert decide(snapshot, DIEGO).action == "none" and p.CODE_PRESET in decide(snapshot, DIEGO).diagnostic_codes


@ROOMS
@pytest.mark.parametrize("preset", ["none", "eco", "comfort", "boost"])
def test_allowed_presets_pass_in_both_rooms(config, preset):
    assert p.preset_ok(snap(preset_mode=preset), config)


def test_setpoint_floor_is_16_for_carlo_and_18_for_diego_luisa():
    snapshot = snap(target=14.2, temperature=14.0, pdc_setpoint=16.0)
    assert decide(snapshot, CARLO, p.EMPTY_CONTEXT).setpoint is None  # not running: no setpoint proposal
    running_low = running(target=14.2, temperature=14.0, pdc_setpoint=16.0)
    assert decide(running_low, CARLO, p.EMPTY_CONTEXT).action == "none"
    assert decide(running_low, DIEGO, p.EMPTY_CONTEXT).action == "set_setpoint"
    assert decide(running_low, DIEGO, p.EMPTY_CONTEXT).setpoint == 18.0


@ROOMS
@pytest.mark.parametrize("central", ["Auto", "Heat only"])
def test_central_modes_allowed(config, central):
    assert decide(snap(central_mode=central), config).action == "start"


@ROOMS
@pytest.mark.parametrize("central", ["Stopped", "Cool only", "Off", "unknown", None])
def test_central_modes_not_allowed(config, central):
    assert decide(snap(central_mode=central), config).action == "none"


# --- sequence predicates (M4 re-checks) ---------------------------------------------------------


@ROOMS
def test_radiator_stop_needs_valve_off_vtherm_off_and_inhibition_reported(config):
    ok = dict(radiator_valve_mode="off", radiator_mode="off", radiator_inhibited=True)
    assert p.radiator_stopped(snap(**ok))
    for change in ({"radiator_valve_mode": "heat"}, {"radiator_mode": "heat"}, {"radiator_inhibited": False}, {"radiator_inhibited": None}, {"radiator_valve_mode": "unknown"}):
        assert not p.radiator_stopped(snap(**{**ok, **change})), change
    assert p.classify_start_failure(p.STAGE_WAITING_TRV, snap(**{**ok, "radiator_inhibited": False}), config) == p.FAILURE_TRV_UNCONFIRMED
    assert p.classify_start_failure(p.STAGE_WAITING_TRV, snap(**ok), config) == p.FAILURE_CONDITIONS_CHANGED
    assert p.classify_start_failure(p.STAGE_WAITING_SPLIT, snap(split_mode="off"), config) == p.FAILURE_SPLIT_UNCONFIRMED
    assert p.classify_start_failure(p.STAGE_WAITING_SPLIT, snap(split_mode="heat"), config) == p.FAILURE_CONDITIONS_CHANGED
    assert p.classify_start_failure("idle", snap(), config) == p.FAILURE_CONDITIONS_CHANGED
    assert p.classify_start_failure(None, snap(), config) == p.FAILURE_CONDITIONS_CHANGED


@ROOMS
def test_confirmation_after_heat_is_the_running_check(config):
    assert p.keep_running_ok(running(), NOW, config)
    assert not p.keep_running_ok(running(split_mode="off"), NOW, config)
    assert not p.keep_running_ok(running(pdc_mode="off"), NOW, config)
    assert not p.keep_running_ok(running(preset_mode="away", heating_override_timer="paused"), NOW, config)


# --- decision envelope --------------------------------------------------------------------------


@ROOMS
def test_decision_exposes_state_string_deadline_and_conditions(config):
    decision = p.evaluate_pdc(snap(), None, NOW, config)
    assert decision.diagnostic_state == "ready|1100|120"
    assert decision.evaluated_at == NOW
    assert decision.deadline == NOW + 40.0  # the 5 s old power sample is the first value to expire
    ids = [c.id for c in decision.conditions]
    assert len(ids) == len(set(ids))
    assert {"override", "automatic", "thermal_gap", "power_capacity", "window_closed", "other_pdc", "raw_guards", "schedule", "cooldown", "stability"} <= set(ids)
    assert cond(decision, "min_on").status == p.CONDITION_NA
    run = decide(running(), config, p.EMPTY_CONTEXT)
    assert cond(run, "thermal_gap").status == p.CONDITION_NA and cond(run, "min_on").status == p.CONDITION_PASS
    young = decide(running(cycle=cycle_heat(300.0)), config, p.EMPTY_CONTEXT)
    assert cond(young, "min_on").status == p.CONDITION_WAIT and cond(young, "min_on").deadline == NOW - 300.0 + 600.0


@ROOMS
def test_conditions_are_informational_and_do_not_change_the_decision(config):
    decision = decide(snap(), config)
    stripped = dataclasses.replace(decision, conditions=())
    assert stripped.action == decision.action == "start"


# --- validation and purity ----------------------------------------------------------------------


def test_config_rejects_non_deterministic_parameters():
    for bad in (
        {"stability_s": -1.0},
        {"stability_s": float("nan")},
        {"power_budget_w": float("inf")},
        {"start_gap_c": True},
        {"setpoint_min_c": 31.0},
        {"allowed_central_modes": ()},
        {"allowed_presets": "none"},
    ):
        with pytest.raises((TypeError, ValueError)):
            p.PdcPolicyConfig(**bad)
    with pytest.raises(TypeError):
        p.PdcPolicyConfig(manual_preset_bypass="yes")
    with pytest.raises(TypeError):
        p.PdcPolicyConfig(start_edge_one_shot=1)


def test_snapshot_rejects_non_finite_boolean_and_non_string_values():
    for bad in (
        {"temperature": float("nan")},
        {"power": float("inf")},
        {"temperature": True},
        {"override": 1},
        {"schedule_switches": "on"},
        {"cycle": {}},
        {"radiator_inhibited": "yes"},
        {"cycle_available": 1},
    ):
        with pytest.raises((TypeError, ValueError)):
            snap(**bad)
    with pytest.raises((TypeError, ValueError)):
        p.PdcCycle(on=float("nan"))
    with pytest.raises(TypeError):
        p.PdcStartContext(edge_consumed="no")


def test_evaluate_validates_its_arguments_and_the_clock():
    with pytest.raises(TypeError):
        p.evaluate_pdc({}, None, NOW)
    with pytest.raises(TypeError):
        p.evaluate_pdc(snap(), {}, NOW)
    with pytest.raises(TypeError):
        p.evaluate_pdc(snap(), None, NOW, {})
    for bad_now in (float("nan"), float("inf"), "now", True, None):
        with pytest.raises((TypeError, ValueError)):
            p.evaluate_pdc(snap(), None, bad_now)


def test_models_are_frozen_and_inputs_are_not_mutated():
    snapshot = snap()
    before = dataclasses.asdict(snapshot)
    p.evaluate_pdc(snapshot, None, NOW)
    assert dataclasses.asdict(snapshot) == before
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.target = 25.0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        CARLO.stability_s = 1.0  # type: ignore[misc]
    previous = {"m": "off"}
    p.observe_split_mode(previous, "heat", 19.0, 10.0)
    assert previous == {"m": "off"}


def test_module_is_pure_no_home_assistant_no_io_no_clock_no_asyncio():
    tree = ast.parse(POLICY_PATH.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "math", "dataclasses", "typing"}, imported
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"time", "now", "utcnow", "sleep", "open", "read_text"}


def test_decisions_are_deterministic_over_a_clock_sweep():
    """Same inputs, same decision, for every second around the 120 s and 600 s thresholds."""
    for offset in range(-3, 4):
        for base in (120.0, 600.0):
            t = NOW + offset
            ctx = ready_ctx(t - base)
            for config in (CARLO, DIEGO):
                one = p.evaluate_pdc(snap(t), ctx, t, config)
                two = p.evaluate_pdc(snap(t), ctx, t, config)
                assert one == two
                assert math.isfinite(one.evaluated_at)


# --- parity with the real Jinja templates (optional) -------------------------------------------


def test_parity_with_the_baseline_templates_on_a_seeded_sample():
    """Runs the offline harness on every boundary scenario and 150 seeded random cases per room."""
    pytest.importorskip("jinja2")
    done = subprocess.run(
        [sys.executable, "-I", str(HARNESS_PATH), "--random", "150", "--seed", "20261124", "--no-report"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
