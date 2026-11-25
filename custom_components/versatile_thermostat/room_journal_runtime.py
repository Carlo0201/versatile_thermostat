"""Home Assistant independent engine of the per-room action journal.

It owns the persistent journal (``room_journal_policy.RoomJournal``), turns state changes into
entries, resolves the author from exact context matches, saves with a delay (a journal is not worth
a disk write per row) and answers the card's queries. No Home Assistant import.

Rules (``SPECIFICA-OVERRIDE-REGISTRO.md`` section 5):

* **never block the plant**: the journal observes; a failing save or an unreadable store changes
  nothing outside the journal, and every error is reported, never hidden;
* **never overwrite what cannot be read**: an unreadable store keeps the journal in memory only and
  the file is left untouched for diagnosis;
* **the author is a fact or nothing**: an exact context match, or unknown;
* **bounded**: retention by age and by rows per room, enforced at every save.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from .room_journal_policy import (
    DEFAULT_MAX_AGE_S,
    DEFAULT_MAX_ENTRIES_PER_ROOM,
    KIND_CHANGE,
    KIND_INITIALIZED,
    KINDS,
    REASON_STATE_REPORT,
    JournalEntry,
    RoomJournal,
    StateView,
    detect_changes,
    resolve_origin,
)

_LOGGER = logging.getLogger(__name__)

SAVE_DELAY_S: Final = 30.0
SAVE_RETRY_S: Final = 60.0
STORE_NOTIFICATION_ID: Final = "room_journal_store_unreadable"


@dataclass(frozen=True, slots=True)
class RoomJournalConfig:
    """One room: the entities whose actions belong to it (VTherm, its TRVs, the split)."""

    room_key: str
    entity_ids: tuple[str, ...]


class JournalPorts(Protocol):
    """What the engine needs from the host."""

    def now(self) -> float: ...
    def candidates(self) -> Mapping[str, tuple[str, str | None]]:
        """Context id -> (entity_id, friendly name) of the automations, scripts and schedules that ran."""
        ...

    async def save(self, data: dict[str, Any]) -> None: ...
    def call_at(self, at: float, callback: Callable[[], None]) -> Callable[[], None]: ...
    def notify(self, notification_id: str, title: str | None, message: str | None) -> None: ...


class RoomJournalRuntime:
    """The journal of every configured room."""

    def __init__(
        self,
        rooms: list[RoomJournalConfig],
        ports: JournalPorts,
        *,
        stored: Mapping[str, Any] | None = None,
        max_age_s: float = DEFAULT_MAX_AGE_S,
        max_entries_per_room: int = DEFAULT_MAX_ENTRIES_PER_ROOM,
        exclude_entity_ids: tuple[str, ...] = (),
    ) -> None:
        self._ports = ports
        self._rooms = {cfg.room_key: cfg for cfg in rooms}
        self._entity_room: dict[str, str] = {}
        for cfg in rooms:
            for entity_id in cfg.entity_ids:
                if self._entity_room.setdefault(entity_id, cfg.room_key) != cfg.room_key:
                    raise ValueError(f"{entity_id} belongs to two rooms: another room must never leak into a journal")
        self._journal = RoomJournal(max_age_s=max_age_s, max_entries_per_room=max_entries_per_room)
        self._exclude = exclude_entity_ids
        self._load_error: str | None = None
        self._dirty = False
        self._cancel_save: Callable[[], None] | None = None
        self._save_lock = asyncio.Lock()
        self._last_save_error: str | None = None
        self._stopped = False
        self._tasks: set[asyncio.Task[Any]] = set()
        if stored is not None and not self._journal.load(stored):
            self._journal = RoomJournal(max_age_s=max_age_s, max_entries_per_room=max_entries_per_room)
            self._load_error = "store_invalid"

    # --------------------------------------------------------------- status

    @property
    def load_error(self) -> str | None:
        return self._load_error

    @property
    def last_save_error(self) -> str | None:
        return self._last_save_error

    @property
    def dropped_on_load(self) -> int:
        return self._journal.dropped_on_load

    def room_keys(self) -> tuple[str, ...]:
        return tuple(self._rooms)

    def entity_ids(self) -> tuple[str, ...]:
        """Every entity to listen to."""
        return tuple(self._entity_room)

    def status(self, room_key: str) -> dict[str, Any]:
        return {
            "journal_entries": self._journal.count(room_key),
            "journal_error": self._load_error or self._last_save_error,
            "journal_unsaved": self._dirty,
        }

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Announce the start of the recording once per room that has no row yet (the card shows it)."""
        self._stopped = False
        now = self._ports.now()
        if self._load_error is not None:
            self._ports.notify(
                STORE_NOTIFICATION_ID,
                "Registro riscaldamento: stato salvato non utilizzabile",
                f"{self._load_error}: il registro resta solo in memoria e il file non viene sovrascritto. Ripristinare il file di stato da backup.",
            )
        for room_key, cfg in self._rooms.items():
            if self._journal.count(room_key) == 0 and cfg.entity_ids:
                self._add(JournalEntry(now, room_key, cfg.entity_ids[0], KIND_INITIALIZED, reason="initialized"))

    async def stop(self) -> None:
        """Flush what is pending (a journal must not lose the last minutes at a clean stop)."""
        self._stopped = True
        if self._cancel_save is not None:
            self._cancel_save()
            self._cancel_save = None
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._dirty:
            await self.flush()

    # -------------------------------------------------------------- record

    def on_state_change(
        self,
        entity_id: str,
        old: StateView | None,
        new: StateView | None,
        *,
        context_id: str | None = None,
        parent_id: str | None = None,
        user_id: str | None = None,
    ) -> JournalEntry | None:
        """Record the actions of one state change. Returns the entry, or ``None`` when there is nothing to record."""
        room = self._entity_room.get(entity_id)
        if room is None:
            return None
        changes = detect_changes(old, new)
        if not changes:
            return None
        source, source_name = resolve_origin((context_id, parent_id), self._ports.candidates(), exclude=self._exclude)
        try:
            entry = JournalEntry(self._ports.now(), room, entity_id, KIND_CHANGE, changes, REASON_STATE_REPORT, user_id, source, source_name)
        except ValueError as err:  # an over-long name or an odd id: the row is lost, the plant is not
            _LOGGER.warning("Room journal: cannot record a change of %s: %s", entity_id, err)
            return None
        self._add(entry)
        return entry

    def record_event(
        self,
        room_key: str,
        kind: str,
        entity_id: str,
        *,
        reason: str | None = None,
        current: float | None = None,
        target: float | None = None,
        power: float | None = None,
        source: str | None = None,
        source_name: str | None = None,
        user_id: str | None = None,
    ) -> JournalEntry | None:
        """Record an event of the integration itself (PDC start/stop, override start/end). A bad value loses the row only."""
        if room_key not in self._rooms or kind not in KINDS or kind == KIND_CHANGE:
            return None
        try:
            entry = JournalEntry(self._ports.now(), room_key, entity_id, kind, (), reason, user_id, source, source_name, current, target, power)
        except ValueError as err:
            _LOGGER.warning("Room journal: cannot record %s for %s: %s", kind, room_key, err)
            return None
        self._add(entry)
        return entry

    def _add(self, entry: JournalEntry) -> None:
        if self._journal.append(entry):
            self._dirty = True
            self._arm_save(SAVE_DELAY_S)

    # ---------------------------------------------------------------- query

    def query(self, room_key: str, *, since: float | None = None, until: float | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """The card's query: newest first, paginated. Another room's rows are never returned."""
        if room_key not in self._rooms:
            raise ValueError("unknown_room")
        rows, total = self._journal.query(room_key, since=since, until=until, limit=limit, offset=offset)
        return {"room": room_key, "total": total, "offset": offset, "limit": limit, "has_more": offset + len(rows) < total, "entries": [row.to_dict() for row in rows]}

    # ---------------------------------------------------------- persistence

    def _arm_save(self, delay: float) -> None:
        if self._stopped or self._cancel_save is not None:
            return
        self._cancel_save = self._ports.call_at(self._ports.now() + delay, self._on_save_timer)

    def _on_save_timer(self) -> None:
        self._cancel_save = None
        task = asyncio.get_running_loop().create_task(self.flush())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def flush(self) -> bool:
        """Prune, then save. A failure keeps the rows in memory and retries; an unreadable store is never replaced."""
        async with self._save_lock:
            self._journal.prune(self._ports.now())
            if self._load_error is not None:
                self._dirty = False  # memory only: nothing to retry
                return False
            data = self._journal.to_dict()
            self._dirty = False
            try:
                await self._ports.save(data)
            except asyncio.CancelledError:
                self._dirty = True
                raise
            except Exception as err:  # noqa: BLE001 - the journal never stops the plant
                self._dirty = True
                self._last_save_error = f"store_save_failed:{type(err).__name__}"
                _LOGGER.warning("Room journal: cannot save: %s", err)
                self._arm_save(SAVE_RETRY_S)
                return False
        self._last_save_error = None
        return True
