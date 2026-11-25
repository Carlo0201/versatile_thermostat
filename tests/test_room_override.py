"""Tests of the timed room override: pure policy and the Home Assistant independent runtime.

Nothing sleeps and Home Assistant is never imported. From the repository root::

    python -m pytest -q --noconftest --import-mode=importlib tests/test_room_override.py
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import os
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_DIR = Path(os.environ["PDC_COMPONENT_DIR"]) if os.environ.get("PDC_COMPONENT_DIR") else REPO_ROOT / "custom_components" / "versatile_thermostat"
PKG = "_vtherm_room_override_pkg_under_test"


def load(name: str) -> types.ModuleType:
    if PKG not in sys.modules:
        package = types.ModuleType(PKG)
        package.__path__ = [str(COMPONENT_DIR)]
        sys.modules[PKG] = package
    return importlib.import_module(f"{PKG}.{name}")


pol = load("room_override_policy")
rt = load("room_override_runtime")

T0 = 1_795_500_000.0
WEEK, WEEKEND, TIMER_OWNER = "switch.schedule_cucina_feriale", "switch.schedule_cucina_weekend", "automation.oikos_riprendi_programma_riscaldamento_cucina"
WORKDAY = "binary_sensor.workday_sensor"


# ------------------------------------------------------------------ pure policy


def test_start_validates_and_restart_keeps_the_original_start():
    first = pol.start_override(pol.IDLE, T0, 120)
    assert first.active and first.ends_at == T0 + 7200 and first.started_at == T0 and first.duration_min == 120
    again = pol.start_override(first, T0 + 600, 60)
    assert again.started_at == T0 and again.ends_at == T0 + 600 + 3600 and again.duration_min == 60
    for bad in (0, -5, 1441, float("nan"), float("inf"), True, "120", None):
        with pytest.raises(ValueError):
            pol.start_override(pol.IDLE, T0, bad)
    with pytest.raises(ValueError):
        pol.start_override(pol.IDLE, float("nan"), 60)


def test_state_invariants():
    with pytest.raises(ValueError):
        pol.OverrideState(active=True)
    with pytest.raises(ValueError):
        pol.OverrideState(ending=True)
    with pytest.raises(ValueError):
        pol.OverrideState(attempts=-1)
    with pytest.raises(ValueError):
        pol.OverrideState(active=True, started_at=T0, ends_at=float("inf"), duration_min=1)


def test_due_and_remaining():
    state = pol.start_override(pol.IDLE, T0, 60)
    assert not pol.is_due(state, T0 + 3599) and pol.is_due(state, T0 + 3600)
    assert pol.is_due(pol.begin_ending(state), T0)  # an end already in progress is due whatever the clock says
    assert pol.remaining_s(state, T0 + 100) == 3500 and pol.remaining_s(state, T0 + 9999) == 0.0 and pol.remaining_s(pol.IDLE, T0) is None


@pytest.mark.parametrize(
    "states,override,expected",
    [
        ([], False, "none"),
        ([], True, "none"),
        (["on", "on"], False, "program"),
        (["triggered", "on"], False, "program"),
        (["off", "off"], False, "manual"),
        (["on", "off"], False, "partial"),
        (["on", "unavailable"], False, "unknown"),
        ([None], False, "unknown"),
        (["unknown", "off"], True, "override"),
        (["off", "off"], True, "override"),
    ],
)
def test_program_mode(states, override, expected):
    assert pol.program_mode(states, override) == expected


@pytest.mark.parametrize(
    "weekdays,weekday,workday,expected",
    [
        (["daily"], 3, "on", True),
        (["mon", "tue"], 1, "on", True),
        (["mon", "tue"], 2, "on", False),
        (["workday"], 2, "on", True),
        (["workday"], 2, "off", False),  # a holiday on a Wednesday
        (["weekend"], 2, "off", True),  # the holiday counts as weekend
        (["weekend"], 5, "on", False),  # a working Saturday
        (["workday"], 4, "unavailable", True),  # unknown sensor: Monday-Friday
        (["workday"], 5, None, False),
        (["weekend"], 6, "unknown", True),
        (None, 0, "on", False),
        (["SAT"], 5, "on", True),
    ],
)
def test_program_applies_today(weekdays, weekday, workday, expected):
    assert pol.program_applies_today(weekdays, weekday, workday) is expected


def test_programs_to_run_keeps_configuration_order_and_rejects_a_bad_weekday():
    programs = [("a", ["workday"]), ("b", ["weekend"]), ("c", ["daily"])]
    assert pol.programs_to_run(programs, 0, "on") == ("a", "c")
    assert pol.programs_to_run(programs, 0, "off") == ("b", "c")
    for bad in (-1, 7, True, "mon"):
        with pytest.raises(ValueError):
            pol.programs_to_run(programs, bad, "on")


# ---------------------------------------------------------------- the runtime


class Plant:
    def __init__(self, stored: dict | None = None, legacy: tuple[str, ...] = (), workday: bool = True) -> None:
        self.clock = T0
        self.weekday_index = 2
        self.states: dict[str, tuple[str | None, dict]] = {
            WEEK: ("on", {"weekdays": ["workday"]}),
            WEEKEND: ("on", {"weekdays": ["weekend"]}),
            WORKDAY: ("on", {}),
            TIMER_OWNER: ("off", {}),
        }
        self.calls: list[tuple[str, str, str]] = []
        self.timers: list[list[Any]] = []
        self.saved: dict | None = copy.deepcopy(stored)
        self.saves = 0
        self.notifications: list[tuple[str, str | None]] = []
        self.fail: dict[tuple[str, str], int] = {}
        self.save_fail = False
        self.ignore_service: set[tuple[str, str]] = set()
        cfg = rt.RoomOverrideConfig("cucina", (WEEK, WEEKEND), WORKDAY if workday else None, legacy)
        self.runtime = rt.RoomOverrideRuntime([cfg], self, stored=stored)

    # ports
    def now(self) -> float:
        return self.clock

    def weekday(self) -> int:
        return self.weekday_index

    def state(self, entity_id: str) -> tuple[str | None, dict]:
        return self.states.get(entity_id, (None, {}))

    async def call_service(self, domain: str, service: str, entity_id: str) -> None:
        self.calls.append((domain, service, entity_id))
        remaining = self.fail.get((service, entity_id), 0)
        if remaining:
            self.fail[(service, entity_id)] = remaining - 1
            raise RuntimeError("boom")
        if (service, entity_id) in self.ignore_service:
            return
        if domain == "switch" and service in ("turn_on", "turn_off"):
            attrs = self.states[entity_id][1]
            self.states[entity_id] = ("on" if service == "turn_on" else "off", attrs)

    def call_at(self, at: float, callback: Callable[[], None]) -> Callable[[], None]:
        entry = [at, callback, True]
        self.timers.append(entry)

        def cancel() -> None:
            entry[2] = False

        return cancel

    async def save(self, data: dict) -> None:
        if self.save_fail:
            raise OSError("disk full")
        self.saved = copy.deepcopy(data)
        self.saves += 1

    def notify(self, notification_id: str, title: str | None, message: str | None) -> None:
        self.notifications.append((notification_id, title))

    # test helpers
    def set(self, entity_id: str, state: str | None, **attrs: Any) -> None:
        self.states[entity_id] = (state, attrs or self.states.get(entity_id, (None, {}))[1])

    async def settle(self) -> None:
        for _ in range(40):
            await asyncio.sleep(0)

    async def advance(self, seconds: float, step: float = 5.0) -> None:
        end = self.clock + seconds
        while self.clock < end:
            self.clock = min(end, self.clock + step)
            due = [t for t in self.timers if t[2] and t[0] <= self.clock]
            self.timers = [t for t in self.timers if t[2] and t[0] > self.clock]
            for entry in sorted(due, key=lambda t: t[0]):
                entry[1]()
            await self.settle()

    def commands(self, service: str | None = None) -> list[tuple[str, str, str]]:
        return [c for c in self.calls if service is None or c[1] == service]


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_a_normal_override_pauses_then_returns_the_program_and_applies_the_slot():
    async def scenario():
        plant = Plant()
        await plant.runtime.start()
        assert await plant.runtime.start_override("cucina", 120) == "started"
        assert plant.states[WEEK][0] == "off" and plant.states[WEEKEND][0] == "off"
        assert plant.saved["rooms"]["cucina"]["active"] is True
        status = plant.runtime.status("cucina")
        assert status["program_mode"] == "override" and status["override_ends_at"] == T0 + 7200
        await plant.advance(7190)
        assert plant.states[WEEK][0] == "off"  # not yet
        await plant.advance(20)
        assert plant.states[WEEK][0] == "on" and plant.states[WEEKEND][0] == "on"
        # Wednesday, working day: only the workday program is run, after the switches are back on
        assert plant.commands("run_action") == [("scheduler", "run_action", WEEK)]
        assert plant.calls.index(("switch", "turn_on", WEEK)) < plant.calls.index(("scheduler", "run_action", WEEK))
        assert plant.runtime.state("cucina") == pol.IDLE and plant.saved["rooms"]["cucina"]["active"] is False
        assert plant.runtime.status("cucina")["program_mode"] == "program"

    run(scenario())


def test_a_holiday_applies_the_weekend_program():
    async def scenario():
        plant = Plant()
        plant.set(WORKDAY, "off")
        await plant.runtime.start()
        await plant.runtime.start_override("cucina", 60)
        await plant.advance(3700)
        assert plant.commands("run_action") == [("scheduler", "run_action", WEEKEND)]

    run(scenario())


def test_restart_with_a_longer_duration_extends_and_a_shorter_one_shortens():
    async def scenario():
        plant = Plant()
        await plant.runtime.start()
        await plant.runtime.start_override("cucina", 60)
        await plant.advance(1800)
        assert await plant.runtime.start_override("cucina", 120) == "restarted"
        assert plant.runtime.state("cucina").ends_at == T0 + 1800 + 7200
        await plant.advance(3700)  # the first expiry would have passed: it must not end the override
        assert plant.runtime.state("cucina").active and plant.states[WEEK][0] == "off"
        assert await plant.runtime.start_override("cucina", 30) == "restarted"
        await plant.advance(1900)
        assert not plant.runtime.state("cucina").active and plant.states[WEEK][0] == "on"
        assert len(plant.commands("run_action")) == 1  # one end, not two

    run(scenario())


def test_cancel_returns_the_program_immediately():
    async def scenario():
        plant = Plant()
        await plant.runtime.start()
        assert await plant.runtime.cancel_override("cucina") == "not_active"
        await plant.runtime.start_override("cucina", 240)
        assert await plant.runtime.cancel_override("cucina") == "ended"
        assert plant.states[WEEK][0] == "on" and not plant.runtime.state("cucina").active
        await plant.advance(15000)
        assert len(plant.commands("run_action")) == 1  # the old expiry did nothing more

    run(scenario())


def test_unknown_room_and_invalid_duration_change_nothing():
    async def scenario():
        plant = Plant()
        await plant.runtime.start()
        assert await plant.runtime.start_override("sala", 60) == "unknown_room"
        assert await plant.runtime.cancel_override("sala") == "unknown_room"
        assert await plant.runtime.start_override("cucina", 0) == "invalid_duration"
        assert await plant.runtime.start_override("cucina", "abc") == "invalid_duration"
        assert plant.calls == [] and plant.saves == 0 and plant.states[WEEK][0] == "on"

    run(scenario())


def test_start_is_refused_when_a_scheduler_is_not_readable():
    async def scenario():
        for bad in ("unavailable", "unknown", None):
            plant = Plant()
            plant.set(WEEKEND, bad)
            await plant.runtime.start()
            assert await plant.runtime.start_override("cucina", 60) == "schedulers_unknown"
            assert plant.calls == [] and plant.saves == 0 and not plant.runtime.state("cucina").active

    run(scenario())


def test_start_is_refused_while_the_legacy_owner_is_not_exactly_off():
    async def scenario():
        for state in ("on", "unavailable", "unknown", None):
            plant = Plant(legacy=(TIMER_OWNER,))
            plant.set(TIMER_OWNER, state)
            await plant.runtime.start()
            assert await plant.runtime.start_override("cucina", 60) == "blocked_by_legacy"
            assert plant.calls == [] and "blocked_by_legacy" in plant.runtime.status("cucina")["override_last_error"]
        plant = Plant(legacy=(TIMER_OWNER,))
        await plant.runtime.start()
        assert await plant.runtime.start_override("cucina", 60) == "started"

    run(scenario())


def test_the_end_is_allowed_even_if_the_legacy_owner_came_back():
    async def scenario():
        plant = Plant(legacy=(TIMER_OWNER,))
        await plant.runtime.start()
        await plant.runtime.start_override("cucina", 60)
        plant.set(TIMER_OWNER, "on")  # somebody re-enabled it: refusing the end would leave the program off for good
        await plant.advance(3700)
        assert plant.states[WEEK][0] == "on" and not plant.runtime.state("cucina").active

    run(scenario())


def test_a_failed_write_ahead_touches_nothing():
    async def scenario():
        plant = Plant()
        await plant.runtime.start()
        plant.save_fail = True
        assert await plant.runtime.start_override("cucina", 60) == "store_save_failed"
        assert plant.calls == [] and plant.states[WEEK][0] == "on" and not plant.runtime.state("cucina").active

    run(scenario())


def test_the_state_is_saved_before_the_first_command():
    async def scenario():
        plant = Plant()
        seen: list[tuple[str, bool, bool]] = []
        original = plant.call_service

        async def spy(domain, service, entity_id):
            state = plant.saved["rooms"]["cucina"]
            seen.append((service, state["pausing"], state["ending"]))
            await original(domain, service, entity_id)

        plant.call_service = spy
        await plant.runtime.start()
        await plant.runtime.start_override("cucina", 60)
        assert seen == [("turn_off", True, False), ("turn_off", True, False)]
        seen.clear()
        await plant.advance(3700)
        assert seen == [("turn_on", False, True), ("turn_on", False, True), ("run_action", False, True)]

    run(scenario())


def test_a_restart_in_the_middle_resumes_and_a_late_expiry_ends_once():
    async def scenario():
        plant = Plant()
        await plant.runtime.start()
        await plant.runtime.start_override("cucina", 60)
        stored = copy.deepcopy(plant.saved)
        for key in ("pausing", "restored", "end_reason"):
            del stored["rooms"]["cucina"][key]  # original schema v1 payload
        await plant.runtime.stop()
        # HA was down for two hours: the override is due at the first look
        other = Plant(stored=stored)
        other.clock = T0 + 7200
        other.states[WEEK], other.states[WEEKEND] = ("off", {"weekdays": ["workday"]}), ("off", {"weekdays": ["weekend"]})
        await other.runtime.start()
        await other.advance(10)
        assert other.states[WEEK][0] == "on" and not other.runtime.state("cucina").active
        assert len(other.commands("run_action")) == 1

    run(scenario())


def test_a_restart_before_the_expiry_rearms_and_keeps_the_program_off():
    async def scenario():
        plant = Plant()
        await plant.runtime.start()
        await plant.runtime.start_override("cucina", 60)
        stored = copy.deepcopy(plant.saved)
        await plant.runtime.stop()
        other = Plant(stored=stored)
        other.clock = T0 + 600
        other.states[WEEK] = ("on", {"weekdays": ["workday"]})  # one switch came back on while HA was down
        other.states[WEEKEND] = ("off", {"weekdays": ["weekend"]})
        await other.runtime.start()
        await other.advance(10)
        assert other.states[WEEK][0] == "off"  # the running override repairs it
        await other.advance(2900)  # clock now T0 + 3510, the override ends at T0 + 3600
        assert other.runtime.state("cucina").active
        await other.advance(100)
        assert not other.runtime.state("cucina").active and other.states[WEEK][0] == "on"

    run(scenario())


def test_the_program_switched_on_by_somebody_else_ends_the_override_without_commands():
    async def scenario():
        plant = Plant()
        await plant.runtime.start()
        await plant.runtime.start_override("cucina", 120)
        plant.calls.clear()
        plant.set(WEEK, "on")
        plant.set(WEEKEND, "on")
        await plant.advance(3000)  # the periodic look is the expiry: bring it nearer to observe the rule
        # not due yet: a wake only happens at the expiry, so force it by restarting the engine
        stored = copy.deepcopy(plant.saved)
        await plant.runtime.stop()
        other = Plant(stored=stored)
        other.clock = plant.clock
        await other.runtime.start()
        await other.advance(10)
        assert not other.runtime.state("cucina").active and other.commands() == []
        assert other.runtime.status("cucina")["override_last_event"] == "ended:externally_resumed"

    run(scenario())


def test_switches_not_back_on_keep_the_override_and_retry_then_notify():
    async def scenario():
        plant = Plant()
        await plant.runtime.start()
        await plant.runtime.start_override("cucina", 60)
        plant.fail[("turn_on", WEEK)] = 100
        await plant.advance(3700)
        state = plant.runtime.state("cucina")
        assert state.active and state.ending and state.attempts >= 1  # never forgotten
        assert plant.commands("run_action") == []  # the slot is applied only once the switches are on
        await plant.advance(200)
        assert plant.runtime.state("cucina").attempts >= rt.NOTIFY_AFTER_ATTEMPTS
        assert any(n[0] == "cucina_override_stuck" and n[1] for n in plant.notifications)
        assert "scheduler_on_failed" in plant.runtime.status("cucina")["override_last_error"]
        plant.fail[("turn_on", WEEK)] = 0
        await plant.advance(60)
        assert plant.states[WEEK][0] == "on" and not plant.runtime.state("cucina").active
        assert ("cucina_override_stuck", None) in plant.notifications
        assert len(plant.commands("run_action")) == 1

    run(scenario())


def test_a_switch_that_ignores_turn_on_is_not_taken_for_on():
    async def scenario():
        plant = Plant()
        await plant.runtime.start()
        await plant.runtime.start_override("cucina", 60)
        plant.ignore_service.add(("turn_on", WEEK))
        await plant.advance(3700)
        assert plant.runtime.state("cucina").active and plant.commands("run_action") == []
        assert plant.runtime.status("cucina")["override_last_error"] == "scheduler_on_not_confirmed"

    run(scenario())


def test_run_action_failure_is_retried_and_the_override_stays_until_it_works():
    async def scenario():
        plant = Plant()
        await plant.runtime.start()
        await plant.runtime.start_override("cucina", 60)
        plant.fail[("run_action", WEEK)] = 1
        await plant.advance(3610)
        assert plant.runtime.state("cucina").active
        await plant.advance(60)
        assert not plant.runtime.state("cucina").active
        assert len(plant.commands("run_action")) == 2

    run(scenario())


def test_turn_off_failure_at_the_start_is_pending_and_repaired_before_expiry():
    async def scenario():
        plant = Plant()
        await plant.runtime.start()
        plant.fail[("turn_off", WEEK)] = 1
        assert await plant.runtime.start_override("cucina", 60) == "pending"
        assert plant.runtime.status("cucina")["override_pausing"]
        assert plant.states[WEEK][0] == "on" and "scheduler_off_failed" in plant.runtime.status("cucina")["override_last_error"]
        await plant.advance(rt.RETRY_S)
        assert plant.states[WEEK][0] == "off" and plant.runtime.state("cucina").active
        assert not plant.runtime.status("cucina")["override_pausing"]
        await plant.advance(3700)
        assert plant.states[WEEK][0] == "on" and not plant.runtime.state("cucina").active

    run(scenario())


def test_failed_duration_change_keeps_the_original_expiry():
    async def scenario():
        plant = Plant()
        assert await plant.runtime.start_override("cucina", 60) == "started"
        await plant.advance(120)
        plant.save_fail = True
        assert await plant.runtime.start_override("cucina", 120) == "store_save_failed"
        plant.save_fail = False
        assert plant.runtime.state("cucina").ends_at == T0 + 3600
        await plant.advance(3480)
        assert not plant.runtime.state("cucina").active
        assert len(plant.commands("run_action")) == 1

    run(scenario())


def test_restart_recovers_a_pause_saved_before_any_switch_was_turned_off():
    async def scenario():
        plant = Plant()
        plant.ignore_service.update({("turn_off", WEEK), ("turn_off", WEEKEND)})
        assert await plant.runtime.start_override("cucina", 60) == "pending"
        assert plant.saved["rooms"]["cucina"]["pausing"]
        await plant.runtime.stop()
        restarted = Plant(stored=plant.saved)
        await restarted.runtime.start()
        await restarted.settle()
        assert restarted.states[WEEK][0] == restarted.states[WEEKEND][0] == "off"
        assert restarted.runtime.state("cucina").active
        assert not restarted.runtime.state("cucina").pausing

    run(scenario())


def test_triggered_is_a_valid_state_when_pausing_and_resuming():
    async def scenario():
        plant = Plant()
        plant.set(WEEK, "triggered")
        assert await plant.runtime.start_override("cucina", 60) == "started"
        plant.set(WEEK, "triggered")
        assert await plant.runtime.cancel_override("cucina") == "ended"
        assert plant.commands("turn_on") == [("switch", "turn_on", WEEKEND)]
        assert plant.commands("run_action") == [("scheduler", "run_action", WEEK)]

    run(scenario())


def test_cancel_retries_its_write_ahead_without_waiting_for_expiry():
    async def scenario():
        plant = Plant()
        await plant.runtime.start_override("cucina", 60)
        plant.save_fail = True
        assert await plant.runtime.cancel_override("cucina") == "store_save_failed"
        assert plant.runtime.status("cucina")["override_ending"]
        assert not plant.commands("turn_on")
        plant.save_fail = False
        await plant.advance(rt.RETRY_S)
        assert plant.runtime.status("cucina")["override_last_event"] == "ended:cancelled"
        assert len(plant.commands("run_action")) == 1

    run(scenario())


@pytest.mark.parametrize("restart", [False, True])
def test_failed_final_save_does_not_replay_the_slot(restart):
    async def scenario():
        plant = Plant()
        await plant.runtime.start_override("cucina", 60)
        original_save = plant.save

        async def fail_idle(data):
            if not data["rooms"]["cucina"]["active"]:
                raise OSError("disk full at final save")
            await original_save(data)

        plant.save = fail_idle
        assert await plant.runtime.cancel_override("cucina") == "store_save_failed"
        assert plant.saved["rooms"]["cucina"]["restored"]
        assert len(plant.commands("run_action")) == 1
        if restart:
            await plant.runtime.stop()
            recovered = Plant(stored=plant.saved)
            await recovered.runtime.start()
            await recovered.settle()
            assert recovered.commands() == []
            assert not recovered.runtime.state("cucina").active
        else:
            plant.save = original_save
            await plant.advance(rt.RETRY_S)
            assert len(plant.commands("run_action")) == 1
            assert not plant.runtime.state("cucina").active

    run(scenario())


@pytest.mark.parametrize(
    "stored",
    [
        {"schema_version": 99, "rooms": {}},
        {"schema_version": 1, "rooms": "x"},
        {"schema_version": 1, "rooms": {"cucina": {"active": "yes"}}},
        {"schema_version": 1, "rooms": {"cucina": {"active": True}}},
    ],
)
def test_an_unreadable_store_blocks_everything_notifies_and_is_never_overwritten(stored):
    async def scenario():
        plant = Plant(stored=stored)
        await plant.runtime.start()
        assert plant.runtime.load_error is not None
        assert any(n[0] == "room_override_store_unreadable" and n[1] for n in plant.notifications)
        assert await plant.runtime.start_override("cucina", 60) == "store_unreadable"
        await plant.advance(500)
        assert plant.calls == [] and plant.saves == 0

    run(scenario())


def test_stop_cancels_the_timers_and_keeps_the_persisted_override():
    async def scenario():
        plant = Plant()
        await plant.runtime.start()
        await plant.runtime.start_override("cucina", 60)
        await plant.runtime.stop()
        await plant.advance(4000)
        assert plant.states[WEEK][0] == "off" and plant.saved["rooms"]["cucina"]["active"] is True

    run(scenario())


def test_a_room_without_program_switches_cannot_start():
    async def scenario():
        plant = Plant()
        cfg = rt.RoomOverrideConfig("sala", ())
        runtime = rt.RoomOverrideRuntime([cfg], plant)
        await runtime.start()
        assert await runtime.start_override("sala", 60) == "no_program"
        assert runtime.status("sala")["program_mode"] == "none"

    run(scenario())
