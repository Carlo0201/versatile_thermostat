"""Pure tests of the boiler command engine (M2-B).

The command module, the collector and the M1 policy are loaded by file path
under private names, so the component package (whose ``__init__`` imports Home
Assistant) is never executed. They run with only pytest installed::

    python -m pytest -q --noconftest --import-mode=importlib tests/test_heating_boiler_command.py

``VTHERM_COMMAND_PATH`` points the tests to another copy of the module: the
mutation check of M2-B runs this file against mutated copies.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_DIR = REPO_ROOT / "custom_components" / "versatile_thermostat"
COMMAND_PATH = Path(os.environ.get("VTHERM_COMMAND_PATH", COMPONENT_DIR / "heating_boiler_command.py"))


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


cmd = _load("_vtherm_pure_heating_boiler_command_under_test", COMMAND_PATH)
policy = _load("_vtherm_pure_heating_boiler_policy_for_command", COMPONENT_DIR / "heating_boiler_policy.py")
collector = _load("_vtherm_pure_heating_boiler_collector_for_command", COMPONENT_DIR / "heating_boiler_collector.py")

T0 = 1_800_000_000.0

OWNER_OK = cmd.Ownership(True, None, (cmd.ExternalOwner("automation.caldaia_controllo", "off"),))
OWNER_NO = cmd.Ownership(False, cmd.BLOCK_EXTERNAL_OWNER, (cmd.ExternalOwner("automation.caldaia_controllo", "on"),))


class Harness:
    """Drives ``step`` like the manager does and keeps the record and the events."""

    def __init__(self) -> None:
        self.record = None
        self.events: list = []
        self.send_id = 0
        self.t = T0
        self.memory = ()

    def inputs(self, action, master, *, ownership=OWNER_OK, settled=True, shutting=False, at=None):
        return cmd.CommandInputs(now=self.t if at is None else at, action=action, master_mode=master, ownership=ownership, settled=settled, shutting_down=shutting)

    def step(self, action, master, **kwargs):
        result = cmd.step(self.record, self.inputs(action, master, **kwargs), self.memory)
        self.record = result.record
        self.memory = result.memory
        self.events.extend(result.events)
        return result

    def begin(self, result, master):
        """The manager revalidated and starts the send."""
        self.send_id += 1
        self.record, event = cmd.start_send(self.record, mode=result.send, attempt=result.attempt, now=self.t, send_id=self.send_id, base_master=master)
        self.events.append(event)
        return event

    def complete(self):
        self.record = cmd.on_service_completed(self.record, now=self.t, send_id=self.send_id)

    def fail(self, error="service_error:Boom"):
        self.record, events = cmd.on_service_failed(self.record, now=self.t, send_id=self.send_id, error=error)
        self.events.extend(events)

    def kinds(self):
        return [event.kind for event in self.events]

    def run_send(self, action, master, *, complete=True):
        """step -> start_send -> service completed, at the current time."""
        result = self.step(action, master)
        assert result.send == action
        self.begin(result, master)
        if complete:
            self.complete()
        return result


# ---------------------------------------------------------------------------
# Constants and contract with the other modules
# ---------------------------------------------------------------------------


def test_timing_constants_are_the_decided_ones():
    assert cmd.MAX_SENDS == 4  # one send and three retries
    assert cmd.RETRY_DELAYS_S == (60.0, 120.0, 300.0)
    assert cmd.CONFIRMATION_TIMEOUT_S == 60.0
    assert cmd.SERVICE_TIMEOUT_S == 30.0
    assert cmd.HEAT_PERSIST_WAIT_S == 5.0
    assert cmd.SETTLE_MIN_INTERVAL_S == 30.0
    assert cmd.STORE_VERSION == 2


def test_modes_match_the_policy_and_the_collector():
    assert cmd.COMMAND_HEAT == policy.BOILER_ACTION_HEAT and cmd.COMMAND_OFF == policy.BOILER_ACTION_OFF and cmd.ACTION_NONE == policy.BOILER_ACTION_NONE
    assert cmd.MASTER_KNOWN_MODES == (policy.BOILER_MASTER_OFF, policy.BOILER_MASTER_HEAT)
    assert cmd.OWNER_CONTROLLER == "controller"


def test_command_diagnostics_keys_match_the_collector_defaults():
    fields = cmd.command_diagnostics(None, cmd.Ownership(False, None, ()), cmd.CONTROL_INACTIVE)
    assert set(fields) == set(collector.DEFAULT_CONTROL_FIELDS)
    assert fields == collector.DEFAULT_CONTROL_FIELDS
    assert cmd.CONTROL_STATUSES  # documented set


def test_module_is_pure():
    source = COMMAND_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "math", "collections", "dataclasses", "typing"}, imported
    for forbidden in ("utcnow", "datetime.now", "time.time", "async_call", "create_task"):
        assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# Ownership interlock
# ---------------------------------------------------------------------------


def owners(*states):
    return [cmd.ExternalOwner(f"automation.a{index}", state) for index, state in enumerate(states)]


def ownership(**changes):
    values = dict(control_requested=True, active=True, external_owners=owners("off"), master_mode="off", evaluation_valid=True)
    values.update(changes)
    return cmd.evaluate_ownership(**values)


def test_ownership_requires_every_condition():
    assert ownership().is_owner and ownership().block_reason is None
    assert ownership(master_mode="heat").is_owner
    assert ownership(control_requested=False).block_reason == cmd.BLOCK_NOT_CONTROL
    assert ownership(active=False).block_reason == cmd.BLOCK_NOT_ACTIVE
    assert ownership(evaluation_valid=False).block_reason == cmd.BLOCK_NO_VALID_EVALUATION


@pytest.mark.parametrize("state", ["on", "unknown", "unavailable", None, "", "OFF", "Off", " off", "off "])
def test_only_the_exact_off_state_releases_the_ownership(state):
    result = ownership(external_owners=owners("off", state))
    assert not result.is_owner and result.block_reason == cmd.BLOCK_EXTERNAL_OWNER
    assert [owner.state for owner in result.external_owners] == ["off", state]


def test_missing_or_empty_external_controller_never_grants_the_ownership():
    assert ownership(external_owners=owners(None)).block_reason == cmd.BLOCK_EXTERNAL_OWNER
    assert ownership(external_owners=[]).block_reason == cmd.BLOCK_NO_EXTERNAL_OWNER


@pytest.mark.parametrize("master", [None, "unknown", "unavailable", "auto", "heat_cool", "", "HEAT"])
def test_master_must_have_a_known_mode(master):
    assert ownership(master_mode=master).block_reason == cmd.BLOCK_MASTER_UNKNOWN


def test_ownership_precedence_follows_the_specification_order():
    assert ownership(control_requested=False, active=False, external_owners=owners("on"), master_mode=None).block_reason == cmd.BLOCK_NOT_CONTROL
    assert ownership(active=False, external_owners=owners("on"), master_mode=None).block_reason == cmd.BLOCK_NOT_ACTIVE
    assert ownership(external_owners=owners("on"), master_mode=None).block_reason == cmd.BLOCK_EXTERNAL_OWNER
    assert ownership(master_mode=None, evaluation_valid=False).block_reason == cmd.BLOCK_MASTER_UNKNOWN


def test_control_status_when_blocked():
    expected = {
        cmd.BLOCK_NOT_CONTROL: cmd.CONTROL_INACTIVE,
        cmd.BLOCK_NOT_ACTIVE: cmd.CONTROL_INACTIVE,
        cmd.BLOCK_EXTERNAL_OWNER: cmd.CONTROL_BLOCKED_EXTERNAL_OWNER,
        cmd.BLOCK_NO_EXTERNAL_OWNER: cmd.CONTROL_BLOCKED_EXTERNAL_OWNER,
        cmd.BLOCK_MASTER_UNKNOWN: cmd.CONTROL_BLOCKED_MASTER_UNKNOWN,
        cmd.BLOCK_NO_VALID_EVALUATION: cmd.CONTROL_STARTING,
    }
    for reason, status in expected.items():
        assert cmd.control_status_when_blocked(cmd.Ownership(False, reason, ())) == status
    assert cmd.CONTROL_BLOCKED_EXTERNAL_OWNER == "blocked_external_owner"


# ---------------------------------------------------------------------------
# Settling after setup / reload
# ---------------------------------------------------------------------------


def settle(state, now, valid=True, action="none"):
    return cmd.update_settling(state, now=now, valid=valid, action=action)


def test_settling_needs_two_valid_evaluations_thirty_seconds_apart():
    state = cmd.SettlingState()
    state = settle(state, T0)
    assert not state.settled and state.anchor_at == T0
    for offset in (1, 15, 29.999):
        state = settle(state, T0 + offset)
        assert not state.settled
    state = settle(state, T0 + 30)  # inclusive on the limit
    assert state.settled


def test_settling_restarts_on_an_invalid_evaluation_or_another_action():
    state = settle(cmd.SettlingState(), T0, action="off")
    state = settle(state, T0 + 20, valid=False)
    assert state == cmd.SettlingState()
    state = settle(state, T0 + 21, action="off")
    state = settle(state, T0 + 40, action="none")  # the action changed: new anchor
    assert not state.settled and state.anchor_at == T0 + 40 and state.anchor_action == "none"
    state = settle(state, T0 + 70, action="none")
    assert state.settled


def test_settling_with_a_clock_that_went_backwards_restarts():
    state = settle(cmd.SettlingState(), T0)
    state = settle(state, T0 - 100)
    assert not state.settled and state.anchor_at == T0 - 100


def test_settled_is_latched():
    state = cmd.SettlingState(settled=True)
    assert settle(state, T0, valid=False, action="off") is state


def test_off_is_not_exempt_from_the_settling():
    h = Harness()
    result = h.step("off", "heat", settled=False)
    assert result.send is None and result.control_status == cmd.CONTROL_SETTLING and h.record is None
    result = h.step("off", "heat", settled=True)
    assert result.send == "off"


# ---------------------------------------------------------------------------
# New requests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action, master", [("heat", "off"), ("off", "heat")])
def test_a_command_is_sent_only_when_the_master_differs(action, master):
    result = Harness().step(action, master)
    assert result.send == action and result.attempt == 1 and result.control_status == cmd.CONTROL_READY and result.record is None  # pending only once the send really starts


@pytest.mark.parametrize("action, master", [("heat", "heat"), ("off", "off")])
def test_confirmation_already_true_before_the_command_sends_nothing(action, master):
    result = Harness().step(action, master)
    assert result.send is None and result.control_status == cmd.CONTROL_READY and result.events == ()


@pytest.mark.parametrize("master", ["off", "heat", None, "unknown", "unavailable"])
def test_action_none_never_sends(master):
    assert Harness().step("none", master).send is None


@pytest.mark.parametrize("master", [None, "unknown", "unavailable", "auto", "heat_cool"])
def test_unknown_master_never_sends(master):
    h = Harness()
    for action in ("heat", "off"):
        # also with a (wrongly) satisfied ownership: the command gate checks the mode itself
        assert h.step(action, master).send is None
    result = h.step("off", master, ownership=cmd.Ownership(False, cmd.BLOCK_MASTER_UNKNOWN, ()))
    assert result.send is None and result.control_status == cmd.CONTROL_BLOCKED_MASTER_UNKNOWN


def test_not_the_owner_sends_nothing_and_reports_why():
    result = Harness().step("off", "heat", ownership=OWNER_NO)
    assert result.send is None and result.control_status == cmd.CONTROL_BLOCKED_EXTERNAL_OWNER and result.wake_at is None


def test_shutting_down_sends_nothing():
    result = Harness().step("off", "heat", shutting=True)
    assert result.send is None and result.control_status == cmd.CONTROL_INACTIVE


def test_inputs_are_validated():
    with pytest.raises(ValueError):
        cmd.CommandInputs(now=float("nan"), action="off", master_mode="heat", ownership=OWNER_OK, settled=True)
    with pytest.raises(ValueError):
        cmd.CommandInputs(now=T0, action="heat_cool", master_mode="heat", ownership=OWNER_OK, settled=True)


# ---------------------------------------------------------------------------
# Service completion is not a confirmation
# ---------------------------------------------------------------------------


def test_service_completion_is_pending_not_confirmed():
    h = Harness()
    result = h.step("heat", "off")
    event = h.begin(result, "off")
    assert event == cmd.CommandEvent(cmd.EVENT_SENT, "heat", 1) and h.record.status == cmd.STATUS_SENDING and h.record.at == T0
    assert h.record.service_completed_at is None and h.record.last_confirmation_at is None
    h.t += 2
    h.complete()
    assert h.record.status == cmd.STATUS_PENDING and h.record.service_completed_at == T0 + 2
    assert h.record.last_confirmation_at is None and h.record.base_master == "off"
    diag = cmd.command_diagnostics(h.record, OWNER_OK, cmd.CONTROL_PENDING)
    assert diag["command_status"] == "pending" and diag["commanded_mode"] == "heat" and diag["service_completed_at"] == T0 + 2
    assert diag["last_command_at"] == T0 and diag["last_confirmation_at"] is None and diag["command_attempts"] == 1


def test_confirmation_only_from_the_observed_master():
    h = Harness()
    h.run_send("heat", "off")
    for master in ("off", "unknown", "unavailable", None, "auto"):
        h.t += 1
        result = h.step("heat", master)
        assert h.record.status == cmd.STATUS_PENDING and result.send is None
    h.t += 1
    result = h.step("none", "heat")  # after the heat the M1 action is none (maintain)
    assert h.record.status == cmd.STATUS_CONFIRMED and h.record.last_confirmation_at == h.t and h.kinds() == ["sent", "confirmed"]
    assert result.control_status == cmd.CONTROL_CONFIRMED and result.send is None and result.wake_at is None


def test_confirmation_timeout_59_and_60_seconds():
    for offset, failed in ((59.999, False), (60.0, True), (61.0, True)):
        h = Harness()
        h.run_send("heat", "off")
        h.t += offset
        result = h.step("heat", "off")
        assert (h.record.status == cmd.STATUS_FAILED) is failed, offset
        if failed:
            assert h.record.error == cmd.ERROR_CONFIRMATION_TIMEOUT and h.record.retry_at == h.t + 60
            assert h.kinds() == ["sent", "failed"] and result.send is None and result.wake_at == h.record.retry_at
        else:
            assert result.wake_at == T0 + 60.0 and result.events == ()


def test_confirmation_at_exactly_sixty_seconds_still_counts():
    h = Harness()
    h.run_send("heat", "off")
    h.t += 60.0
    result = h.step("none", "heat")
    assert h.record.status == cmd.STATUS_CONFIRMED and h.kinds() == ["sent", "confirmed"] and result.send is None


def test_timeout_is_measured_from_the_service_completion():
    h = Harness()
    result = h.step("heat", "off")
    h.begin(result, "off")
    h.t += 25  # slow service
    h.complete()
    h.t += 59
    h.step("heat", "off")
    assert h.record.status == cmd.STATUS_PENDING  # 84 s after the start, 59 s after the completion
    h.t += 1
    h.step("heat", "off")
    assert h.record.status == cmd.STATUS_FAILED


def test_confirmed_while_the_service_is_still_running():
    h = Harness()
    result = h.step("heat", "off")
    h.begin(result, "off")
    h.t += 3
    h.step("none", "heat")  # the state event arrives before the service returns
    assert h.record.status == cmd.STATUS_CONFIRMED
    h.t += 1
    h.complete()
    assert h.record.status == cmd.STATUS_CONFIRMED and h.record.service_completed_at == h.t
    h.fail()  # a late error does not undo a confirmation
    assert h.record.status == cmd.STATUS_CONFIRMED


# ---------------------------------------------------------------------------
# Retries, backoff, give up
# ---------------------------------------------------------------------------


def test_four_sends_with_growing_waits_then_command_failed():
    h = Harness()
    starts = []
    expected_waits = [60.0, 120.0, 300.0]
    for attempt in range(1, 5):
        result = h.step("heat", "off")
        assert result.send == "heat" and result.attempt == attempt
        h.begin(result, "off")
        starts.append(h.t)
        h.t += 1
        h.complete()
        h.t += 59.999
        assert h.step("heat", "off").send is None and h.record.status == cmd.STATUS_PENDING
        h.t += 0.001  # 60 s after the completion
        result = h.step("heat", "off")
        assert h.record.status == cmd.STATUS_FAILED
        if attempt < 4:
            wait = expected_waits[attempt - 1]
            assert h.record.retry_at == h.t + wait and result.wake_at == h.record.retry_at and result.control_status == cmd.CONTROL_PENDING
            h.t += wait - 0.001
            assert h.step("heat", "off").send is None  # not due yet
            h.t += 0.001
        else:
            assert h.record.retry_at is None and h.record.is_exhausted
            assert result.control_status == cmd.CONTROL_COMMAND_FAILED and result.wake_at is None
    assert h.kinds().count("sent") == 4 and h.kinds().count("failed") == 4 and h.kinds().count("exhausted") == 1
    assert h.record.attempts == 4
    # no fifth send, however long we wait, as long as nothing changes
    for _ in range(10):
        h.t += 600
        result = h.step("heat", "off")
        assert result.send is None and result.control_status == cmd.CONTROL_COMMAND_FAILED
    assert h.kinds().count("sent") == 4
    assert [round(b - a) for a, b in zip(starts, starts[1:])] == [1 + 60 + 60, 1 + 60 + 120, 1 + 60 + 300]


def test_service_failure_follows_the_same_backoff():
    h = Harness()
    for attempt, wait in ((1, 60.0), (2, 120.0), (3, 300.0)):
        result = h.step("off", "heat")
        assert result.send == "off" and result.attempt == attempt
        h.begin(result, "heat")
        h.t += 5
        h.fail("service_timeout" if attempt == 2 else "service_error:HomeAssistantError")
        assert h.record.status == cmd.STATUS_FAILED and h.record.retry_at == h.t + wait
        h.t += wait
    result = h.step("off", "heat")
    assert result.send == "off" and result.attempt == 4
    h.begin(result, "heat")
    h.fail("service_timeout")
    assert h.record.is_exhausted and h.kinds()[-2:] == ["failed", "exhausted"]
    assert h.record.error == "service_timeout"


def test_the_limit_is_per_requested_mode_until_the_decision_changes():
    h = Harness()
    for _ in range(4):
        result = h.step("heat", "off")
        h.begin(result, "off")
        h.fail()
        h.t += 301
    assert h.record.is_exhausted
    assert h.step("heat", "off").send is None
    # the M1 action changes: the exhausted request is released...
    result = h.step("none", "off")
    assert h.record.status == cmd.STATUS_SUPERSEDED and result.send is None and result.control_status == cmd.CONTROL_READY
    # ...but the spent budget of the mode is remembered for the window (review M2-B I-2)
    result = h.step("heat", "off")
    assert result.send is None and result.control_status == cmd.CONTROL_COMMAND_FAILED
    h.t += cmd.BUDGET_WINDOW_S - 1
    assert h.step("heat", "off").send is None
    h.t += 1
    result = h.step("heat", "off")
    assert result.send == "heat" and result.attempt == 1  # the window is over: a fresh budget


def test_flapping_action_never_exceeds_four_sends_per_mode_in_the_window():
    """Review M2-B I-2: failing service and an action that oscillates through none."""
    for mode, master in (("heat", "off"), ("off", "heat")):
        h = Harness()
        sends = 0
        end = T0 + cmd.BUDGET_WINDOW_S - 1
        while h.t < end:
            for action in (mode, "none"):
                result = h.step(action, master)
                if result.send:
                    sends += 1
                    h.begin(result, master)
                    h.fail()
                h.t += 2
        assert sends <= cmd.MAX_SENDS, (mode, sends)
        assert sends >= 2  # the backoff spaces them, it does not forbid them


def test_a_confirmation_or_the_opposite_command_resets_the_budget():
    h = Harness()
    result = h.step("heat", "off")
    h.begin(result, "off")
    h.fail()
    h.step("none", "off")
    assert h.step("heat", "off").send is None  # backoff of the first failure still running
    h.t += 61
    result = h.step("heat", "off")
    assert result.send == "heat" and result.attempt == 2
    h.begin(result, "off")
    h.step("none", "heat")  # the boiler really went on: confirmed
    assert h.memory == ()
    # an opposite command forgets the other mode
    result = h.step("off", "heat")
    h.begin(result, "heat")
    h.fail()
    result = h.step("none", "heat")
    assert [b.mode for b in h.memory] == ["off"]
    h.t += 61
    h.step("none", "off")
    result = h.step("heat", "off")
    assert result.send == "heat" and result.attempt == 1
    h.begin(result, "off")
    h.step("heat", "off")
    assert [b.mode for b in h.memory] == ["heat"] and h.memory[0].attempts == 1


def test_exhausted_request_is_released_when_the_master_goes_unavailable():
    """Review M2-B I-1: unavailable -> heat must not leave the boiler on for ever."""
    h = Harness()
    for _ in range(4):
        result = h.step("off", "heat")
        h.begin(result, "heat")
        h.fail()
        h.t += 301
    assert h.record.is_exhausted and h.step("off", "heat").send is None
    blocked = cmd.Ownership(False, cmd.BLOCK_MASTER_UNKNOWN, OWNER_OK.external_owners)
    result = h.step("off", "unavailable", ownership=blocked)
    assert h.record.status == cmd.STATUS_SUPERSEDED and h.events[-1].detail == "master_unavailable"
    assert result.control_status == cmd.CONTROL_BLOCKED_MASTER_UNKNOWN
    h.t += 40
    result = h.step("off", "heat")  # the master is back in heat, the action is still off
    assert result.send is None and result.wake_at == h.t - 40 + cmd.RELEASE_WAIT_S  # a short wait first
    h.t += cmd.RELEASE_WAIT_S - 40
    result = h.step("off", "heat")
    assert result.send == "off" and result.attempt == 1  # a fresh budget with its own backoff
    h.begin(result, "heat")
    h.fail()
    assert h.record.retry_at == h.t + 60


def test_exhausted_request_is_released_when_the_master_changes_state():
    h = Harness()
    for _ in range(4):
        result = h.step("heat", "off")
        h.begin(result, "off")
        h.fail()
        h.t += 301
    assert h.record.is_exhausted
    assert h.step("heat", "heat").send is None
    assert h.record.status == cmd.STATUS_CONFIRMED  # late confirmation of the same request


def test_exhausted_off_request_stays_blocked_while_nothing_changes_and_confirms_late():
    h = Harness()
    for _ in range(4):
        result = h.step("off", "heat")
        h.begin(result, "heat")
        h.fail()
        h.t += 301
    assert h.record.is_exhausted
    # an unreadable master (the interlock would block anyway) neither releases nor confirms
    h.step("off", "auto", ownership=cmd.Ownership(True, None, ()))
    assert h.record.is_exhausted
    h.step("off", "off")  # the master finally went off
    assert h.record.status == cmd.STATUS_CONFIRMED


def test_late_confirmation_between_retries():
    h = Harness()
    h.run_send("heat", "off")
    h.t += 60
    h.step("heat", "off")
    assert h.record.status == cmd.STATUS_FAILED and h.record.retry_at is not None
    h.t += 10
    result = h.step("none", "heat")  # the boiler turned on late
    assert h.record.status == cmd.STATUS_CONFIRMED and h.record.retry_at is None and result.send is None
    h.t += 1000
    assert h.step("none", "heat").send is None
    assert h.kinds() == ["sent", "failed", "confirmed"]


def test_retry_is_not_sent_if_the_action_changed():
    h = Harness()
    h.run_send("heat", "off")
    h.t += 60
    h.step("heat", "off")
    h.t += 60
    result = h.step("none", "off")  # the retry is due but the action is gone
    assert result.send is None and h.record.status == cmd.STATUS_SUPERSEDED
    assert h.kinds()[-1] == "superseded"


# ---------------------------------------------------------------------------
# OFF has priority and replaces a pending HEAT
# ---------------------------------------------------------------------------


def test_off_replaces_a_pending_heat_at_once():
    h = Harness()
    h.run_send("heat", "off")
    assert h.record.status == cmd.STATUS_PENDING
    h.t += 5
    result = h.step("off", "heat")  # the boiler is heating but no radiator is valid any more
    assert result.send == "off" and result.attempt == 1
    assert h.kinds() == ["sent", "superseded"] and h.events[-1].mode == "heat" and h.events[-1].detail == "action:off"
    assert h.record.status == cmd.STATUS_SUPERSEDED and h.record.last_confirmation_at is None  # the heat is not "confirmed"
    h.begin(result, "heat")
    assert h.record.mode == "off" and h.record.attempts == 1 and h.record.base_master == "heat"


def test_off_replaces_a_heat_waiting_for_its_retry_without_waiting():
    h = Harness()
    h.run_send("heat", "off")
    h.t += 60
    h.step("heat", "off")
    assert h.record.retry_at == h.t + 60
    h.t += 1  # the retry is not due
    result = h.step("off", "heat")
    assert result.send == "off" and result.attempt == 1  # no waiting for the retry delay


def test_off_replaces_a_heat_still_being_sent():
    h = Harness()
    result = h.step("heat", "off")
    h.begin(result, "off")
    h.t += 3
    result = h.step("off", "heat")
    assert result.send == "off" and h.record.status == cmd.STATUS_SUPERSEDED


def test_late_heat_observation_is_not_the_confirmation_of_the_off():
    h = Harness()
    h.run_send("off", "heat")
    h.t += 10
    result = h.step("off", "heat")  # the master still (or again) reports heat
    assert h.record.status == cmd.STATUS_PENDING and h.record.last_confirmation_at is None and result.send is None
    h.t += 50
    h.step("off", "heat")
    assert h.record.status == cmd.STATUS_FAILED  # only its own timeout and retries
    assert h.record.mode == "off"


def test_the_reverse_case_a_heat_after_a_pending_off():
    h = Harness()
    h.run_send("off", "heat")
    h.t += 5
    result = h.step("heat", "off")  # the master went off, M1 now wants heat
    # the off was observed (it is not a replacement): it is superseded because the action flipped,
    # and the new heat starts a new request
    assert result.send == "heat" and h.record.status == cmd.STATUS_SUPERSEDED


def test_action_none_supersedes_an_unconfirmed_request():
    h = Harness()
    h.run_send("heat", "off")
    h.t += 5
    result = h.step("none", "off")
    assert h.record.status == cmd.STATUS_SUPERSEDED and result.send is None
    assert h.events[-1].detail == "action:none" and result.control_status == cmd.CONTROL_READY


def test_a_confirmed_request_does_not_block_a_new_one():
    h = Harness()
    h.run_send("heat", "off")
    h.t += 5
    h.step("none", "heat")
    assert h.record.status == cmd.STATUS_CONFIRMED
    h.t += 4000
    result = h.step("off", "heat")  # later the boiler must be turned off
    assert result.send == "off" and result.attempt == 1
    h.begin(result, "heat")
    assert h.record.last_confirmation_at == T0 + 5  # carried over


# ---------------------------------------------------------------------------
# Loss of ownership, shutdown, stale callbacks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("setup", ["sending", "pending", "retry"])
def test_loss_of_ownership_cancels_everything_pending_without_compensation(setup):
    h = Harness()
    result = h.step("heat", "off")
    h.begin(result, "off")
    if setup in ("pending", "retry"):
        h.complete()
    if setup == "retry":
        h.t += 60
        h.step("heat", "off")
        assert h.record.retry_at is not None
    h.t += 1
    result = h.step("heat", "off", ownership=OWNER_NO)  # the external controller was switched on
    assert h.record.status == cmd.STATUS_CANCELLED and h.record.retry_at is None
    assert result.send is None and result.wake_at is None and result.control_status == cmd.CONTROL_BLOCKED_EXTERNAL_OWNER
    assert h.events[-1] == cmd.CommandEvent(cmd.EVENT_CANCELLED, "heat", h.record.attempts, cmd.BLOCK_EXTERNAL_OWNER)
    # later, even with the ownership back and the request still wanted, nothing is retried: a new request starts
    h.t += 400
    result = h.step("heat", "off")
    assert result.send == "heat" and result.attempt == 1


def test_old_generation_is_not_active_and_cancels():
    h = Harness()
    h.run_send("off", "heat")
    stale = cmd.Ownership(False, cmd.BLOCK_NOT_ACTIVE, ())
    h.step("off", "heat", ownership=stale)
    assert h.record.status == cmd.STATUS_CANCELLED and h.events[-1].detail == cmd.BLOCK_NOT_ACTIVE


def test_shutdown_cancels_pending_without_sending():
    h = Harness()
    h.run_send("off", "heat")
    result = h.step("off", "heat", shutting=True)
    assert h.record.status == cmd.STATUS_CANCELLED and h.events[-1].detail == "shutdown" and result.send is None


def test_a_confirmation_is_not_registered_without_the_ownership():
    h = Harness()
    h.run_send("heat", "off")
    h.step("none", "heat", ownership=OWNER_NO)
    assert h.record.status == cmd.STATUS_CANCELLED and h.record.last_confirmation_at is None


def test_late_service_results_of_an_old_send_are_ignored():
    h = Harness()
    result = h.step("heat", "off")
    h.begin(result, "off")
    old_id = h.send_id
    h.step("off", "heat")  # replaced...
    result = h.step("off", "heat")
    h.begin(result, "heat")
    snapshot = h.record
    # the heat service returns late, with the id of the old send
    assert cmd.on_service_completed(h.record, now=h.t + 5, send_id=old_id) is snapshot
    record, events = cmd.on_service_failed(h.record, now=h.t + 5, send_id=old_id, error="x")
    assert record is snapshot and events == ()
    assert cmd.on_service_completed(None, now=h.t, send_id=1) is None
    assert cmd.on_service_failed(None, now=h.t, send_id=1, error="x") == (None, ())


def test_results_after_cancellation_or_supersede_are_ignored():
    h = Harness()
    result = h.step("heat", "off")
    h.begin(result, "off")
    h.step("heat", "off", ownership=OWNER_NO)
    cancelled = h.record
    assert cmd.on_service_completed(cancelled, now=h.t, send_id=h.send_id) is cancelled
    assert cmd.on_service_failed(cancelled, now=h.t, send_id=h.send_id, error="x")[0] is cancelled


def test_a_lost_send_task_is_registered_as_a_failure():
    h = Harness()
    result = h.step("heat", "off")
    h.begin(result, "off")
    h.t += cmd.SEND_STALE_AFTER_S - 0.001
    assert h.step("heat", "off").send is None and h.record.status == cmd.STATUS_SENDING
    h.t += 0.001
    h.step("heat", "off")
    assert h.record.status == cmd.STATUS_FAILED and h.record.error == cmd.ERROR_SEND_LOST


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["sending", "pending", "failed"])
def test_unresolved_requests_become_unconfirmed_after_a_restart(status):
    h = Harness()
    result = h.step("heat", "off")
    h.begin(result, "off")
    if status != "sending":
        h.complete()
    if status == "failed":
        h.fail()
    stored = cmd.record_from_dict(cmd.record_to_dict(h.record))
    restored, events = cmd.restart_record(stored)
    assert restored.status == cmd.STATUS_UNCONFIRMED and restored.retry_at is None
    assert events == (cmd.CommandEvent(cmd.EVENT_UNCONFIRMED, "heat", 1, "restart"),)
    # the restart never confirms: the master is read again
    h2 = Harness()
    h2.record = restored
    h2.step("none", "heat")
    assert h2.record.status == cmd.STATUS_UNCONFIRMED and h2.record.last_confirmation_at is None
    assert h2.events == []


def test_exhausted_request_is_unconfirmed_after_a_restart_and_not_blocked():
    h = Harness()
    for _ in range(4):
        result = h.step("heat", "off")
        h.begin(result, "off")
        h.fail()
        h.t += 301
    assert h.record.is_exhausted
    restored, _ = cmd.restart_record(cmd.record_from_dict(cmd.record_to_dict(h.record)))
    h2 = Harness()
    h2.record = restored
    result = h2.step("heat", "off")
    assert result.send == "heat" and result.attempt == 1


@pytest.mark.parametrize("status", ["confirmed", "superseded", "cancelled", "unconfirmed"])
def test_resolved_records_are_kept_by_the_restart(status):
    record = cmd.CommandRecord(mode="off", status=status, attempts=1, at=T0)
    assert cmd.restart_record(record) == (record, ())
    assert cmd.restart_record(None) == (None, ())


def test_restart_with_the_master_still_wrong_starts_a_new_request_after_the_settling():
    record, _ = cmd.restart_record(cmd.CommandRecord(mode="off", status="pending", attempts=2, at=T0, service_completed_at=T0 + 1))
    h = Harness()
    h.record = record
    assert h.step("off", "heat", settled=False).send is None
    result = h.step("off", "heat", settled=True)
    assert result.send == "off" and result.attempt == 1


# ---------------------------------------------------------------------------
# Idempotence, purity, validation
# ---------------------------------------------------------------------------


def test_applying_a_step_twice_adds_no_event():
    h = Harness()
    h.run_send("heat", "off")
    h.t += 60
    first = h.step("heat", "off")
    again = cmd.step(h.record, h.inputs("heat", "off"))
    assert first.events != () and again.events == () and again.record == h.record and again.send is None


def test_step_does_not_mutate_and_is_deterministic():
    record = cmd.CommandRecord(mode="heat", status="pending", attempts=1, at=T0, service_completed_at=T0 + 1, base_master="off")
    inputs = cmd.CommandInputs(now=T0 + 100, action="heat", master_mode="off", ownership=OWNER_OK, settled=True)
    first, second = cmd.step(record, inputs), cmd.step(record, inputs)
    assert first == second and record.status == "pending"
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.status = "confirmed"  # type: ignore[misc]


def test_start_send_validation():
    with pytest.raises(ValueError):
        cmd.start_send(None, mode="cool", attempt=1, now=T0, send_id=1, base_master="off")
    with pytest.raises(ValueError):
        cmd.start_send(None, mode="heat", attempt=0, now=T0, send_id=1, base_master="off")
    with pytest.raises(ValueError):
        cmd.start_send(None, mode="heat", attempt=5, now=T0, send_id=1, base_master="off")
    record, _ = cmd.start_send(None, mode="heat", attempt=2, now=T0, send_id=1, base_master="off")  # resumes a remembered budget
    assert record.attempts == 2 and record.base_master == "off"
    pending = cmd.CommandRecord(mode="heat", status="pending", attempts=1, at=T0)
    with pytest.raises(ValueError):
        cmd.start_send(pending, mode="off", attempt=1, now=T0, send_id=2, base_master="heat")  # never over an unresolved request
    failed = cmd.CommandRecord(mode="heat", status="failed", attempts=1, at=T0, retry_at=T0 + 60, base_master="off")
    with pytest.raises(ValueError):
        cmd.start_send(failed, mode="off", attempt=2, now=T0, send_id=2, base_master="heat")  # a retry keeps the mode
    with pytest.raises(ValueError):
        cmd.start_send(failed, mode="heat", attempt=3, now=T0, send_id=2, base_master="off")  # and the attempt sequence
    record, event = cmd.start_send(failed, mode="heat", attempt=2, now=T0 + 60, send_id=2, base_master="heat")
    assert record.attempts == 2 and record.base_master == "off" and event.attempt == 2  # the base master is the first one


def test_record_validation():
    for kwargs in (
        dict(mode="cool", status="pending", attempts=1, at=T0),
        dict(mode="heat", status="weird", attempts=1, at=T0),
        dict(mode="heat", status="pending", attempts=5, at=T0),
        dict(mode="heat", status="pending", attempts=True, at=T0),
        dict(mode="heat", status="pending", attempts=1, at=float("nan")),
        dict(mode="heat", status="pending", attempts=1, at=T0, retry_at=float("inf")),
    ):
        with pytest.raises(ValueError):
            cmd.CommandRecord(**kwargs)


# ---------------------------------------------------------------------------
# Control status table
# ---------------------------------------------------------------------------


def test_control_status_values():
    h = Harness()
    assert h.step("none", "off", settled=False).control_status == cmd.CONTROL_SETTLING
    assert h.step("none", "off").control_status == cmd.CONTROL_READY
    h.run_send("heat", "off")
    assert h.step("heat", "off").control_status == cmd.CONTROL_PENDING
    assert h.step("none", "heat").control_status == cmd.CONTROL_CONFIRMED
    # the master leaves the commanded mode afterwards: back to ready
    assert h.step("none", "off").control_status == cmd.CONTROL_READY
    assert h.step("none", "off", ownership=cmd.Ownership(False, cmd.BLOCK_NO_VALID_EVALUATION, ())).control_status == cmd.CONTROL_STARTING
    assert set(cmd.CONTROL_STATUSES) == {
        "inactive",
        "starting",
        "settling",
        "blocked_external_owner",
        "blocked_master_unknown",
        "ready",
        "pending",
        "confirmed",
        "command_failed",
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_record_roundtrip_keeps_the_context_but_not_the_retry():
    record = cmd.CommandRecord(
        mode="off",
        status="failed",
        attempts=2,
        at=T0 + 5,
        send_id=7,
        service_completed_at=T0 + 6,
        last_confirmation_at=T0 - 100,
        base_master="heat",
        retry_at=T0 + 999,
        error="confirmation_timeout",
    )
    data = cmd.record_to_dict(record)
    assert {"mode", "at", "attempts", "status"} <= set(data)  # the fields required by the specification
    assert "retry_at" not in data
    assert cmd.record_from_dict(data) == dataclasses.replace(record, retry_at=None)
    assert cmd.record_to_dict(None) is None


@pytest.mark.parametrize(
    "value",
    [
        None,
        "x",
        [],
        5,
        {},
        {"mode": "heat"},
        {"mode": "cool", "status": "pending", "attempts": 1, "at": 1.0},
        {"mode": "heat", "status": "x", "attempts": 1, "at": 1.0},
        {"mode": "heat", "status": "pending", "attempts": "1", "at": 1.0},
        {"mode": "heat", "status": "pending", "attempts": 9, "at": 1.0},
        {"mode": "heat", "status": "pending", "attempts": 1, "at": "now"},
        {"mode": "heat", "status": "pending", "attempts": 1, "at": float("nan")},
    ],
)
def test_invalid_stored_records_read_as_no_record(value):
    assert cmd.record_from_dict(value) is None


def test_stored_record_tolerates_garbage_in_the_optional_fields():
    record = cmd.record_from_dict({"mode": "heat", "status": "pending", "attempts": 1, "at": T0, "send_id": -3, "base_master": 5, "error": 7, "service_completed_at": "x"})
    assert record.send_id == 0 and record.base_master is None and record.error is None and record.service_completed_at is None


def test_store_migration_from_m2a_adds_the_command():
    v1 = {"cycle": {"m": "off", "off": 1, "r": None, "s": []}, "legacy_seeded": True, "first_origin": "legacy_seed", "saved_at": T0}
    migrated = cmd.migrate_store_data(1, 1, v1)
    assert migrated["command"] is None and migrated["cycle"] == v1["cycle"] and migrated["legacy_seeded"] is True
    assert "command" not in v1  # the input is not mutated
    assert cmd.migrate_store_data(1, 1, "garbage") == {"command": None}
    current = {"cycle": {}, "command": {"mode": "heat"}}
    assert cmd.migrate_store_data(2, 1, current) == current
    with pytest.raises(NotImplementedError):
        cmd.migrate_store_data(3, 1, current)
    with pytest.raises(NotImplementedError):
        cmd.migrate_store_data(0, 1, current)


# ---------------------------------------------------------------------------
# Wake-up instants and diagnostics of the owners
# ---------------------------------------------------------------------------


def test_wake_up_instants():
    h = Harness()
    result = h.step("heat", "off")
    assert result.send == "heat" and result.wake_at is None  # nothing to wait for: the send is imminent
    h.begin(result, "off")
    assert h.step("heat", "off").wake_at == T0 + cmd.SEND_STALE_AFTER_S  # a send that never reports back
    h.t += 25
    h.complete()
    assert h.step("heat", "off").wake_at == T0 + 25 + cmd.CONFIRMATION_TIMEOUT_S  # from the service completion
    h.t += 60
    result = h.step("heat", "off")
    assert h.record.status == cmd.STATUS_FAILED and result.wake_at == h.record.retry_at == h.t + 60
    h.t += 60
    result = h.step("heat", "off")
    assert result.send == "heat" and result.attempt == 2 and result.wake_at is None  # the retry is due: no wake-up needed
    assert Harness().step("none", "off").wake_at is None


def test_diagnostics_list_the_external_owners_in_order():
    seen = cmd.Ownership(False, cmd.BLOCK_EXTERNAL_OWNER, (cmd.ExternalOwner("automation.a", "on"), cmd.ExternalOwner("automation.b", None)))
    fields = cmd.command_diagnostics(None, seen, cmd.CONTROL_BLOCKED_EXTERNAL_OWNER)
    assert fields["external_owners"] == [{"entity_id": "automation.a", "state": "on"}, {"entity_id": "automation.b", "state": None}]
    assert fields["control_status"] == "blocked_external_owner"
