"""Tests of the pure radiator <-> PDC hand-over state machine (M4).

Run with only pytest installed, from the repository root::

    python -m pytest -q --noconftest --import-mode=importlib tests/test_heating_pdc_transaction.py

The clock is explicit; nothing sleeps. The review of M4 (ownership of the
inhibition, strict confirmations, ``releasing`` phase, late HEAT, persistence) is
covered by the tests in the sections "inhibition ownership", "strict
confirmations", "release", "late confirmations" and "safety invariants".
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import itertools
import os
import random
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    Path(os.environ["PDC_COMPONENT_DIR"]) if os.environ.get("PDC_COMPONENT_DIR") else REPO_ROOT / "custom_components" / "versatile_thermostat"
) / "heating_pdc_transaction.py"
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
OFF = dict(split_mode="off", pdc_mode="off")


def tx0(room: str = ROOM, token: int = 0) -> object:
    return m.PdcTransaction(room, token=token)


def obs(token: int, **changes: object) -> object:
    values = dict(override_off=True)
    values.update(changes)
    return m.PdcObservation(token, **values)


def kinds(step: object) -> list[str]:
    return [c.kind for c in step.commands]


def start(room: str = ROOM, reservation: object = FREE, now: float = T0, **changes: object) -> object:
    kwargs = dict(override_off=True, other_pdc_busy=False, radiator_inhibited_before=False)
    kwargs.update(changes)
    return m.request_start(tx0(room), reservation, now, **kwargs)


def to_waiting_target(now: float = T0) -> object:
    s = start(now=now)
    return m.observe(s.transaction, s.reservation, obs(1, radiator_stopped=True, conditions_ok=True), now + 10.0)


def to_waiting_heat(now: float = T0) -> object:
    s = to_waiting_target(now)
    return m.observe(s.transaction, s.reservation, obs(1, target_confirmed=True, radiator_stopped=True, conditions_ok=True), now + 10.0)


def to_running(now: float = T0) -> object:
    s = to_waiting_heat(now)
    return m.observe(s.transaction, s.reservation, obs(1, split_mode="heat", radiator_stopped=True, conditions_ok=True), now + 25.0)


def to_returning(failure_obs: object = None, now: float = T0) -> object:
    s = to_running(now)
    return m.request_return(s.transaction, s.reservation, now + 700.0, obs(1))


def to_releasing(now: float = T0) -> object:
    s = to_returning(now=now)
    return m.observe(s.transaction, s.reservation, obs(1, **OFF), s.transaction.phase_started_at + 8.0)


def to_idle(now: float = T0) -> object:
    s = to_releasing(now)
    return m.confirm_release(s.transaction, s.reservation, now + 800.0, token=1, released=True)


# --- acceptance and mutual exclusion ------------------------------------------------------------


def test_start_reserves_inhibits_the_radiator_and_waits_25_s():
    s = start()
    assert s.accepted and s.reason is None
    tx = s.transaction
    assert (tx.phase, tx.token, tx.deadline, tx.started_at) == (m.PHASE_WAITING_RADIATOR, 1, T0 + 25.0, T0)
    assert tx.inhibition_owned and tx.reservation_held and tx.inhibited_before is False
    assert s.reservation == m.PdcReservation(ROOM, 1)
    assert s.commands == (m.PdcCommand(m.CMD_INHIBIT_RADIATOR, 1, ROOM),)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"override_off": False}, m.DENIED_OVERRIDE),
        ({"override_off": None}, m.DENIED_OVERRIDE),
        ({"other_pdc_busy": True}, m.DENIED_OTHER_PDC),
        ({"other_pdc_busy": None}, m.DENIED_OTHER_PDC),
        ({"radiator_inhibited_before": True}, m.DENIED_FOREIGN_INHIBITION),
        ({"radiator_inhibited_before": None}, m.DENIED_INHIBITION_UNKNOWN),
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
    again = m.request_start(first.transaction, first.reservation, T0, override_off=True, other_pdc_busy=False, radiator_inhibited_before=False)
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


# --- inhibition ownership (review) --------------------------------------------------------------


def test_the_request_requires_an_explicit_inhibition_state():
    with pytest.raises(TypeError):
        m.request_start(tx0(), FREE, T0, override_off=True, other_pdc_busy=False)  # type: ignore[call-arg]


def test_an_inhibition_that_is_already_there_is_never_claimed():
    for before in (True, None):
        s = start(radiator_inhibited_before=before)
        assert not s.accepted and s.transaction.inhibition_owned is False and s.commands == () and s.reservation == FREE


def test_owned_means_not_inhibited_before_the_request():
    s = start(radiator_inhibited_before=False)
    assert s.transaction.inhibition_owned and s.transaction.inhibited_before is False
    assert s.commands[0].token == s.transaction.token  # the runtime releases only the inhibition placed with this token


def test_call_that_found_the_inhibition_already_present_drops_the_claim_and_abandons_the_start():
    s = start()
    step = m.record_inhibition_result(s.transaction, s.reservation, T0 + 1.0, token=1, inhibited_after=True, transitioned=False, obs=obs(1, **OFF))
    assert step.transaction.inhibition_owned is False
    assert step.transaction.phase == m.PHASE_IDLE and step.transaction.failure == m.FAILURE_INHIBITION_NOT_OWNED
    assert m.CMD_RELEASE_INHIBITION not in kinds(step) and step.reservation == FREE
    # without a split reading the return path asks for OFF first and never releases a foreign inhibition either
    blind = m.record_inhibition_result(s.transaction, s.reservation, T0 + 1.0, token=1, inhibited_after=True, transitioned=False)
    assert blind.transaction.phase == m.PHASE_RETURNING and kinds(blind) == [m.CMD_PDC_OFF] and not blind.transaction.inhibition_owned
    done = m.observe(blind.transaction, blind.reservation, obs(1, **OFF), T0 + 5.0)
    assert done.transaction.phase == m.PHASE_IDLE and m.CMD_RELEASE_INHIBITION not in kinds(done) and done.reservation == FREE


def test_a_call_that_did_not_inhibit_leaves_nothing_to_release():
    s = start()
    step = m.record_inhibition_result(s.transaction, s.reservation, T0 + 1.0, token=1, inhibited_after=False, transitioned=False, obs=obs(1, **OFF))
    assert step.transaction.phase == m.PHASE_IDLE and step.transaction.failure == m.FAILURE_INHIBITION_FAILED and not step.transaction.inhibition_owned


def test_confirmed_creation_and_unreadable_state_keep_the_claim():
    s = start()
    ok = m.record_inhibition_result(s.transaction, s.reservation, T0 + 1.0, token=1, inhibited_after=True, transitioned=True)
    assert ok.transaction == s.transaction and ok.commands == ()
    unreadable = m.record_inhibition_result(s.transaction, s.reservation, T0 + 1.0, token=1, inhibited_after=None, transitioned=None)
    assert unreadable.transaction == s.transaction  # the claim made before the call stands
    unknown_marker = m.record_inhibition_result(s.transaction, s.reservation, T0 + 1.0, token=1, inhibited_after=True, transitioned=None)
    assert unknown_marker.transaction == s.transaction
    late = m.record_inhibition_result(s.transaction, s.reservation, T0 + 1.0, token=7, inhibited_after=True, transitioned=False)
    assert late.reason == m.IGNORED_STALE and late.transaction == s.transaction


def test_an_unreadable_flag_with_no_call_sent_never_claims_the_inhibition():
    # The flag was unknown before the call and nothing was sent: whatever appears later is not ours.
    s = start()
    step = m.record_inhibition_result(s.transaction, s.reservation, T0 + 1.0, token=1, inhibited_after=None, transitioned=None, obs=obs(1, **OFF), call_sent=False)
    assert not step.transaction.inhibition_owned
    assert step.transaction.phase == m.PHASE_IDLE and step.transaction.failure == m.FAILURE_INHIBITION_FAILED
    # a call that was sent and whose result is unreadable still keeps the claim (it may have been applied)
    sent = m.record_inhibition_result(s.transaction, s.reservation, T0 + 1.0, token=1, inhibited_after=None, transitioned=None, call_sent=True)
    assert sent.transaction == s.transaction


def test_a_stop_confirmation_of_a_foreign_inhibition_does_not_start_the_pdc():
    s = start()
    foreign = dataclasses.replace(s.transaction, inhibition_owned=False)
    step = m.observe(foreign, s.reservation, obs(1, radiator_stopped=True, conditions_ok=True), T0 + 5.0)
    assert step.transaction.failure == m.FAILURE_INHIBITION_NOT_OWNED and kinds(step) == [m.CMD_PDC_OFF]
    heat_phase = dataclasses.replace(to_waiting_target().transaction, inhibition_owned=False)
    blocked = m.observe(heat_phase, s.reservation, obs(1, target_confirmed=True, radiator_stopped=True, conditions_ok=True), T0 + 12.0)
    assert m.CMD_PDC_HEAT not in kinds(blocked) and blocked.transaction.failure == m.FAILURE_INHIBITION_NOT_OWNED


def test_a_return_without_ownership_never_emits_a_release():
    s = to_running()
    foreign = dataclasses.replace(s.transaction, inhibition_owned=False)
    back = m.request_return(foreign, s.reservation, T0 + 900.0, obs(1, **OFF))
    assert back.transaction.phase == m.PHASE_IDLE and kinds(back) == [m.CMD_RELEASE_RESERVATION]
    slow = m.request_return(foreign, s.reservation, T0 + 900.0, obs(1))
    done = m.observe(slow.transaction, slow.reservation, obs(1, **OFF), T0 + 905.0)
    assert done.transaction.phase == m.PHASE_IDLE and m.CMD_RELEASE_INHIBITION not in kinds(done)


# --- start path ---------------------------------------------------------------------------------


def test_happy_path_commands_in_order_and_no_release_while_running():
    s = start()
    s = m.observe(s.transaction, s.reservation, obs(1, radiator_stopped=True, conditions_ok=True), T0 + 12.0)
    assert (s.transaction.phase, kinds(s)) == (m.PHASE_WAITING_TARGET, [m.CMD_SET_TARGET])
    s = m.observe(s.transaction, s.reservation, obs(1, target_confirmed=True, radiator_stopped=True, conditions_ok=True), T0 + 13.0)
    assert (s.transaction.phase, kinds(s), s.transaction.deadline) == (m.PHASE_WAITING_HEAT, [m.CMD_PDC_HEAT], T0 + 33.0)
    assert s.transaction.heat_sent_at == T0 + 13.0
    s = m.observe(s.transaction, s.reservation, obs(1, split_mode="heat", radiator_stopped=True, conditions_ok=True), T0 + 30.0)
    assert s.transaction.phase == m.PHASE_RUNNING and s.commands == () and s.transaction.heat_sent_at is None
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


@pytest.mark.parametrize("radiator_stopped", [False, None])
def test_a_green_recheck_without_the_radiator_stop_never_reaches_the_target_step(radiator_stopped):
    """The machine does not rely on the caller: ``conditions_ok`` alone is not a confirmed stop."""
    s = start()
    for elapsed in (3.0, 25.0, 100.0):
        step = m.observe(s.transaction, s.reservation, obs(1, radiator_stopped=radiator_stopped, conditions_ok=True), T0 + elapsed)
        assert m.CMD_SET_TARGET not in kinds(step) and step.transaction.phase != m.PHASE_WAITING_TARGET
    expired = m.observe(s.transaction, s.reservation, obs(1, radiator_stopped=radiator_stopped, conditions_ok=True), T0 + 25.0)
    assert expired.transaction.failure == m.FAILURE_TRV_UNCONFIRMED and kinds(expired) == [m.CMD_PDC_OFF]


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
    changed = m.observe(tx, res, obs(1, target_confirmed=True, radiator_stopped=True, conditions_ok=False), tx.phase_started_at)
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


# --- strict confirmations (review) --------------------------------------------------------------


def test_the_pdc_is_never_started_without_the_radiator_stop_and_the_target_confirmed():
    waiting = to_waiting_target()
    tx, res = waiting.transaction, waiting.reservation
    sent = []
    for target, stopped, conds in itertools.product((True, False, None), repeat=3):
        step = m.observe(tx, res, obs(1, target_confirmed=target, radiator_stopped=stopped, conditions_ok=conds), tx.phase_started_at)
        if m.CMD_PDC_HEAT in kinds(step):
            sent.append((target, stopped, conds))
    assert sent == [(True, True, True)]


def test_a_stop_not_reported_at_the_hand_over_to_heat_is_a_radiator_released_failure():
    waiting = to_waiting_target()
    step = m.observe(waiting.transaction, waiting.reservation, obs(1, target_confirmed=True, radiator_stopped=False, conditions_ok=True), waiting.transaction.phase_started_at)
    assert step.transaction.failure == m.FAILURE_RADIATOR_RELEASED and kinds(step) == [m.CMD_PDC_OFF] and m.CMD_PDC_HEAT not in kinds(step)


def test_running_is_reached_only_with_split_heat_the_radiator_stopped_and_the_recheck():
    waiting = to_waiting_heat()
    tx, res = waiting.transaction, waiting.reservation
    reached = []
    for split, stopped, conds in itertools.product(("heat", "off", None), (True, False, None), (True, False, None)):
        step = m.observe(tx, res, obs(1, split_mode=split, radiator_stopped=stopped, conditions_ok=conds), tx.phase_started_at + 3.0)
        if step.transaction.phase == m.PHASE_RUNNING:
            reached.append((split, stopped, conds))
    assert reached == [("heat", True, True)]
    released = m.observe(tx, res, obs(1, split_mode="heat", radiator_stopped=False, conditions_ok=True), tx.phase_started_at + 3.0)
    assert released.transaction.failure == m.FAILURE_RADIATOR_RELEASED
    # an unreported radiator can still be confirmed until the HEAT wait ends
    pending = m.observe(tx, res, obs(1, split_mode="heat", radiator_stopped=None, conditions_ok=True), tx.phase_started_at + 3.0)
    assert not pending.accepted and pending.commands == ()
    expired = m.observe(tx, res, obs(1, split_mode="heat", radiator_stopped=None, conditions_ok=True), tx.phase_started_at + 20.0)
    assert expired.transaction.failure == m.FAILURE_RADIATOR_RELEASED


# --- return path --------------------------------------------------------------------------------


def test_return_releases_the_inhibition_only_after_the_split_and_the_pdc_are_confirmed_off():
    s = to_returning()
    tx, res = s.transaction, s.reservation
    assert tx.phase == m.PHASE_RETURNING and kinds(s) == [m.CMD_PDC_OFF] and tx.deadline == tx.phase_started_at + 20.0
    for state in (None, "unknown", "unavailable", "heat", "cool", "dry"):
        step = m.observe(tx, res, obs(1, split_mode=state, pdc_mode="off"), tx.phase_started_at + 5.0)
        assert m.CMD_RELEASE_INHIBITION not in kinds(step) and m.CMD_RELEASE_RESERVATION not in kinds(step)
        assert step.transaction.inhibition_owned and step.transaction.reservation_held
    for pdc in (None, "unknown", "heat"):
        step = m.observe(tx, res, obs(1, split_mode="off", pdc_mode=pdc), tx.phase_started_at + 5.0)
        assert m.CMD_RELEASE_INHIBITION not in kinds(step) and step.transaction.phase == m.PHASE_RETURNING
    done = m.observe(tx, res, obs(1, **OFF), tx.phase_started_at + 8.0)
    assert done.transaction.phase == m.PHASE_RELEASING and kinds(done) == [m.CMD_RELEASE_INHIBITION]
    assert all(c.token == 1 for c in done.commands)
    # the reservation stays held until the radiator is really back
    assert done.reservation == res and done.transaction.reservation_held and done.transaction.inhibition_owned
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
    # nothing is released while the OFF is not confirmed, whatever arrives
    for split, pdc in (("heat", "off"), (None, None), ("off", "heat"), ("unavailable", "unavailable")):
        after = m.observe(stuck.transaction, res, obs(1, split_mode=split, pdc_mode=pdc), t + 40.0)
        assert after.transaction == stuck.transaction and after.commands == ()


def test_return_unconfirmed_retries_by_itself_and_a_late_off_confirmation_completes_it():
    s = to_returning()
    tx, res = s.transaction, s.reservation
    t = tx.phase_started_at
    fb = m.observe(tx, res, obs(1, split_mode="heat"), t + 20.0)
    stuck = m.observe(fb.transaction, res, obs(1, split_mode="heat"), t + 35.0)
    assert stuck.transaction.deadline == t + 35.0 + 120.0
    retry = m.observe(stuck.transaction, res, obs(1, split_mode="heat", pdc_mode="heat"), stuck.transaction.deadline)
    assert retry.transaction.phase == m.PHASE_RETURNING and kinds(retry) == [m.CMD_PDC_OFF]
    late = m.observe(stuck.transaction, res, obs(1, **OFF), t + 100.0)
    assert late.transaction.phase == m.PHASE_RELEASING and kinds(late) == [m.CMD_RELEASE_INHIBITION]
    explicit = m.request_return(stuck.transaction, res, t + 500.0, obs(1))
    assert explicit.transaction.phase == m.PHASE_RETURNING and kinds(explicit) == [m.CMD_PDC_OFF]
    no_retry = m.PdcTransactionConfig(return_retry_s=0.0)
    fb2 = m.observe(tx, res, obs(1, split_mode="heat"), t + 20.0, no_retry)
    stuck2 = m.observe(fb2.transaction, res, obs(1, split_mode="heat"), t + 35.0, no_retry)
    assert stuck2.transaction.deadline is None
    assert m.observe(stuck2.transaction, res, obs(1, split_mode="heat"), t + 9000.0, no_retry).commands == ()


def test_split_confirmed_off_in_the_direct_phase_releases():
    s = to_returning()
    t = s.transaction.phase_started_at
    fb = m.observe(s.transaction, s.reservation, obs(1, split_mode="heat"), t + 21.0)
    done = m.observe(fb.transaction, fb.reservation, obs(1, **OFF), t + 30.0)
    assert done.transaction.phase == m.PHASE_RELEASING and kinds(done) == [m.CMD_RELEASE_INHIBITION]


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


def test_release_needs_both_the_split_and_the_pdc_vtherm_off():
    running = to_running()
    for split, pdc in (("off", "heat"), ("heat", "off"), ("off", None), (None, "off"), ("off", "unavailable")):
        step = m.request_return(running.transaction, running.reservation, T0 + 800.0, obs(1, split_mode=split, pdc_mode=pdc))
        assert step.transaction.phase == m.PHASE_RETURNING and m.CMD_RELEASE_INHIBITION not in kinds(step)
        later = m.observe(step.transaction, step.reservation, obs(1, split_mode=split, pdc_mode=pdc), T0 + 805.0)
        assert m.CMD_RELEASE_INHIBITION not in kinds(later)


def test_split_already_off_before_the_wait_starts_the_release_immediately():
    running = to_running()
    step = m.request_return(running.transaction, running.reservation, T0 + 800.0, obs(1, **OFF))
    assert step.transaction.phase == m.PHASE_RELEASING and kinds(step) == [m.CMD_RELEASE_INHIBITION]
    partial = m.request_return(running.transaction, running.reservation, T0 + 800.0, obs(1, split_mode="off", pdc_mode="heat"))
    assert partial.transaction.phase == m.PHASE_RETURNING and kinds(partial) == [m.CMD_PDC_OFF]
    unknown = m.request_return(running.transaction, running.reservation, T0 + 800.0, obs(1, split_mode="off", pdc_mode=None))
    assert unknown.transaction.phase == m.PHASE_RETURNING


def test_failed_start_returns_and_releases_only_after_off_confirmation():
    s = start()
    s = m.observe(s.transaction, s.reservation, obs(1, radiator_stopped=False), T0 + 25.0)
    assert s.transaction.phase == m.PHASE_RETURNING
    done = m.observe(s.transaction, s.reservation, obs(1, **OFF), T0 + 30.0)
    assert done.transaction.phase == m.PHASE_RELEASING
    idle = m.confirm_release(done.transaction, done.reservation, T0 + 31.0, token=1, released=True)
    assert idle.transaction.phase == m.PHASE_IDLE and idle.reservation == FREE
    # the machine can start again with the next generation
    again = m.request_start(idle.transaction, idle.reservation, T0 + 700.0, override_off=True, other_pdc_busy=False, radiator_inhibited_before=False)
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


# --- release (review) ---------------------------------------------------------------------------


def test_a_failed_release_keeps_the_ownership_the_reservation_and_retries():
    s = to_releasing()
    tx, res = s.transaction, s.reservation
    failed = m.confirm_release(tx, res, T0 + 800.0, token=1, released=False)
    assert failed.transaction.phase == m.PHASE_RELEASING and failed.transaction.inhibition_owned and failed.reservation == res and failed.commands == ()
    assert failed.transaction.deadline == T0 + 830.0
    early = m.observe(failed.transaction, res, obs(1, **OFF), T0 + 829.0)
    assert early.commands == ()
    retry = m.observe(failed.transaction, res, obs(1, **OFF), T0 + 830.0)
    assert kinds(retry) == [m.CMD_RELEASE_INHIBITION] and retry.transaction.release_attempts == 2
    unknown = m.confirm_release(retry.transaction, res, T0 + 831.0, token=1, released=None)
    assert unknown.transaction.phase == m.PHASE_RELEASING
    ok = m.confirm_release(retry.transaction, res, T0 + 832.0, token=1, released=True)
    assert ok.transaction.phase == m.PHASE_IDLE and ok.reservation == FREE and kinds(ok) == [m.CMD_RELEASE_RESERVATION]
    late = m.confirm_release(ok.transaction, ok.reservation, T0 + 833.0, token=1, released=True)
    assert late.commands == () and not late.accepted
    stale = m.confirm_release(tx, res, T0 + 800.0, token=9, released=True)
    assert stale.reason == m.IGNORED_STALE and stale.transaction == tx


def test_a_release_attempt_is_dropped_if_the_pdc_came_back_in_the_meantime():
    s = to_releasing()
    for split, pdc in (("heat", "off"), ("off", "heat"), ("cool", "cool")):
        step = m.observe(s.transaction, s.reservation, obs(1, split_mode=split, pdc_mode=pdc), T0 + 801.0)
        assert step.transaction.phase == m.PHASE_RETURNING and kinds(step) == [m.CMD_PDC_OFF] and m.CMD_RELEASE_INHIBITION not in kinds(step)
    # unknown readings are not a reason to go back, nor to release
    quiet = m.observe(s.transaction, s.reservation, obs(1, split_mode="unavailable", pdc_mode=None), s.transaction.deadline + 1.0)
    assert quiet.transaction.phase == m.PHASE_RELEASING and quiet.commands == ()


def test_a_radiator_seen_not_inhibited_ends_the_release():
    s = to_releasing()
    step = m.observe(s.transaction, s.reservation, obs(1, radiator_inhibited=False), T0 + 801.0)
    assert step.transaction.phase == m.PHASE_IDLE and step.reservation == FREE and m.CMD_RELEASE_INHIBITION not in kinds(step)
    unknown = m.observe(s.transaction, s.reservation, obs(1, radiator_inhibited=None), T0 + 801.0)
    assert unknown.transaction.phase == m.PHASE_RELEASING


# --- late confirmations (review) -----------------------------------------------------------------


def test_a_split_off_seen_while_a_heat_command_is_in_flight_is_not_an_end_of_session():
    w = to_waiting_heat()
    heat_at = w.transaction.heat_sent_at
    early = m.request_return(w.transaction, w.reservation, heat_at + 5.0, obs(1, **OFF))
    assert early.transaction.phase == m.PHASE_RETURNING and kinds(early) == [m.CMD_PDC_OFF]
    assert m.next_wakeup(early.transaction, heat_at + 5.0) == heat_at + 20.0  # not the 20 s OFF deadline of the return
    still = m.observe(early.transaction, early.reservation, obs(1, **OFF), heat_at + 19.0)
    assert still.commands == () and still.transaction.phase == m.PHASE_RETURNING
    settled = m.observe(early.transaction, early.reservation, obs(1, **OFF), heat_at + 20.0)
    assert settled.transaction.phase == m.PHASE_RELEASING and kinds(settled) == [m.CMD_RELEASE_INHIBITION]
    # late split HEAT of that command: it is seen while returning and keeps the return going
    late_heat = m.observe(early.transaction, early.reservation, obs(1, split_mode="heat", pdc_mode="off"), heat_at + 12.0)
    assert late_heat.commands == () and late_heat.transaction.phase == m.PHASE_RETURNING
    # a HEAT that was never sent does not delay anything
    plain = m.request_return(to_running().transaction, FREE, T0 + 900.0, obs(1, **OFF))
    assert plain.transaction.phase == m.PHASE_RELEASING


# --- tokens, duplicates, late events ------------------------------------------------------------


def test_stale_and_future_tokens_are_ignored_everywhere():
    s = start()
    for token in (0, 2, 99):
        step = m.observe(s.transaction, s.reservation, obs(token, radiator_stopped=True, conditions_ok=True), T0 + 1.0)
        assert step.transaction == s.transaction and step.reason == m.IGNORED_STALE and step.commands == () and not step.accepted
        stale_return = m.request_return(s.transaction, s.reservation, T0 + 1.0, obs(token))
        assert stale_return.reason == m.IGNORED_STALE and stale_return.commands == ()


def test_late_answer_of_a_previous_transaction_cannot_complete_the_new_one():
    done = to_idle()
    second = m.request_start(done.transaction, done.reservation, T0 + 1500.0, override_off=True, other_pdc_busy=False, radiator_inhibited_before=False)
    assert second.transaction.token == 2
    late = m.observe(second.transaction, second.reservation, obs(1, radiator_stopped=True, conditions_ok=True, split_mode="heat"), T0 + 1501.0)
    assert late.transaction == second.transaction and late.reason == m.IGNORED_STALE
    # a late release of the old token does not free the new reservation
    assert m.release(second.reservation, ROOM, 1) == second.reservation
    assert m.confirm_release(second.transaction, second.reservation, T0 + 1502.0, token=1, released=True).transaction == second.transaction


def test_duplicate_events_emit_each_command_once():
    s = start()
    ok = obs(1, radiator_stopped=True, conditions_ok=True)
    first = m.observe(s.transaction, s.reservation, ok, T0 + 5.0)
    assert kinds(first) == [m.CMD_SET_TARGET]
    # the same event again: the machine is already waiting for the target and does not re-send anything
    dup = m.observe(first.transaction, first.reservation, ok, T0 + 5.0)
    assert m.CMD_SET_TARGET not in kinds(dup) and m.CMD_INHIBIT_RADIATOR not in kinds(dup) and m.CMD_PDC_HEAT not in kinds(dup)
    returning = to_returning()
    off = obs(1, **OFF)
    done = m.observe(returning.transaction, returning.reservation, off, T0 + 800.0)
    twice = m.observe(done.transaction, done.reservation, off, T0 + 800.0)
    assert kinds(done) == [m.CMD_RELEASE_INHIBITION] and twice.commands == () and twice.reason == m.IGNORED_NO_CHANGE
    idle = m.confirm_release(done.transaction, done.reservation, T0 + 801.0, token=1, released=True)
    again = m.observe(idle.transaction, idle.reservation, off, T0 + 802.0)
    assert again.commands == () and again.reason == m.IGNORED_IDLE


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
    step = m.observe(s.transaction, s.reservation, obs(1, override_off=False, **OFF), s.transaction.phase_started_at + 3.0)
    assert step.transaction.phase == m.PHASE_OVERRIDE_HOLD and step.commands == () and step.transaction.inhibition_owned


def test_override_during_the_release_freezes_it():
    s = to_releasing()
    step = m.observe(s.transaction, s.reservation, obs(1, override_off=False, **OFF), T0 + 900.0)
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


def test_protective_and_resume_returns_run_under_an_active_override():
    s = to_running()
    for override_off in (False, None):
        step = m.request_return(s.transaction, s.reservation, T0 + 900.0, obs(1, override_off=override_off), reason=m.FAILURE_OVERRIDE_PROTECTION, override_exempt=True)
        assert step.transaction.phase == m.PHASE_RETURNING and kinds(step) == [m.CMD_PDC_OFF] and step.transaction.override_exempt
        # the exemption survives the wait: the override on does not freeze this return
        done = m.observe(step.transaction, step.reservation, obs(1, override_off=override_off, **OFF), T0 + 905.0)
        assert done.transaction.phase == m.PHASE_RELEASING and kinds(done) == [m.CMD_RELEASE_INHIBITION]
        idle = m.confirm_release(done.transaction, done.reservation, T0 + 906.0, token=1, released=True)
        assert idle.transaction.phase == m.PHASE_IDLE and idle.transaction.override_exempt is False
    # a return already under way becomes exempt when the protection arrives later
    plain = m.request_return(s.transaction, s.reservation, T0 + 900.0, obs(1))
    upgraded = m.request_return(plain.transaction, plain.reservation, T0 + 901.0, obs(1, override_off=False), override_exempt=True)
    assert upgraded.transaction.override_exempt and upgraded.commands == ()


def test_adopt_manual_session_goes_straight_to_override_hold():
    step = m.adopt_manual(tx0(), FREE, T0, radiator_inhibited_before=False)
    tx = step.transaction
    assert step.accepted and tx.phase == m.PHASE_OVERRIDE_HOLD and tx.manual and tx.inhibition_owned and tx.reservation_held and tx.token == 1
    assert kinds(step) == [m.CMD_INHIBIT_RADIATOR] and step.reservation == m.PdcReservation(ROOM, 1)
    assert m.observe(tx, step.reservation, obs(1), T0 + 5.0).reason == m.IGNORED_HOLD
    # the manual session can be settled by the inhibition result without abandoning anything
    dropped = m.record_inhibition_result(tx, step.reservation, T0 + 1.0, token=1, inhibited_after=True, transitioned=False)
    assert dropped.transaction.phase == m.PHASE_OVERRIDE_HOLD and not dropped.transaction.inhibition_owned and dropped.commands == ()
    # a foreign inhibition is not claimed, an unknown one refuses, a busy machine refuses
    foreign = m.adopt_manual(tx0(), FREE, T0, radiator_inhibited_before=True)
    assert foreign.accepted and not foreign.transaction.inhibition_owned and foreign.commands == ()
    assert not m.adopt_manual(tx0(), FREE, T0, radiator_inhibited_before=None).accepted
    assert m.adopt_manual(tx, step.reservation, T0, radiator_inhibited_before=False).reason == m.DENIED_BUSY
    # a manual session does not need the reservation (the other PDC is excluded by the managed flag)
    taken = m.adopt_manual(tx0(OTHER), step.reservation, T0, radiator_inhibited_before=False)
    assert taken.accepted and not taken.transaction.reservation_held and taken.reservation == step.reservation
    # ending the session is the normal return
    back = m.request_return(tx, step.reservation, T0 + 600.0, obs(1, **OFF), reason=m.FAILURE_OVERRIDE_ENDED)
    assert back.transaction.phase == m.PHASE_RELEASING and kinds(back) == [m.CMD_RELEASE_INHIBITION]


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
    old = m.observe(tx, step.reservation, obs(1, **OFF), T0 + 5001.0)
    assert old.reason == m.IGNORED_STALE and old.commands == ()
    done = m.observe(tx, step.reservation, obs(2, **OFF), T0 + 5002.0)
    assert done.transaction.phase == m.PHASE_RELEASING and kinds(done) == [m.CMD_RELEASE_INHIBITION]
    idle = m.confirm_release(done.transaction, done.reservation, T0 + 5003.0, token=2, released=True)
    assert idle.reservation == FREE


def test_recover_a_pending_release_retries_it_only_if_off_is_still_confirmed():
    s = to_releasing()
    again = m.recover(s.transaction, s.reservation, T0 + 5000.0, obs(1, **OFF))
    assert again.transaction.phase == m.PHASE_RELEASING and again.transaction.token == 2 and kinds(again) == [m.CMD_RELEASE_INHIBITION]
    back = m.recover(s.transaction, s.reservation, T0 + 5000.0, obs(1, split_mode="heat", pdc_mode="heat"))
    assert back.transaction.phase == m.PHASE_RETURNING and m.CMD_RELEASE_INHIBITION not in kinds(back)


def test_recover_a_heat_command_in_flight_before_the_restart_waits_for_nothing_after_the_settle_time():
    w = to_waiting_heat()
    step = m.recover(w.transaction, w.reservation, w.transaction.heat_sent_at + 3000.0, obs(1, **OFF))
    assert step.transaction.phase == m.PHASE_RELEASING  # long ago: settled


def test_recover_idle_and_unknown_override_do_nothing_dangerous():
    assert m.recover(tx0(), FREE, T0, obs(0)).transaction == tx0()
    s = to_waiting_heat()
    unknown = m.recover(s.transaction, s.reservation, T0 + 100.0, obs(1, override_off=None))
    assert unknown.transaction.phase == m.PHASE_OVERRIDE_HOLD and unknown.commands == () and unknown.transaction.inhibition_owned
    no_obs = m.recover(s.transaction, s.reservation, T0 + 100.0, None)
    assert no_obs.transaction.phase == m.PHASE_OVERRIDE_HOLD and no_obs.commands == ()
    running = to_running()
    assert m.recover(running.transaction, running.reservation, T0 + 100.0, obs(1, split_mode=None, pdc_mode=None)).transaction.phase == m.PHASE_RETURNING
    manual = m.adopt_manual(tx0(), FREE, T0, radiator_inhibited_before=False)
    kept = m.recover(manual.transaction, manual.reservation, T0 + 100.0, obs(1, override_off=False))
    assert kept.transaction == manual.transaction and kept.commands == ()
    ended = m.recover(manual.transaction, manual.reservation, T0 + 100.0, obs(1, override_off=True, **OFF))
    assert ended.transaction.phase == m.PHASE_RELEASING


# --- persistence --------------------------------------------------------------------------------


def test_transactions_round_trip_through_plain_data_in_every_phase():
    seen = set()
    for ctor in (start, to_waiting_target, to_waiting_heat, to_running, to_returning, to_releasing, to_idle):
        step = ctor()
        data = m.transaction_to_dict(step.transaction)
        assert m.transaction_from_dict(data, ROOM) == step.transaction
        seen.add(step.transaction.phase)
    manual = m.adopt_manual(tx0(), FREE, T0, radiator_inhibited_before=False)
    assert m.transaction_from_dict(m.transaction_to_dict(manual.transaction), ROOM) == manual.transaction
    assert seen >= {m.PHASE_WAITING_RADIATOR, m.PHASE_WAITING_TARGET, m.PHASE_WAITING_HEAT, m.PHASE_RUNNING, m.PHASE_RETURNING, m.PHASE_RELEASING, m.PHASE_IDLE}
    held = m.PdcReservation(ROOM, 3)
    assert m.reservation_from_dict(m.reservation_to_dict(held)) == held and m.reservation_from_dict(m.reservation_to_dict(FREE)) == FREE


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema_version=2),
        lambda d: d.update(room_key="other"),
        lambda d: d.update(phase="sleeping"),
        lambda d: d.update(token=-1),
        lambda d: d.update(token=True),
        lambda d: d.update(inhibition_owned="yes"),
        lambda d: d.update(deadline=float("nan")),
        lambda d: d.update(release_attempts=-3),
        lambda d: d.update(inhibited_before="no"),
    ],
)
def test_invalid_persisted_data_is_rejected_not_guessed(mutate):
    data = m.transaction_to_dict(to_running().transaction)
    mutate(data)
    with pytest.raises((ValueError, TypeError)):
        m.transaction_from_dict(data, ROOM)
    with pytest.raises(ValueError):
        m.transaction_from_dict([], ROOM)
    with pytest.raises(ValueError):
        m.reservation_from_dict("x")
    with pytest.raises(ValueError):
        m.reservation_from_dict({"holder": ROOM, "token": None})


# --- safety invariants (random walk) ------------------------------------------------------------


def test_random_walk_never_breaks_the_four_safety_invariants():
    """Whatever the observations and the order of requests: the PDC is never commanded to HEAT without
    a confirmed radiator stop, a confirmed target and an owned inhibition; the inhibition is released
    only if owned and only against an observation with split and PDC ``off``; a foreign inhibition is
    never released; a stale token never produces a command."""
    rng = random.Random(20261124)
    modes = ("off", "heat", "cool", "unknown", "unavailable", None)
    bools = (True, False, None)
    for _ in range(300):
        tx, res = tx0(), FREE
        now = T0
        foreign_walk = rng.random() < 0.3
        for _ in range(60):
            now += rng.choice([0.0, 1.0, 5.0, 19.0, 20.0, 25.0, 40.0, 130.0])
            token = tx.token if rng.random() < 0.9 else rng.choice([0, tx.token + 1, 99])
            o = m.PdcObservation(
                token,
                override_off=rng.choice([True, True, True, False, None]),
                split_mode=rng.choice(modes),
                pdc_mode=rng.choice(modes),
                radiator_stopped=rng.choice(bools),
                target_confirmed=rng.choice(bools),
                conditions_ok=rng.choice(bools),
                radiator_inhibited=rng.choice(bools),
            )
            action = rng.choice(["observe", "observe", "observe", "start", "return", "cancel", "recover", "release", "result", "adopt", "exempt"])
            before = tx
            if action == "observe":
                step = m.observe(tx, res, o, now)
            elif action == "start":
                step = m.request_start(
                    tx, res, now, override_off=rng.choice(bools), other_pdc_busy=rng.choice(bools), radiator_inhibited_before=rng.choice([False, False, True, None])
                )
            elif action == "return":
                step = m.request_return(tx, res, now, o)
            elif action == "exempt":
                step = m.request_return(tx, res, now, o, override_exempt=True)
            elif action == "cancel":
                step = m.cancel(tx, res, now, o)
            elif action == "recover":
                step = m.recover(tx, res, now, o)
            elif action == "release":
                step = m.confirm_release(tx, res, now, token=token, released=rng.choice(bools))
            elif action == "adopt":
                step = m.adopt_manual(tx, res, now, radiator_inhibited_before=rng.choice(bools))
            else:
                step = m.record_inhibition_result(tx, res, now, token=token, inhibited_after=rng.choice(bools), transitioned=rng.choice(bools), obs=o)
            if foreign_walk and action == "start" and step.accepted:
                step = dataclasses.replace(step, transaction=dataclasses.replace(step.transaction, inhibition_owned=False))
            for cmd in step.commands:
                assert cmd.token == step.transaction.token and cmd.room_key == ROOM
                if cmd.kind == m.CMD_PDC_HEAT:
                    assert action == "observe" and o.target_confirmed is True and o.radiator_stopped is True and o.conditions_ok is True and before.inhibition_owned
                    assert before.phase == m.PHASE_WAITING_TARGET and o.token == before.token
                if cmd.kind == m.CMD_RELEASE_INHIBITION:
                    owner = step.transaction.inhibition_owned
                    assert owner, "release without ownership"
                    if action == "release":
                        pass
                    else:
                        assert o.split_mode == "off" and o.pdc_mode == "off", (action, o)
                if cmd.kind == m.CMD_SET_TARGET:
                    assert o.radiator_stopped is True and o.conditions_ok is True and before.inhibition_owned
            if step.transaction.phase == m.PHASE_IDLE:
                assert not step.transaction.inhibition_owned and not step.transaction.reservation_held
            assert step.transaction.token >= before.token
            if step.transaction.reservation_held:
                assert step.reservation == m.PdcReservation(ROOM, step.transaction.token) or step.transaction.manual or step.reservation.holder in (ROOM, None)
            tx, res = step.transaction, step.reservation
            if tx.phase == m.PHASE_IDLE and rng.random() < 0.5:
                res = FREE if res.holder in (None, ROOM) else res


# --- validation and purity ----------------------------------------------------------------------


def test_models_validate_their_inputs_and_are_frozen():
    for bad in ({"radiator_timeout_s": -1.0}, {"heat_timeout_s": float("nan")}, {"off_timeout_s": True}, {"direct_off_timeout_s": float("inf")}, {"release_retry_s": -1.0}):
        with pytest.raises((TypeError, ValueError)):
            m.PdcTransactionConfig(**bad)
    with pytest.raises(ValueError):
        m.PdcTransaction("")
    with pytest.raises(ValueError):
        m.PdcTransaction(ROOM, token=-1)
    with pytest.raises(ValueError):
        m.PdcTransaction(ROOM, phase="nope")
    with pytest.raises(TypeError):
        m.PdcObservation("1")
    with pytest.raises(TypeError):
        m.PdcObservation(1, override_off="yes")
    with pytest.raises(TypeError):
        m.PdcObservation(1, radiator_inhibited="no")
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
    assert imported <= {"__future__", "math", "dataclasses", "typing", "collections"}, imported
