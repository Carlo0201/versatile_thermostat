"""Pure regressions required for the first boiler-control release."""

import pytest

from .test_heating_boiler_command import OWNER_OK, T0, cmd


def test_restart_keeps_exhausted_budget_even_after_the_action_was_superseded():
    record = cmd.CommandRecord("heat", cmd.STATUS_SUPERSEDED, 4, T0)
    memory = (cmd.ModeBudget("heat", 4, T0),)
    restored, _ = cmd.restart_record(cmd.record_from_dict(cmd.record_to_dict(record)))
    budget = cmd.restore_budget(cmd.budget_to_list(memory), restored, T0 + 20)
    inputs = cmd.CommandInputs(T0 + 20, "heat", "off", OWNER_OK, True)
    result = cmd.step(restored, inputs, budget)
    assert result.send is None and result.control_status == cmd.CONTROL_COMMAND_FAILED
    assert result.wake_at == T0 + cmd.BUDGET_WINDOW_S


@pytest.mark.parametrize("saved", [[], [{"mode": "off", "attempts": 2, "last_at": T0, "retry_at": None}]])
def test_pending_send_saved_before_the_budget_update_is_counted_after_restart(saved):
    record = cmd.CommandRecord("off", cmd.STATUS_PENDING, 2, T0)
    restored, _ = cmd.restart_record(cmd.record_from_dict(cmd.record_to_dict(record)))
    memory = cmd.restore_budget(saved, restored, T0 + 5)
    assert memory[0].attempts == 2
    early = cmd.step(restored, cmd.CommandInputs(T0 + 6, "off", "heat", OWNER_OK, True), memory)
    assert early.send is None
    later = cmd.step(restored, cmd.CommandInputs(T0 + 66, "off", "heat", OWNER_OK, True), memory)
    assert later.send == "off" and later.attempt == 3


def test_valid_budget_preserves_its_backoff_and_expires_normally():
    memory = (cmd.ModeBudget("off", 2, T0, T0 + 120),)
    restored = cmd.restore_budget(cmd.budget_to_list(memory), None, T0 + 10)
    assert restored == memory
    result = cmd.step(None, cmd.CommandInputs(T0 + 20, "off", "heat", OWNER_OK, True), restored)
    assert result.send is None and result.wake_at == T0 + 120
    assert cmd.restore_budget(cmd.budget_to_list(memory), None, T0 + cmd.BUDGET_WINDOW_S) == ()


@pytest.mark.parametrize("value", [{}, "bad", [{"mode": "heat", "attempts": True, "last_at": T0}], [{"mode": "off", "attempts": 1, "last_at": T0 + 50}]])
def test_malformed_budget_cannot_grant_a_new_allowance(value):
    restored = cmd.restore_budget(value, None, T0)
    assert restored and all(item.attempts == cmd.MAX_SENDS for item in restored)


def test_unknown_old_budget_field_is_optional_and_a_future_clock_is_conservative():
    assert cmd.restore_budget(None, None, T0) == ()
    record = cmd.CommandRecord("heat", cmd.STATUS_UNCONFIRMED, 1, T0 + 100)
    restored = cmd.restore_budget(None, record, T0)
    assert restored == (cmd.ModeBudget("heat", cmd.MAX_SENDS, T0),)


def test_shutdown_cancels_the_pending_request_but_persists_spent_sends():
    record = cmd.CommandRecord("off", cmd.STATUS_PENDING, 3, T0)
    stop = cmd.step(record, cmd.CommandInputs(T0 + 5, "off", "heat", OWNER_OK, True, shutting_down=True))
    assert stop.record.status == cmd.STATUS_CANCELLED and stop.send is None
    saved = cmd.budget_to_list(stop.memory)
    restored = cmd.restore_budget(saved, cmd.record_from_dict(cmd.record_to_dict(stop.record)), T0 + 10)
    result = cmd.step(stop.record, cmd.CommandInputs(T0 + 10, "off", "heat", OWNER_OK, True), restored)
    assert result.attempt == 4  # never a fresh first attempt after shutdown
