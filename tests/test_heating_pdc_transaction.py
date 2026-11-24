"""Tests of the pure radiator <-> PDC hand-over state machine (M4).

Run with only pytest installed, from the repository root::

    python -m pytest -q --noconftest --import-mode=importlib tests/test_heating_pdc_transaction.py

The clock is explicit; nothing sleeps.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import itertools
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "custom_components" / "versatile_thermostat" / "heating_pdc_transaction.py"
MODULE_NAME = "_vtherm_pure_heating_pdc_transaction_under_test"


def _load() -> ModuleType:
    existing = sys.modules.get(MODULE_NAME)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


m = _load()

T0 = 1_795_500_000.0
ROOM = "camera_carlo"
OTHER = "camera_diego_luisa"
FREE = m.PdcReservation()


def tx0(room: str = ROOM, token: int = 0) -> object:
    return m.PdcTransaction(room, token=token)


def obs(token: int, **changes: object) -> object:
    values = dict(override_off=True)
    values.update(changes)
    return m.PdcObservation(token, **values)


def kinds(step: object) -> list[str]:
    return [c.kind for c in step.commands]


def start(room: str = ROOM, reservation: object = FREE, now: float = T0, **changes: object) -> object:
    kwargs = dict(override_off=True, other_pdc_busy=False)
    kwargs.update(changes)
    return m.request_start(tx0(room), reservation, now, **kwargs)


def to_waiting_target(now: float = T0) -> object:
    s = start(now=now)
    return m.observe(s.transaction, s.reservation, obs(1, radiator_stopped=True, conditions_ok=True), now + 10.0)


def to_waiting_heat(now: float = T0) -> object:
    s = to_waiting_target(now)
    return m.observe(s.transaction, s.reservation, obs(1, target_confirmed=True, conditions_ok=True), now + 10.0)


def to_running(now: float = T0) -> object:
    s = to_waiting_heat(now)
    return m.observe(s.transaction, s.reservation, obs(1, split_mode="heat", conditions_ok=True), now + 25.0)


def to_returning(failure_obs: object = None, now: float = T0) -> object:
    s = to_running(now)
    return m.request_return(s.transaction, s.reservation, now + 700.0, obs(1))


# --- acceptance and mutual exclusion ------------------------------------------------------------


def test_start_reserves_inhibits_the_radiator_and_waits_25_s():
    s = start()
    assert s.accepted and s.reason is None
    tx = s.transaction
    assert (tx.phase, tx.token, tx.deadline, tx.started_at) == (m.PHASE_WAITING_RADIATOR, 1, T0 + 25.0, T0)
    assert tx.inhibition_owned and tx.reservation_held
    assert s.reservation == m.PdcReservation(ROOM, 1)
    assert s.commands == (m.PdcCommand(m.CMD_INHIBIT_RADIATOR, 1, ROOM),)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"override_off": False}, m.DENIED_OVERRIDE),
        ({"override_off": None}, m.DENIED_OVERRIDE),
        ({"other_pdc_busy": True}, m.DENIED_OTHER_PDC),
        ({"other_pdc_busy": None}, m.DENIED_OTHER_PDC),
    ],
)
def test_start_refused_without_side_effects(changes, reason):
    s = start(**changes)
    assert not s.accepted and s.reason == reason and s.commands == ()
    assert s.transaction == tx0() and s.reservation == FREE


def test_two_simultaneous_requests_only_one_gets_the_reservation():
    first = start(ROOM)
    second = start(OTHER, reservation=first.reservation)
    assert first.accepted
    assert not second.accepted and second.reason == m.DENIED_RESERVED and second.commands == ()
    assert second.reservation == first.reservation and second.transaction == tx0(OTHER)
    # the same room asking again while busy is refused as busy
    again = m.request_start(first.transaction, first.reservation, T0, override_off=True, other_pdc_busy=False)
    assert not again.accepted and again.reason == m.DENIED_BUSY and again.commands == ()


def test_reservation_helpers_ignore_late_releases():
    held, granted = m.reserve(FREE, ROOM, 3)
    assert granted and held == m.PdcReservation(ROOM, 3)
    assert m.reserve(held, ROOM, 3) == (held, True)  # idempotent
    assert m.reserve(held, OTHER, 1) == (held, False)
    assert m.reserve(held, ROOM, 4) == (held, False)
    assert m.release(held, ROOM, 2) == held  # old token
    assert m.release(held, OTHER, 3) == held  # another room
    assert m.release(held, ROOM, 3) == FREE
    with pytest.raises(ValueError):
        m.PdcReservation(ROOM, None)


# --- start path ---------------------------------------------------------------------------------


def test_happy_path_commands_in_order_and_no_release_while_running():
    s = start()
    s = m.observe(s.transaction, s.reservation, obs(1, radiator_stopped=True, conditions_ok=True), T0 + 12.0)
    assert (s.transaction.phase, kinds(s)) == (m.PHASE_WAITING_TARGET, [m.CMD_SET_TARGET])
    s = m.observe(s.transaction, s.reservation, obs(1, target_confirmed=True, conditions_ok=True), T0 + 13.0)
    assert (s.transaction.phase, kinds(s), s.transaction.deadline) == (m.PHASE_WAITING_HEAT, [m.CMD_PDC_HEAT], T0 + 33.0)
    s = m.observe(s.transaction, s.reservation, obs(1, split_mode="heat", conditions_ok=True), T0 + 30.0)
    assert s.transaction.phase == m.PHASE_RUNNING and s.commands == ()
    assert s.transaction.inhibition_owned and s.transaction.reservation_held
    assert s.reservation == m.PdcReservation(ROOM, 1)


def test_waiting_for_the_radiator_is_not_a_failure_before_25_s():
    s = start()
    for elapsed in (0.0, 10.0, 24.999):
        step = m.observe(s.transaction, s.reservation, obs(1, radiator_stopped=False, conditions_ok=False), T0 + elapsed)
        assert step.transaction == s.transaction and step.commands == () and not step.accepted
    stopped_but_conditions_changed = m.observe(s.transaction, s.reservation, obs(1, radiator_stopped=True, conditions_ok=False), T0 + 3.0)
    assert stopped_but_conditions_changed.transaction.failure == m.FAILURE_CONDITIONS_CHANGED
    assert kinds(stopped_but_conditions_changed) == [m.CMD_PDC_OFF]


@pytest.mark.parametrize("radiator_stopped", [False, None])
def test_radiator_timeout_at_25_s_returns_without_releasing_anything(radiator_stopped):
    s = start()
    step = m.observe(s.transaction, s.reservation, obs(1, radiator_stopped=radiator_stopped, conditions_ok=False), T0 + 25.0)
    assert step.transaction.phase == m.PHASE_RETURNING and step.transaction.failure == m.FAILURE_TRV_UNCONFIRMED
    assert kinds(step) == [m.CMD_PDC_OFF]
    assert step.transaction.inhibition_owned and step.transaction.reservation_held and step.reservation == s.reservation


def test_radiator_confirmed_exactly_at_the_deadline_is_accepted():
    s = start()
    step = m.observe(s.transaction, s.reservation, obs(1, radiator_stopped=True, conditions_ok=True), T0 + 25.0)
    assert step.transaction.phase == m.PHASE_WAITING_TARGET


def test_target_has_no_wait_in_the_baseline_and_a_wait_can_be_configured():
    waiting = to_waiting_target()
    tx, res = waiting.transaction, waiting.reservation
    assert tx.deadline == tx.phase_started_at
    fail = m.observe(tx, res, obs(1, target_confirmed=False, conditions_ok=False), tx.phase_started_at)
    assert fail.transaction.failure == m.FAILURE_TARGET_UNCONFIRMED and kinds(fail) == [m.CMD_PDC_OFF]
    changed = m.observe(tx, res, obs(1, target_confirmed=True, conditions_ok=False), tx.phase_started_at)
    assert changed.transaction.failure == m.FAILURE_CONDITIONS_CHANGED
    slow = m.PdcTransactionConfig(target_timeout_s=5.0)
    s = start()
    s = m.observe(s.transaction, s.reservation, obs(1, radiator_stopped=True, conditions_ok=True), T0 + 10.0, slow)
    assert s.transaction.deadline == T0 + 15.0
    wait = m.observe(s.transaction, s.reservation, obs(1, target_confirmed=None, conditions_ok=False), T0 + 14.0, slow)
    assert not wait.accepted and wait.commands == ()
    late = m.observe(s.transaction, s.reservation, obs(1, target_confirmed=None, conditions_ok=False), T0 + 15.0, slow)
    assert late.transaction.failure == m.FAILURE_TARGET_UNCONFIRMED


def test_heat_confirmation_waits_20_s_then_fails_with_split_unconfirmed():
    waiting = to_waiting_heat()
    tx, res = waiting.transaction, waiting.reservation
    for elapsed in (0.0, 19.9):
        step = m.observe(tx, res, obs(1, split_mode="off", conditions_ok=False), tx.phase_started_at + elapsed)
        assert not step.accepted and step.commands == ()
    for state in ("off", None, "unavailable"):
        step = m.observe(tx, res, obs(1, split_mode=state, conditions_ok=False), tx.phase_started_at + 20.0)
        assert step.transaction.failure == m.FAILURE_SPLIT_UNCONFIRMED and kinds(step) == [m.CMD_PDC_OFF]
    lost = m.observe(tx, res, obs(1, split_mode="heat", conditions_ok=False), tx.phase_started_at + 3.0)
    assert lost.transaction.failure == m.FAILURE_CONDITIONS_CHANGED
    unknown_conditions = m.observe(tx, res, obs(1, split_mode="heat", conditions_ok=None), tx.phase_started_at + 3.0)
    assert unknown_conditions.transaction.phase == m.PHASE_RETURNING


# --- return path --------------------------------------------------------------------------------


def test_return_releases_the_inhibition_only_after_the_split_is_confirmed_off():
    s = to_returning()
    tx, res = s.transaction, s.reservation
    assert tx.phase == m.PHASE_RETURNING and kinds(s) == [m.CMD_PDC_OFF] and tx.deadline == tx.phase_started_at + 20.0
    for state in (None, "unknown", "unavailable", "heat", "cool", "dry"):
        step = m.observe(tx, res, obs(1, split_mode=state), tx.phase_started_at + 5.0)
        assert m.CMD_RELEASE_INHIBITION not in kinds(step) and m.CMD_RELEASE_RESERVATION not in kinds(step)
        assert step.transaction.inhibition_owned and step.transaction.reservation_held
    done = m.observe(tx, res, obs(1, split_mode="off"), tx.phase_started_at + 8.0)
    assert done.transaction.phase == m.PHASE_IDLE and kinds(done) == [m.CMD_RELEASE_INHIBITION, m.CMD_RELEASE_RESERVATION]
    assert all(c.token == 1 for c in done.commands)
    assert done.reservation == FREE and not done.transaction.inhibition_owned and not done.transaction.reservation_held
    assert done.transaction.token == 1  # the generation survives idle


def test_direct_off_fallback_after_20_s_and_return_unconfirmed_after_15_s_more():
    s = to_returning()
    tx, res = s.transaction, s.reservation
    t = tx.phase_started_at
    fallback = m.observe(tx, res, obs(1, split_mode="heat"), t + 20.0)
    assert fallback.transaction.phase == m.PHASE_RETURNING_DIRECT and kinds(fallback) == [m.CMD_SPLIT_OFF_DIRECT]
    assert fallback.transaction.deadline == t + 35.0 and fallback.transaction.direct_off_sent
    waiting = m.observe(fallback.transaction, res, obs(1, split_mode="heat"), t + 34.0)
    assert not waiting.accepted and waiting.commands == ()
    stuck = m.observe(fallback.transaction, res, obs(1, split_mode="heat"), t + 35.0)
    assert stuck.transaction.phase == m.PHASE_RETURN_UNCONFIRMED and stuck.reason == m.FAILURE_RETURN_UNCONFIRMED
    assert stuck.commands == () and stuck.transaction.inhibition_owned and stuck.reservation == res
    # nothing more happens by itself, and nothing is released
    for state in ("off", "heat", None):
        after = m.observe(stuck.transaction, res, obs(1, split_mode=state), t + 400.0)
        assert after.transaction == stuck.transaction and after.commands == ()
    # a later explicit retry restarts the return and still releases only after OFF
    retry = m.request_return(stuck.transaction, res, t + 500.0, obs(1))
    assert retry.transaction.phase == m.PHASE_RETURNING and kinds(retry) == [m.CMD_PDC_OFF]
    done = m.observe(retry.transaction, res, obs(1, split_mode="off"), t + 510.0)
    assert done.transaction.phase == m.PHASE_IDLE and m.CMD_RELEASE_INHIBITION in kinds(done)


def test_split_confirmed_off_in_the_direct_phase_releases():
    s = to_returning()
    t = s.transaction.phase_started_at
    fb = m.observe(s.transaction, s.reservation, obs(1, split_mode="heat"), t + 21.0)
    done = m.observe(fb.transaction, fb.reservation, obs(1, split_mode="off"), t + 30.0)
    assert done.transaction.phase == m.PHASE_IDLE and kinds(done) == [m.CMD_RELEASE_INHIBITION, m.CMD_RELEASE_RESERVATION]


def test_never_releases_without_a_confirmed_off_over_a_state_grid():
    """Exhaustive: no sequence of non-off observations in any phase ever emits a release command."""
    modes = (None, "unknown", "unavailable", "heat", "cool", "auto", "dry")
    for ctor in (start, to_waiting_target, to_waiting_heat, to_running, to_returning):
        s = ctor()
        for split, pdc, rad, conds, tgt, elapsed in itertools.product(
            modes, modes[:3] + ("heat",), (True, False, None), (True, False, None), (True, False, None), (0.0, 19.9, 20.0, 25.0, 40.0, 100.0)
        ):
            step = m.observe(
                s.transaction,
                s.reservation,
                obs(1, split_mode=split, pdc_mode=pdc, radiator_stopped=rad, conditions_ok=conds, target_confirmed=tgt),
                s.transaction.phase_started_at + elapsed,
            )
            assert m.CMD_RELEASE_INHIBITION not in kinds(step), (ctor.__name__, split, elapsed)
            assert m.CMD_RELEASE_RESERVATION not in kinds(step)
            assert step.reservation == s.reservation


def test_split_already_off_before_the_wait_releases_immediately_without_waiting():
    running = to_running()
    step = m.request_return(running.transaction, running.reservation, T0 + 800.0, obs(1, split_mode="off", pdc_mode="off"))
    assert step.transaction.phase == m.PHASE_IDLE and kinds(step) == [m.CMD_RELEASE_INHIBITION, m.CMD_RELEASE_RESERVATION]
    # PDC VTherm still not off: the OFF command is sent and the wait is kept
    partial = m.request_return(running.transaction, running.reservation, T0 + 800.0, obs(1, split_mode="off", pdc_mode="heat"))
    assert partial.transaction.phase == m.PHASE_RETURNING and kinds(partial) == [m.CMD_PDC_OFF]
    # split off but unknown PDC mode is not "already off"
    unknown = m.request_return(running.transaction, running.reservation, T0 + 800.0, obs(1, split_mode="off", pdc_mode=None))
    assert unknown.transaction.phase == m.PHASE_RETURNING


def test_failed_start_returns_and_releases_only_after_off_confirmation():
    s = start()
    s = m.observe(s.transaction, s.reservation, obs(1, radiator_stopped=False), T0 + 25.0)
    assert s.transaction.phase == m.PHASE_RETURNING
    done = m.observe(s.transaction, s.reservation, obs(1, split_mode="off"), T0 + 30.0)
    assert done.transaction.phase == m.PHASE_IDLE and done.reservation == FREE
    # the machine can start again with the next generation
    again = m.request_start(done.transaction, done.reservation, T0 + 700.0, override_off=True, other_pdc_busy=False)
    assert again.accepted and again.transaction.token == 2 and again.commands[0].token == 2


def test_cancellation_uses_the_safe_return_path():
    waiting = to_waiting_target()
    step = m.cancel(waiting.transaction, waiting.reservation, T0 + 11.0, obs(1))
    assert step.transaction.phase == m.PHASE_RETURNING and step.transaction.failure == m.FAILURE_CANCELLED
    assert kinds(step) == [m.CMD_PDC_OFF] and step.transaction.inhibition_owned
    idle = m.cancel(tx0(), FREE, T0, obs(0))
    assert not idle.accepted and idle.reason == m.IGNORED_IDLE and idle.commands == ()
    already = m.cancel(step.transaction, step.reservation, T0 + 12.0, obs(1))
    assert not already.accepted and already.reason == m.IGNORED_NO_CHANGE and already.commands == ()


# --- tokens, duplicates, late events ------------------------------------------------------------


def test_stale_and_future_tokens_are_ignored_everywhere():
    s = start()
    for token in (0, 2, 99):
        step = m.observe(s.transaction, s.reservation, obs(token, radiator_stopped=True, conditions_ok=True), T0 + 1.0)
        assert step.transaction == s.transaction and step.reason == m.IGNORED_STALE and step.commands == () and not step.accepted
        stale_return = m.request_return(s.transaction, s.reservation, T0 + 1.0, obs(token))
        assert stale_return.reason == m.IGNORED_STALE and stale_return.commands == ()


def test_late_answer_of_a_previous_transaction_cannot_complete_the_new_one():
    first = to_returning()
    done = m.observe(first.transaction, first.reservation, obs(1, split_mode="off"), T0 + 800.0)
    second = m.request_start(done.transaction, done.reservation, T0 + 1500.0, override_off=True, other_pdc_busy=False)
    assert second.transaction.token == 2
    late = m.observe(second.transaction, second.reservation, obs(1, radiator_stopped=True, conditions_ok=True, split_mode="heat"), T0 + 1501.0)
    assert late.transaction == second.transaction and late.reason == m.IGNORED_STALE
    # a late release of the old token does not free the new reservation
    assert m.release(second.reservation, ROOM, 1) == second.reservation


def test_duplicate_events_emit_each_command_once():
    s = start()
    ok = obs(1, radiator_stopped=True, conditions_ok=True)
    first = m.observe(s.transaction, s.reservation, ok, T0 + 5.0)
    assert kinds(first) == [m.CMD_SET_TARGET]
    # the same event again: the machine is already waiting for the target and does not re-send anything
    dup = m.observe(first.transaction, first.reservation, ok, T0 + 5.0)
    assert m.CMD_SET_TARGET not in kinds(dup) and m.CMD_INHIBIT_RADIATOR not in kinds(dup)
    returning = to_returning()
    off = obs(1, split_mode="off")
    done = m.observe(returning.transaction, returning.reservation, off, T0 + 800.0)
    twice = m.observe(done.transaction, done.reservation, off, T0 + 800.0)
    assert len(done.commands) == 2 and twice.commands == () and twice.reason == m.IGNORED_IDLE


# --- manual override ----------------------------------------------------------------------------


@pytest.mark.parametrize("override_off", [False, None])
@pytest.mark.parametrize("ctor", [start, to_waiting_target, to_waiting_heat])
def test_override_not_off_freezes_the_sequence_without_commands_or_releases(ctor, override_off):
    s = ctor()
    step = m.observe(
        s.transaction, s.reservation, obs(1, override_off=override_off, radiator_stopped=True, conditions_ok=True, split_mode="heat"), s.transaction.phase_started_at + 1.0
    )
    assert step.transaction.phase == m.PHASE_OVERRIDE_HOLD and step.commands == ()
    assert step.transaction.inhibition_owned and step.transaction.reservation_held and step.reservation == s.reservation
    again = m.observe(step.transaction, step.reservation, obs(1, radiator_stopped=True, conditions_ok=True, split_mode="off"), T0 + 100.0)
    assert again.transaction == step.transaction and again.commands == () and again.reason == m.IGNORED_HOLD


def test_override_during_the_return_freezes_it_and_keeps_the_inhibition():
    s = to_returning()
    step = m.observe(s.transaction, s.reservation, obs(1, override_off=False, split_mode="off"), s.transaction.phase_started_at + 3.0)
    assert step.transaction.phase == m.PHASE_OVERRIDE_HOLD and step.commands == () and step.transaction.inhibition_owned


def test_return_after_a_frozen_override_needs_the_override_confirmed_off():
    s = to_waiting_target()
    held = m.observe(s.transaction, s.reservation, obs(1, override_off=False), T0 + 15.0)
    blocked = m.request_return(held.transaction, held.reservation, T0 + 20.0, obs(1, override_off=False))
    assert not blocked.accepted and blocked.reason == m.DENIED_OVERRIDE and blocked.commands == ()
    no_obs = m.request_return(held.transaction, held.reservation, T0 + 20.0, None)
    assert no_obs.reason == m.DENIED_OVERRIDE and no_obs.commands == ()
    back = m.request_return(held.transaction, held.reservation, T0 + 21.0, obs(1, override_off=True))
    assert back.transaction.phase == m.PHASE_RETURNING and kinds(back) == [m.CMD_PDC_OFF]


def test_override_while_running_does_not_touch_the_machine():
    s = to_running()
    step = m.request_return(s.transaction, s.reservation, T0 + 900.0, obs(1, override_off=False))
    assert step.transaction.phase == m.PHASE_RUNNING and step.commands == () and step.reason == m.DENIED_OVERRIDE


# --- restart and reconnection -------------------------------------------------------------------


def test_recover_keeps_a_verified_running_transaction():
    s = to_running()
    step = m.recover(s.transaction, s.reservation, T0 + 5000.0, obs(1, split_mode="heat", pdc_mode="heat"))
    assert step.transaction == s.transaction and step.commands == ()


@pytest.mark.parametrize("ctor", [start, to_waiting_target, to_waiting_heat, to_running, to_returning])
def test_recover_sends_everything_else_through_the_return_path_with_a_new_token(ctor):
    s = ctor()
    step = m.recover(s.transaction, s.reservation, T0 + 5000.0, obs(1, split_mode="heat", pdc_mode="off"))
    tx = step.transaction
    assert tx.phase == m.PHASE_RETURNING and tx.token == 2 and tx.failure == m.FAILURE_RECOVERED
    assert kinds(step) == [m.CMD_PDC_OFF] and step.commands[0].token == 2
    assert step.reservation == m.PdcReservation(ROOM, 2) and tx.inhibition_owned
    # events of the pre-restart generation are stale
    old = m.observe(tx, step.reservation, obs(1, split_mode="off"), T0 + 5001.0)
    assert old.reason == m.IGNORED_STALE and old.commands == ()
    done = m.observe(tx, step.reservation, obs(2, split_mode="off"), T0 + 5002.0)
    assert done.transaction.phase == m.PHASE_IDLE and done.reservation == FREE


def test_recover_idle_and_unknown_override_do_nothing_dangerous():
    assert m.recover(tx0(), FREE, T0, obs(0)).transaction == tx0()
    s = to_waiting_heat()
    unknown = m.recover(s.transaction, s.reservation, T0 + 100.0, obs(1, override_off=None))
    assert unknown.transaction.phase == m.PHASE_OVERRIDE_HOLD and unknown.commands == () and unknown.transaction.inhibition_owned
    no_obs = m.recover(s.transaction, s.reservation, T0 + 100.0, None)
    assert no_obs.transaction.phase == m.PHASE_OVERRIDE_HOLD and no_obs.commands == ()
    running = to_running()
    assert m.recover(running.transaction, running.reservation, T0 + 100.0, obs(1, split_mode=None, pdc_mode=None)).transaction.phase == m.PHASE_RETURNING


# --- inhibition ownership -----------------------------------------------------------------------


def test_inhibition_state_before_the_request_is_recorded_and_commands_carry_the_owner_token():
    s = start(radiator_inhibited_before=True)
    assert s.transaction.inhibited_before is True and s.transaction.inhibition_owned
    assert s.commands[0].token == s.transaction.token  # the runtime releases only the inhibition placed with this token
    unknown = start(radiator_inhibited_before=None)
    assert unknown.transaction.inhibited_before is None


# --- validation and purity ----------------------------------------------------------------------


def test_models_validate_their_inputs_and_are_frozen():
    for bad in ({"radiator_timeout_s": -1.0}, {"heat_timeout_s": float("nan")}, {"off_timeout_s": True}, {"direct_off_timeout_s": float("inf")}):
        with pytest.raises((TypeError, ValueError)):
            m.PdcTransactionConfig(**bad)
    with pytest.raises(ValueError):
        m.PdcTransaction("")
    with pytest.raises(ValueError):
        m.PdcTransaction(ROOM, token=-1)
    with pytest.raises(TypeError):
        m.PdcObservation("1")
    with pytest.raises(TypeError):
        m.PdcObservation(1, override_off="yes")
    with pytest.raises(ValueError):
        start(now=float("nan"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        tx0().phase = m.PHASE_RUNNING  # type: ignore[misc]


def test_module_is_pure():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "math", "dataclasses", "typing"}, imported
