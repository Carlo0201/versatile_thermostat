"""Pure model of the per-room action journal (the "Registro" of the Oikos room cards).

It replaces the automation ``riscaldamento_registro_azioni_per_stanza`` and the ``logbook.log``
rows with a persistent, bounded journal owned by the integration. The entry layout keeps the field
names of the ``OIKOS_HEATING_V1`` rows that the card already understands (``kind``, ``changes`` with
``field``/``from``/``to``, ``reason``, ``user_id``, ``source``, ``source_name``, ``current``,
``target``, ``power``) so that a card can read both sources.

Rules inherited from the automation, now enforced here and tested:

* only a change of the *mode* or of the target, preset, activity, fan or swing is an action; the
  measured temperature and every diagnostic attribute are ignored;
* an old or a new state that does not exist is not a change (no row at start-up for a first state);
* the author comes only from an exact context match supplied by the caller. A missing author stays
  missing: it is never guessed (a remote control has no context, the journal never says "user");
* values must be scalars: anything else is turned into a short text, never into a nested object.

No Home Assistant import, no I/O, no clock, no asyncio.
"""

from __future__ import annotations

import json
import math
import re
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

STORE_SCHEMA_VERSION: Final = 1

KIND_CHANGE: Final = "change"
KIND_PDC_START: Final = "pdc_start"
KIND_PDC_STOP: Final = "pdc_stop"
KIND_INITIALIZED: Final = "initialized"
KIND_OVERRIDE_START: Final = "override_start"
KIND_OVERRIDE_END: Final = "override_end"
KINDS: Final = (KIND_CHANGE, KIND_PDC_START, KIND_PDC_STOP, KIND_INITIALIZED, KIND_OVERRIDE_START, KIND_OVERRIDE_END)

REASON_STATE_REPORT: Final = "state_report"

# journal field -> state attribute; ``mode`` is the state itself
FIELD_MODE: Final = "mode"
FIELD_ATTRIBUTES: Final = {"target": "temperature", "preset": "preset_mode", "activity": "hvac_action", "fan": "fan_mode", "swing": "swing_mode"}

DEFAULT_MAX_AGE_S: Final = 7 * 86400.0  # the card asks for at most 168 hours
DEFAULT_MAX_ENTRIES_PER_ROOM: Final = 2000
MAX_QUERY_LIMIT: Final = 500
MAX_TEXT: Final = 160
MAX_CHANGES: Final = 12

ROOM_KEY_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ENTITY_ID_PATTERN: Final = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


def _scalar(value: Any) -> str | int | float | bool | None:
    """A JSON scalar for a state value: nested values become a short text, NaN/inf become ``None``."""
    if value is None or isinstance(value, (bool, str)):
        return value if not isinstance(value, str) else value[:MAX_TEXT]
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    return str(value)[:MAX_TEXT]


