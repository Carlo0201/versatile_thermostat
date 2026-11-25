"""Tests of the per-room action journal: pure model and the Home Assistant independent runtime.

Nothing sleeps and Home Assistant is never imported. From the repository root::

    python -m pytest -q --noconftest --import-mode=importlib tests/test_room_journal.py
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import json
import os
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_DIR = Path(os.environ["PDC_COMPONENT_DIR"]) if os.environ.get("PDC_COMPONENT_DIR") else REPO_ROOT / "custom_components" / "versatile_thermostat"
PKG = "_vtherm_room_journal_pkg_under_test"


def load(name: str) -> types.ModuleType:
    if PKG not in sys.modules:
        package = types.ModuleType(PKG)
        package.__path__ = [str(COMPONENT_DIR)]
        sys.modules[PKG] = package
    return importlib.import_module(f"{PKG}.{name}")


pol = load("room_journal_policy")
rt = load("room_journal_runtime")

T0 = 1_795_500_000.0
VT_CARLO, TRV_CARLO, SPLIT_CARLO = "climate.trv_vtherm_camera_carlo", "climate.trv_camera_carlo", "climate.camera_carlo_condizionatore"
VT_CUCINA, TRV_CUCINA = "climate.trv_vtherm_cucina", "climate.trv_cucina"


def view(state="heat", **attrs):
    return pol.StateView(state, attrs)


# ------------------------------------------------------------------ detect changes


def fields(changes):
    return [(c.field, c.from_value, c.to_value) for c in changes]


def test_only_actions_are_changes_never_the_measured_temperature_or_diagnostics():
    old = view("heat", temperature=19.0, current_temperature=18.0, preset_mode="comfort", hvac_action="idle", ema_temp=18.1)
    new = view("heat", temperature=19.0, current_temperature=18.4, preset_mode="comfort", hvac_action="idle", ema_temp=18.2, last_update="x")
    assert pol.detect_changes(old, new) == ()


def test_every_action_field_is_detected_in_a_stable_order():
    old = view("heat", temperature=19.0, preset_mode="comfort", hvac_action="idle", fan_mode="auto", swing_mode="off")
    new = view("off", temperature=16.5, preset_mode="eco", hvac_action="off", fan_mode="low", swing_mode="vertical")
    assert fields(pol.detect_changes(old, new)) == [
        ("mode", "heat", "off"),
        ("target", 19.0, 16.5),
        ("preset", "comfort", "eco"),
        ("activity", "idle", "off"),
        ("fan", "auto", "low"),
        ("swing", "off", "vertical"),
    ]


def test_a_missing_old_or_new_state_is_not_a_change():
    assert pol.detect_changes(None, view()) == () and pol.detect_changes(view(), None) == ()


def test_unavailable_to_a_state_is_recorded_like_the_automation_does():
    assert fields(pol.detect_changes(view("unavailable"), view("heat"))) == [("mode", "unavailable", "heat")]


def test_values_are_always_scalars():
    changes = pol.detect_changes(view("heat", temperature=float("nan"), preset_mode=["a"]), view("heat", temperature=float("inf"), preset_mode={"b": 1}))
    for change in changes:
        for value in (change.from_value, change.to_value):
            assert value is None or isinstance(value, (str, int, float, bool))
    assert fields(pol.detect_changes(view("heat", temperature=19), view("heat", temperature=float("nan")))) == [("target", 19, None)]


# ----------------------------------------------------------------- the author


def test_the_author_is_an_exact_context_match_or_unknown():
    candidates = {"ctx-auto": ("automation.scelta", "Scelta radiatore"), "ctx-parent": ("script.riprendi", "Riprendi")}
    assert pol.resolve_origin(["ctx-auto", None], candidates) == ("automation.scelta", "Scelta radiatore")
    assert pol.resolve_origin([None, "ctx-parent"], candidates) == ("script.riprendi", "Riprendi")
    assert pol.resolve_origin(["ctx-auto", "ctx-parent"], candidates) == ("automation.scelta", "Scelta radiatore")  # own context first
    assert pol.resolve_origin(["ctx-other", ""], candidates) == (None, None)  # a remote control has no context: never guessed
    assert pol.resolve_origin([], candidates) == (None, None)


def test_the_journal_own_entity_is_never_the_author():
    candidates = {"ctx": ("automation.registro", "Registro")}
    assert pol.resolve_origin(["ctx"], candidates, exclude=["automation.registro"]) == (None, None)


# ------------------------------------------------------------------- the entry


def entry(at=T0, room="camera_carlo", entity=VT_CARLO, **kw):
    kw.setdefault("changes", (pol.Change("mode", "off", "heat"),))
    return pol.JournalEntry(at, room, entity, **kw)


def test_an_entry_is_validated():
    entry()
    for kw in (
        {"at": float("nan")},
        {"at": -1},
        {"at": True},
        {"room": "Camera Carlo"},
        {"entity": "not an id"},
        {"kind": "nope"},
        {"changes": ()},  # a change entry without a change
        {"reason": "x" * 500},
        {"current": float("inf")},
        {"power": "high"},
        {"changes": tuple(pol.Change("mode", "a", "b") for _ in range(13))},
    ):
        with pytest.raises(ValueError):
            entry(**kw)
    pol.JournalEntry(T0, "camera_carlo", VT_CARLO, pol.KIND_PDC_START, reason="heat_demand", current=18.2, target=21.0, power=1100.0)


def test_an_entry_round_trips_in_the_card_layout():
    original = entry(reason="state_report", user_id="u1", source="automation.x", source_name="X")
    data = original.to_dict()
    assert data["kind"] == "change" and data["changes"] == [{"field": "mode", "from": "off", "to": "heat"}]
    assert pol.entry_from_dict(json.loads(json.dumps(data))) == original
    assert "current" not in data  # absent values are not written


def test_damaged_stored_entries_are_rejected():
    for bad in (
        None,
        "x",
        [],
        {"at": 1},
        {"at": T0, "room": "r", "entity_id": VT_CARLO, "kind": "change", "changes": "nope"},
        {"at": T0, "room": "r", "entity_id": VT_CARLO, "changes": [1]},
    ):
        with pytest.raises((TypeError, ValueError)):
            pol.entry_from_dict(bad)


# ------------------------------------------------------------------ the journal


def test_append_orders_deduplicates_and_bounds():
    journal = pol.RoomJournal(max_entries_per_room=3)
    assert journal.append(entry(T0 + 10)) and not journal.append(entry(T0 + 10))  # same row twice
    journal.append(entry(T0 + 30, changes=(pol.Change("target", 1, 2),)))
    journal.append(entry(T0 + 20, changes=(pol.Change("preset", "a", "b"),)))  # a late row goes in its place
    assert [row.at for row in journal.query("camera_carlo")[0]] == [T0 + 30, T0 + 20, T0 + 10]
    journal.append(entry(T0 + 40, changes=(pol.Change("fan", "a", "b"),)))
    rows, total = journal.query("camera_carlo")
    assert total == 3 and [row.at for row in rows] == [T0 + 40, T0 + 30, T0 + 20]
    assert journal.append(entry(T0 + 10))  # the dropped row's key is free again


def test_prune_by_age():
    journal = pol.RoomJournal(max_age_s=100)
    for i in range(5):
        journal.append(entry(T0 + i * 40, changes=(pol.Change("target", i, i + 1),)))
    assert journal.prune(T0 + 200) == 3  # rows at 0, 40, 80 are older than now - 100
    assert journal.count("camera_carlo") == 2
    with pytest.raises(ValueError):
        journal.prune(float("nan"))


def test_query_is_newest_first_filtered_and_paginated_per_room():
    journal = pol.RoomJournal()
    for i in range(7):
        journal.append(entry(T0 + i, changes=(pol.Change("target", i, i + 1),)))
    journal.append(entry(T0 + 3, room="cucina", entity=VT_CUCINA))
    rows, total = journal.query("camera_carlo", limit=3)
    assert total == 7 and [r.at for r in rows] == [T0 + 6, T0 + 5, T0 + 4]
    rows, _ = journal.query("camera_carlo", limit=3, offset=6)
    assert [r.at for r in rows] == [T0]
    rows, total = journal.query("camera_carlo", since=T0 + 2, until=T0 + 4)
    assert total == 3 and all(r.room == "camera_carlo" for r in rows)  # another room never leaks
    assert journal.query("sala") == ([], 0)
    for kw in ({"limit": 0}, {"limit": 501}, {"limit": True}, {"offset": -1}, {"since": float("nan")}, {"until": "x"}):
        with pytest.raises(ValueError):
            journal.query("camera_carlo", **kw)


def test_retention_must_be_positive():
    for kw in ({"max_age_s": 0}, {"max_age_s": float("nan")}, {"max_entries_per_room": 0}):
        with pytest.raises(ValueError):
            pol.RoomJournal(**kw)


def test_load_drops_damaged_rows_but_rejects_a_foreign_schema():
    journal = pol.RoomJournal()
    journal.append(entry())
    data = journal.to_dict()
    data["rooms"]["camera_carlo"].append({"at": "bad"})
    data["rooms"]["camera_carlo"].append(entry(T0 + 9, room="cucina", entity=VT_CUCINA).to_dict())  # a row filed under the wrong room
    other = pol.RoomJournal()
    assert other.load(data) and other.count("camera_carlo") == 1 and other.dropped_on_load == 2
    for bad in (None, {"schema_version": 2, "rooms": {}}, {"schema_version": True, "rooms": {}}, {"schema_version": 1, "rooms": []}, {"schema_version": 1, "rooms": {"r": "x"}}):
        assert pol.RoomJournal().load(bad) is False


# ---------------------------------------------------------------------- runtime


class Plant:
    def __init__(self, stored: dict | None = None, **kw: Any) -> None:
        self.clock = T0
        self.candidates: dict[str, tuple[str, str | None]] = {}
        self.timers: list[list[Any]] = []
        self.saved: dict | None = copy.deepcopy(stored)
        self.saves = 0
        self.save_fail = False
        self.notifications: list[tuple[str, str | None]] = []
        rooms = [rt.RoomJournalConfig("camera_carlo", (VT_CARLO, TRV_CARLO, SPLIT_CARLO)), rt.RoomJournalConfig("cucina", (VT_CUCINA, TRV_CUCINA))]
        self.runtime = rt.RoomJournalRuntime(rooms, self, stored=stored, **kw)

    def now(self) -> float:
        return self.clock

    def candidates_(self):
        return self.candidates

    async def save(self, data: dict) -> None:
        if self.save_fail:
            raise OSError("disk full")
        self.saved = copy.deepcopy(data)
        self.saves += 1

    def call_at(self, at: float, callback: Callable[[], None]) -> Callable[[], None]:
        entry_ = [at, callback, True]
        self.timers.append(entry_)

        def cancel() -> None:
            entry_[2] = False

        return cancel

    def notify(self, notification_id: str, title: str | None, message: str | None) -> None:
        self.notifications.append((notification_id, title))

    async def settle(self) -> None:
        for _ in range(30):
            await asyncio.sleep(0)

    async def advance(self, seconds: float, step: float = 5.0) -> None:
        end = self.clock + seconds
        while self.clock < end:
            self.clock = min(end, self.clock + step)
            due = [t for t in self.timers if t[2] and t[0] <= self.clock]
            self.timers = [t for t in self.timers if t[2] and t[0] > self.clock]
            for item in sorted(due, key=lambda t: t[0]):
                item[1]()
            await self.settle()


def make(**kw):
    plant = Plant(**kw)
    plant.runtime._ports = types.SimpleNamespace(now=plant.now, candidates=lambda: plant.candidates, save=plant.save, call_at=plant.call_at, notify=plant.notify)
    return plant


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_a_state_change_becomes_a_row_with_its_author():
    async def scenario():
        plant = make()
        await plant.runtime.start()
        plant.candidates["ctx-1"] = ("automation.scelta", "Scelta radiatore")
        got = plant.runtime.on_state_change(VT_CARLO, view("off"), view("heat", temperature=21.0), context_id="ctx-1", user_id=None)
        assert got and got.source == "automation.scelta" and got.source_name == "Scelta radiatore" and got.reason == "state_report"
        rows = plant.runtime.query("camera_carlo")["entries"]
        assert rows[0]["changes"][0] == {"field": "mode", "from": "off", "to": "heat"} and rows[0]["source"] == "automation.scelta"

    run(scenario())


def test_an_unknown_author_stays_unknown_and_a_user_id_is_kept_as_given():
    async def scenario():
        plant = make()
        await plant.runtime.start()
        remote = plant.runtime.on_state_change(SPLIT_CARLO, view("off"), view("heat"))
        assert remote.source is None and remote.user_id is None  # nothing is attributed to "the user" by guess
        user = plant.runtime.on_state_change(SPLIT_CARLO, view("heat"), view("off"), context_id="ctx-x", user_id="abc")
        assert user.user_id == "abc" and user.source is None

    run(scenario())


def test_nothing_is_recorded_for_noise_or_for_an_entity_of_no_room():
    async def scenario():
        plant = make()
        await plant.runtime.start()
        before = plant.runtime.query("camera_carlo")["total"]
        assert plant.runtime.on_state_change(VT_CARLO, view("heat", current_temperature=18.0), view("heat", current_temperature=18.4)) is None
        assert plant.runtime.on_state_change("climate.altro", view("off"), view("heat")) is None
        assert plant.runtime.on_state_change(VT_CARLO, None, view("heat")) is None
        assert plant.runtime.query("camera_carlo")["total"] == before

    run(scenario())


def test_rooms_never_leak_into_each_other():
    async def scenario():
        plant = make()
        await plant.runtime.start()
        plant.runtime.on_state_change(VT_CUCINA, view("off"), view("heat"))
        carlo = {row["entity_id"] for row in plant.runtime.query("camera_carlo")["entries"]}
        assert VT_CUCINA not in carlo and TRV_CUCINA not in carlo
        with pytest.raises(ValueError):
            plant.runtime.query("sala")

    run(scenario())


def test_an_entity_in_two_rooms_is_refused():
    with pytest.raises(ValueError):
        rt.RoomJournalRuntime([rt.RoomJournalConfig("a", (VT_CARLO,)), rt.RoomJournalConfig("b", (VT_CARLO,))], types.SimpleNamespace())


def test_start_writes_one_initialized_row_per_empty_room_and_not_again():
    async def scenario():
        plant = make()
        await plant.runtime.start()
        for room in ("camera_carlo", "cucina"):
            rows = plant.runtime.query(room)["entries"]
            assert len(rows) == 1 and rows[0]["kind"] == "initialized"
        await plant.advance(40)
        other = make(stored=plant.saved)
        await other.runtime.start()
        assert other.runtime.query("camera_carlo")["total"] == 1  # a restart does not add another announcement

    run(scenario())


def test_events_of_the_integration_are_recorded_and_bad_ones_lose_only_the_row():
    async def scenario():
        plant = make()
        await plant.runtime.start()
        start = plant.runtime.record_event("camera_carlo", "pdc_start", VT_CARLO, reason="heat_demand", current=18.2, target=21.0, power=1100)
        assert start and start.kind == "pdc_start" and start.power == 1100.0
        assert plant.runtime.record_event("camera_carlo", "change", VT_CARLO) is None  # a change comes from a state, not from an event
        assert plant.runtime.record_event("sala", "pdc_start", VT_CARLO) is None
        assert plant.runtime.record_event("camera_carlo", "pdc_stop", VT_CARLO, power=float("nan")) is None

    run(scenario())


def test_saves_are_delayed_batched_and_flushed_at_stop():
    async def scenario():
        plant = make()
        await plant.runtime.start()
        for i in range(10):
            plant.runtime.on_state_change(VT_CARLO, view("heat", temperature=i), view("heat", temperature=i + 1))
        assert plant.saves == 0  # no write per row
        await plant.advance(31)
        assert plant.saves == 1 and plant.runtime.status("camera_carlo")["journal_unsaved"] is False
        plant.runtime.on_state_change(VT_CARLO, view("heat", temperature=50), view("heat", temperature=51))
        await plant.runtime.stop()  # the last minutes are not lost
        assert plant.saves == 2 and plant.saved["rooms"]["camera_carlo"][-1]["changes"][0]["to"] == 51

    run(scenario())


def test_a_failed_save_keeps_the_rows_reports_the_error_and_retries():
    async def scenario():
        plant = make()
        await plant.runtime.start()
        plant.save_fail = True
        plant.runtime.on_state_change(VT_CARLO, view("off"), view("heat"))
        await plant.advance(31)
        status = plant.runtime.status("camera_carlo")
        assert status["journal_unsaved"] is True and status["journal_error"].startswith("store_save_failed")
        assert plant.runtime.query("camera_carlo")["total"] == 2  # still there (initialized + change)
        plant.save_fail = False
        await plant.advance(70)
        assert plant.saves == 1 and plant.runtime.status("camera_carlo")["journal_error"] is None

    run(scenario())


def test_retention_is_applied_at_every_save():
    async def scenario():
        plant = make(max_age_s=100)
        await plant.runtime.start()
        plant.runtime.on_state_change(VT_CARLO, view("off"), view("heat"))
        await plant.advance(31)
        plant.clock += 500
        plant.runtime.on_state_change(VT_CARLO, view("heat"), view("off"))
        await plant.advance(31)
        rows = plant.saved["rooms"]["camera_carlo"]
        assert len(rows) == 1 and rows[0]["changes"][0]["to"] == "off"

    run(scenario())


@pytest.mark.parametrize("stored", [{"schema_version": 7, "rooms": {}}, {"schema_version": 1, "rooms": 3}, "garbage"])
def test_an_unreadable_store_keeps_the_journal_in_memory_and_is_never_overwritten(stored):
    async def scenario():
        plant = make(stored=stored)
        await plant.runtime.start()
        assert plant.runtime.load_error == "store_invalid"
        assert any(n[0] == "room_journal_store_unreadable" and n[1] for n in plant.notifications)
        got = plant.runtime.on_state_change(VT_CARLO, view("off"), view("heat"))
        assert got is not None  # the journal still records
        await plant.advance(120)
        await plant.runtime.stop()
        assert plant.saves == 0 and plant.saved == stored

    run(scenario())


def test_journal_data_is_json_serialisable_end_to_end():
    async def scenario():
        plant = make()
        await plant.runtime.start()
        plant.runtime.on_state_change(VT_CARLO, view("off", preset_mode="eco"), view("heat", preset_mode="comfort", temperature=21.5))
        await plant.runtime.stop()
        assert json.loads(json.dumps(plant.saved)) == plant.saved
        assert json.dumps(plant.runtime.query("camera_carlo"))

    run(scenario())