def _text(name: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise ValueError(f"{name} must be text up to {MAX_TEXT} characters")
    return value


def _number(name: str, value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


@dataclass(frozen=True, slots=True)
class Change:
    """One changed field."""

    field: str
    from_value: str | int | float | bool | None
    to_value: str | int | float | bool | None

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "from": self.from_value, "to": self.to_value}


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One row of the journal. ``at`` is epoch seconds (the time zone is applied only for display)."""

    at: float
    room: str
    entity_id: str
    kind: str = KIND_CHANGE
    changes: tuple[Change, ...] = ()
    reason: str | None = None
    user_id: str | None = None
    source: str | None = None
    source_name: str | None = None
    current: float | None = None
    target: float | None = None
    power: float | None = None

    def __post_init__(self) -> None:
        if isinstance(self.at, bool) or not isinstance(self.at, (int, float)) or not math.isfinite(self.at) or self.at < 0:
            raise ValueError("at must be a finite non-negative number")
        if not isinstance(self.room, str) or not ROOM_KEY_PATTERN.match(self.room):
            raise ValueError("invalid room key")
        if not isinstance(self.entity_id, str) or not ENTITY_ID_PATTERN.match(self.entity_id):
            raise ValueError("invalid entity id")
        if self.kind not in KINDS:
            raise ValueError("unknown kind")
        if len(self.changes) > MAX_CHANGES or not all(isinstance(change, Change) and isinstance(change.field, str) and change.field for change in self.changes):
            raise ValueError("invalid changes")
        if self.kind == KIND_CHANGE and not self.changes:
            raise ValueError("a change entry needs at least one change")
        for name in ("reason", "user_id", "source", "source_name"):
            _text(name, getattr(self, name))
        for name in ("current", "target", "power"):
            _number(name, getattr(self, name))

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"at": self.at, "room": self.room, "entity_id": self.entity_id, "kind": self.kind, "changes": [change.to_dict() for change in self.changes]}
        for name in ("reason", "user_id", "source", "source_name", "current", "target", "power"):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        return data

    def dedupe_key(self) -> str:
        return json.dumps([self.entity_id, round(self.at * 1000), self.kind, [change.to_dict() for change in self.changes]], sort_keys=True)


def entry_from_dict(data: Any) -> JournalEntry:
    """Rebuild an entry from its stored form, or ``ValueError`` (a damaged row is dropped by the caller)."""
    if not isinstance(data, Mapping):
        raise ValueError("entry must be a mapping")
    raw_changes = data.get("changes") or []
    if not isinstance(raw_changes, Sequence) or isinstance(raw_changes, (str, bytes)):
        raise ValueError("changes must be a list")
    changes = []
    for item in raw_changes:
        if not isinstance(item, Mapping) or not isinstance(item.get("field"), str):
            raise ValueError("invalid change")
        changes.append(Change(item["field"], _scalar(item.get("from")), _scalar(item.get("to"))))
    return JournalEntry(
        at=data.get("at"),  # type: ignore[arg-type]
        room=data.get("room"),  # type: ignore[arg-type]
        entity_id=data.get("entity_id"),  # type: ignore[arg-type]
        kind=data.get("kind", KIND_CHANGE),
        changes=tuple(changes),
        reason=data.get("reason"),
        user_id=data.get("user_id"),
        source=data.get("source"),
        source_name=data.get("source_name"),
        current=data.get("current"),
        target=data.get("target"),
        power=data.get("power"),
    )


@dataclass(frozen=True, slots=True)
class StateView:
    """What the journal needs of a state: the state text and the attributes."""

    state: str | None
    attributes: Mapping[str, Any] = field(default_factory=dict)


def detect_changes(old: StateView | None, new: StateView | None) -> tuple[Change, ...]:
    """The actions between two states; empty when either side does not exist or only diagnostics moved."""
    if old is None or new is None:
        return ()
    out: list[Change] = []
    if old.state != new.state:
        out.append(Change(FIELD_MODE, _scalar(old.state), _scalar(new.state)))
    for name, attribute in FIELD_ATTRIBUTES.items():
        before, after = old.attributes.get(attribute), new.attributes.get(attribute)
        if before != after:
            out.append(Change(name, _scalar(before), _scalar(after)))
    return tuple(out)


def resolve_origin(context_ids: Iterable[str | None], candidates: Mapping[str, tuple[str, str | None]], *, exclude: Iterable[str] = ()) -> tuple[str | None, str | None]:
    """The automation, script or schedule that owns one of the context ids, only on an exact match.

    ``candidates`` maps a context id to ``(entity_id, friendly_name)``. The first id wins in the
    order given (own context before parent). ``exclude`` removes the journal's own entities. When
    nothing matches the origin is unknown: ``(None, None)``.
    """
    blocked = set(exclude)
    for context_id in context_ids:
        if not context_id:
            continue
        found = candidates.get(context_id)
        if found is not None and found[0] not in blocked:
            return found[0], found[1]
    return None, None


class RoomJournal:
    """Bounded, in-memory journal of every room, with query and (de)serialisation."""

    def __init__(self, *, max_age_s: float = DEFAULT_MAX_AGE_S, max_entries_per_room: int = DEFAULT_MAX_ENTRIES_PER_ROOM) -> None:
        if not math.isfinite(max_age_s) or max_age_s <= 0 or max_entries_per_room < 1:
            raise ValueError("retention must be positive")
        self._max_age_s = float(max_age_s)
        self._max_entries = int(max_entries_per_room)
        self._rooms: dict[str, deque[JournalEntry]] = {}
        self._keys: dict[str, set[str]] = {}
        self.dropped_on_load = 0

    def rooms(self) -> tuple[str, ...]:
        return tuple(self._rooms)

    def count(self, room: str | None = None) -> int:
        if room is not None:
            return len(self._rooms.get(room, ()))
        return sum(len(rows) for rows in self._rooms.values())

    def append(self, entry: JournalEntry) -> bool:
        """Add an entry (kept in time order). A duplicate is ignored: returns whether it was added."""
        rows = self._rooms.setdefault(entry.room, deque())
        keys = self._keys.setdefault(entry.room, set())
        key = entry.dedupe_key()
        if key in keys:
            return False
        keys.add(key)
        if not rows or entry.at >= rows[-1].at:
            rows.append(entry)
        else:  # a late row: insert in order (rare, rows are almost always appended)
            ordered = sorted([*rows, entry], key=lambda row: row.at)
            rows.clear()
            rows.extend(ordered)
        while len(rows) > self._max_entries:
            keys.discard(rows.popleft().dedupe_key())
        return True

    def prune(self, now: float) -> int:
        """Drop what is older than the retention window. Returns how many rows went away."""
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        removed = 0
        for room, rows in self._rooms.items():
            keys = self._keys.setdefault(room, set())
            while rows and rows[0].at < now - self._max_age_s:
                keys.discard(rows.popleft().dedupe_key())
                removed += 1
        return removed

    def query(self, room: str, *, since: float | None = None, until: float | None = None, limit: int = 100, offset: int = 0) -> tuple[list[JournalEntry], int]:
        """Newest first. Returns the page and the total number of matches (so the card can paginate)."""
        for name, value in (("since", since), ("until", until)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise ValueError(f"{name} must be a finite number")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_QUERY_LIMIT:
            raise ValueError(f"limit must be 1..{MAX_QUERY_LIMIT}")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be >= 0")
        rows = [row for row in reversed(self._rooms.get(room, ())) if (since is None or row.at >= since) and (until is None or row.at <= until)]
        return rows[offset : offset + limit], len(rows)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": STORE_SCHEMA_VERSION, "rooms": {room: [row.to_dict() for row in rows] for room, rows in self._rooms.items()}}

    def load(self, data: Any) -> bool:
        """Load a stored journal. Returns ``False`` when the whole store is unusable (schema/shape).

        A single damaged row is dropped and counted in ``dropped_on_load``: a journal is a record, not
        an ownership claim, so one bad row must not hide the others.
        """
        if not isinstance(data, Mapping) or type(data.get("schema_version")) is not int or data.get("schema_version") != STORE_SCHEMA_VERSION:
            return False
        rooms = data.get("rooms")
        if not isinstance(rooms, Mapping):
            return False
        for room, rows in rooms.items():
            if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
                return False
            for item in rows:
                try:
                    entry = entry_from_dict(item)
                except (TypeError, ValueError):
                    self.dropped_on_load += 1
                    continue
                if entry.room != room:
                    self.dropped_on_load += 1
                    continue
                self.append(entry)
        return True
