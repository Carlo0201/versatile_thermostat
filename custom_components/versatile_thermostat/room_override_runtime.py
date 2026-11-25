"""Home Assistant independent engine of the timed room override.

It executes :mod:`room_override_policy` against ports (states, services, a store, a wake-up
timer): no Home Assistant import, so it runs under a simulated plant with a controlled clock.

Rules (specification section 8 of ``SPECIFICA-OVERRIDE-REGISTRO.md``):

* **write-ahead**: the override (and its end sequence) is persisted before the first command,
  so a restart in the middle resumes it instead of forgetting a program that is switched off;
* **confirmation**: the pause and restore sequences retry until every Scheduler switch is
  confirmed in the desired state. Unknown states never count as enabled or disabled;
* **completed restore**: after Scheduler accepts the current-slot commands, record that phase
  before clearing the override so a failed cleanup can be retried without repeating commands;
* **one owner**: the *start* is refused while a declared legacy owner (the old
  ``riprendi_programma`` automation) is not exactly ``off``. The *end* is always allowed: it is
  idempotent and refusing it would leave the program switched off for good;
* **expiry is wall-clock**: ``ends_at`` is epoch seconds, so it survives restarts and DST changes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any, Final, Protocol

from .room_override_policy import (
    IDLE,
    MODE_UNKNOWN,
    SCHEDULER_ENABLED,
    SCHEDULER_KNOWN,
    STORE_SCHEMA_VERSION,
    SWITCH_OFF,
    OverrideState,
    begin_ending,
    failed_attempt,
    is_due,
    program_mode,
    programs_to_run,
    remaining_s,
    start_override,
)

_LOGGER = logging.getLogger(__name__)

RETRY_S: Final = 30.0
NOTIFY_AFTER_ATTEMPTS: Final = 5

RESULT_STARTED: Final = "started"
RESULT_RESTARTED: Final = "restarted"
RESULT_ENDED: Final = "ended"
RESULT_NOT_ACTIVE: Final = "not_active"
RESULT_UNKNOWN_ROOM: Final = "unknown_room"
RESULT_BLOCKED_LEGACY: Final = "blocked_by_legacy"
RESULT_SCHEDULERS_UNKNOWN: Final = "schedulers_unknown"
RESULT_NO_PROGRAM: Final = "no_program"
RESULT_SAVE_FAILED: Final = "store_save_failed"
RESULT_STORE_UNREADABLE: Final = "store_unreadable"
RESULT_INVALID_DURATION: Final = "invalid_duration"
RESULT_PENDING: Final = "pending"  # a command or confirmation is still being retried


@dataclass(frozen=True, slots=True)
class RoomOverrideConfig:
    """One room: its program switches, the working-day sensor and the legacy owners to wait for."""

    room_key: str
    scheduler_entity_ids: tuple[str, ...]
    workday_entity_id: str | None = None
    legacy_owner_entity_ids: tuple[str, ...] = ()


class OverridePorts(Protocol):
    """What the engine needs from the host."""

    def now(self) -> float: ...
    def weekday(self) -> int:
        """Local weekday, 0 = Monday."""
        ...

    def state(self, entity_id: str) -> tuple[str | None, Mapping[str, Any]]: ...
    async def call_service(self, domain: str, service: str, entity_id: str) -> None: ...
    def call_at(self, at: float, callback: Callable[[], None]) -> Callable[[], None]: ...
    async def save(self, data: dict[str, Any]) -> None: ...
    def notify(self, notification_id: str, title: str | None, message: str | None) -> None: ...


class _Room:
    def __init__(self, cfg: RoomOverrideConfig) -> None:
        self.cfg = cfg
        self.state: OverrideState = IDLE
        self.lock = asyncio.Lock()
        self.generation = 0
        self.cancel_wake: Callable[[], None] | None = None
        self.last_error: str | None = None
        self.last_event: str | None = None
        self.notified = False
        self.pending_end_reason: str | None = None
        self.restore_complete = False


class RoomOverrideRuntime:
    """The override engine of every configured room."""

    def __init__(self, rooms: list[RoomOverrideConfig], ports: OverridePorts, *, stored: Mapping[str, Any] | None = None) -> None:
        self._ports = ports
        self._rooms = {cfg.room_key: _Room(cfg) for cfg in rooms}
        self._stopped = False
        self._save_lock = asyncio.Lock()
        self._load_error: str | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._store_notified = False
        if stored is not None:
            self._load(stored)

    # --------------------------------------------------------------- status

    @property
    def load_error(self) -> str | None:
        """Why the persisted state was not used (``None`` when it was)."""
        return self._load_error

    def room_keys(self) -> tuple[str, ...]:
        return tuple(self._rooms)

    def state(self, room_key: str) -> OverrideState:
        return self._rooms[room_key].state

    def status(self, room_key: str) -> dict[str, Any]:
        """Attributes for the room: nothing here is derived from a switch that is not readable."""
        room = self._rooms[room_key]
        now = self._ports.now()
        switches = self._states(room)
        return {
            "override_active": room.state.active,
            "override_pausing": room.state.pausing,
            "override_ending": room.state.ending or room.pending_end_reason is not None,
            "override_started_at": room.state.started_at,
            "override_ends_at": room.state.ends_at,
            "override_duration_min": room.state.duration_min,
            "override_remaining_s": remaining_s(room.state, now),
            "program_mode": MODE_UNKNOWN if self._load_error is not None else program_mode(switches, room.state.active),
            "override_attempts": room.state.attempts,
            "override_last_error": room.last_error or self._load_error,
            "override_last_event": room.last_event,
        }

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Resume what was persisted: a due override ends, a running one is re-armed and its switches kept off."""
        self._stopped = False
        self._notify_store()
        if self._load_error is not None:
            return  # never act on a state we cannot read; the operator is told
        for room in self._rooms.values():
            if room.state.active:
                self._spawn(self._on_wake(room, room.generation))  # look now: repair, end or re-arm

    async def stop(self) -> None:
        """Cancel timers and tasks; the persisted override is left untouched (it resumes at the next start)."""
        self._stopped = True
        for room in self._rooms.values():
            self._cancel_wake(room)
            room.generation += 1
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        # Service calls may enter directly, outside the timer task set.
        for room in self._rooms.values():
            async with room.lock:
                self._cancel_wake(room)

    # ------------------------------------------------------------- commands

    async def start_override(self, room_key: str, minutes: object) -> str:
        """Pause the program for ``minutes`` (restarts a running override). Returns a ``RESULT_*`` code."""
        room = self._rooms.get(room_key)
        if room is None:
            return RESULT_UNKNOWN_ROOM
        async with room.lock:
            if self._stopped:
                return RESULT_PENDING
            if self._load_error is not None:
                return self._fail(room, RESULT_STORE_UNREADABLE)
            if not room.cfg.scheduler_entity_ids:
                return self._fail(room, RESULT_NO_PROGRAM)
            blockers = self._legacy_blockers(room)
            if blockers:
                return self._fail(room, f"{RESULT_BLOCKED_LEGACY}:{','.join(blockers)}")
            if not room.state.active and any(state not in SCHEDULER_KNOWN for state in self._states(room)):
                return self._fail(room, RESULT_SCHEDULERS_UNKNOWN)
            try:
                new_state = start_override(room.state, self._ports.now(), minutes)
            except ValueError:
                return self._fail(room, RESULT_INVALID_DURATION)
            was_active = room.state.active
            planned = tuple(
                entity_id for entity_id in room.cfg.scheduler_entity_ids
                if self._ports.state(entity_id)[0] in SCHEDULER_ENABLED or entity_id in room.state.paused_schedulers
            )
            new_state = replace(new_state, paused_schedulers=planned)
            if not await self._save(room, new_state):
                return self._fail(room, RESULT_SAVE_FAILED)
            room.generation += 1
            room.pending_end_reason = None
            room.restore_complete = False
            room.last_error = None
            room.last_event = "pause_pending"
            if not await self._continue_pause(room):
                return RESULT_PENDING
            room.last_event = RESULT_RESTARTED if was_active else RESULT_STARTED
            return room.last_event

    async def cancel_override(self, room_key: str) -> str:
        """Give the program back now (also ``resume_program``): same end sequence as the expiry."""
        room = self._rooms.get(room_key)
        if room is None:
            return RESULT_UNKNOWN_ROOM
        async with room.lock:
            if self._stopped:
                return RESULT_PENDING
            if self._load_error is not None:
                return self._fail(room, RESULT_STORE_UNREADABLE)
            if not room.state.active:
                return RESULT_NOT_ACTIVE
            return await self._end(room, "cancelled")

    # --------------------------------------------------------------- engine

    def _fail(self, room: _Room, code: str) -> str:
        room.last_error = code
        return code.split(":", 1)[0]

    def _legacy_blockers(self, room: _Room) -> list[str]:
        return [entity_id for entity_id in room.cfg.legacy_owner_entity_ids if self._ports.state(entity_id)[0] != SWITCH_OFF]

    def _states(self, room: _Room) -> list[str | None]:
        return [self._ports.state(entity_id)[0] for entity_id in room.cfg.scheduler_entity_ids]

    async def _continue_pause(self, room: _Room) -> bool:
        """Finish the persisted pause, or retry before the override's deadline."""
        if self._stopped:
            return False
        if is_due(room.state, self._ports.now()):
            self._wake(room, self._ports.now())
            return False
        for entity_id in room.state.paused_schedulers:
            if self._stopped:
                return False
            if self._ports.state(entity_id)[0] not in SCHEDULER_ENABLED:
                continue
            if self._legacy_blockers(room):
                self._fail(room, RESULT_BLOCKED_LEGACY)
                self._wake_later(room)
                return False
            try:
                await self._ports.call_service("switch", "turn_off", entity_id)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - retried by the next wake
                room.last_error = f"scheduler_off_failed:{type(err).__name__}"
                _LOGGER.warning("Room override %s: turn_off(%s) failed: %s", room.cfg.room_key, entity_id, err)
        if any(self._ports.state(entity_id)[0] != SWITCH_OFF for entity_id in room.state.paused_schedulers):
            if not room.last_error or not room.last_error.startswith("scheduler_off_failed:"):
                room.last_error = "scheduler_off_not_confirmed"
            self._wake_later(room)
            return False
        if self._stopped or is_due(room.state, self._ports.now()):
            self._wake(room, self._ports.now())
            return False
        if not await self._save(room, replace(room.state, pausing=False)):
            self._fail(room, RESULT_SAVE_FAILED)
            self._wake_later(room)
            return False
        room.last_error = None
        self._wake(room, self._ports.now())
        return not self._stopped and not is_due(room.state, self._ports.now())

    async def _end(self, room: _Room, reason: str) -> str:
        """Advance the end sequence with the room lock held."""
        self._cancel_wake(room)
        reason = room.state.end_reason or room.pending_end_reason or reason
        room.pending_end_reason = reason
        if not room.restore_complete and not room.state.restored:
            if not room.state.ending and not await self._save(room, begin_ending(room.state, reason)):
                self._wake_later(room)
                return self._fail(room, RESULT_SAVE_FAILED)
            if self._stopped:
                return RESULT_PENDING
            done = await self._run_end_commands(room)
            if not done:
                if self._stopped:
                    return RESULT_PENDING
                # Keep counting command failures even when the disk is unavailable.
                room.state = failed_attempt(room.state)
                await self._save(room, room.state)
                self._notify_stuck(room)
                self._wake_later(room)
                return RESULT_PENDING
            room.restore_complete = True
        if not room.state.restored:
            restored = replace(begin_ending(room.state, reason), restored=True)
            if not await self._save(room, restored):
                self._wake_later(room)
                return self._fail(room, RESULT_SAVE_FAILED)
        if not await self._save(room, IDLE):
            self._wake_later(room)
            return self._fail(room, RESULT_SAVE_FAILED)
        room.pending_end_reason = None
        room.restore_complete = False
        room.last_error = None
        room.last_event = f"ended:{reason}"
        self._notify_stuck(room)
        return RESULT_ENDED

    async def _run_end_commands(self, room: _Room) -> bool:
        ok = True
        paused = room.state.paused_schedulers
        for entity_id in paused:
            if self._stopped or self._legacy_blockers(room):
                return False
            state, _ = self._ports.state(entity_id)
            if state in SCHEDULER_ENABLED:
                continue
            if state != SWITCH_OFF:
                room.last_error = "scheduler_state_unknown"
                return False
            try:
                await self._ports.call_service("switch", "turn_on", entity_id)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                room.last_error = f"scheduler_on_failed:{type(err).__name__}"
                _LOGGER.warning("Room override %s: turn_on(%s) failed: %s", room.cfg.room_key, entity_id, err)
                ok = False
        if any(self._ports.state(entity_id)[0] not in SCHEDULER_ENABLED for entity_id in paused):
            room.last_error = room.last_error or "scheduler_on_not_confirmed"
            return False
        if not ok:
            return False
        workday = self._ports.state(room.cfg.workday_entity_id)[0] if room.cfg.workday_entity_id else None
        programs = [(entity_id, self._ports.state(entity_id)[1].get("weekdays")) for entity_id in paused]
        for entity_id in programs_to_run(programs, self._ports.weekday(), workday):
            if entity_id in room.state.resumed_actions:
                continue
            if self._stopped or self._legacy_blockers(room) or self._ports.state(entity_id)[0] not in SCHEDULER_ENABLED:
                return False
            try:
                await self._ports.call_service("scheduler", "run_action", entity_id)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                room.last_error = f"run_action_failed:{type(err).__name__}"
                _LOGGER.warning("Room override %s: run_action(%s) failed: %s", room.cfg.room_key, entity_id, err)
                ok = False
            else:
                room.state = replace(room.state, resumed_actions=(*room.state.resumed_actions, entity_id))
                if not await self._save(room, room.state):
                    return False
        return ok

    # ----------------------------------------------------------- scheduling

    def _wake(self, room: _Room, now: float) -> None:
        """Arm the next look at this room: the expiry, or now when it is already due / its end is pending."""
        self._cancel_wake(room)
        if self._stopped or not room.state.active:
            return
        generation = room.generation
        at = now if is_due(room.state, now) else (room.state.ends_at or now)
        room.cancel_wake = self._ports.call_at(at, lambda: self._spawn(self._on_wake(room, generation)))

    def _wake_later(self, room: _Room) -> None:
        self._cancel_wake(room)
        if self._stopped or not room.state.active:
            return
        generation = room.generation
        at = self._ports.now() + RETRY_S
        if not room.pending_end_reason and not room.state.ending and room.state.ends_at is not None:
            at = min(at, room.state.ends_at)
        room.cancel_wake = self._ports.call_at(at, lambda: self._spawn(self._on_wake(room, generation)))

    def _cancel_wake(self, room: _Room) -> None:
        if room.cancel_wake is not None:
            room.cancel_wake()
            room.cancel_wake = None

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _on_wake(self, room: _Room, generation: int) -> None:
        async with room.lock:
            if self._stopped or generation != room.generation or not room.state.active:
                return
            now = self._ports.now()
            if room.pending_end_reason or room.state.ending:
                await self._end(room, "expired")
                return
            states = self._states(room)
            if not room.state.pausing and room.state.paused_schedulers and all(
                self._ports.state(entity_id)[0] in SCHEDULER_ENABLED for entity_id in room.state.paused_schedulers
            ):
                # An already confirmed pause was released externally.
                room.restore_complete = True
                await self._end(room, "externally_resumed")
                return
            if is_due(room.state, now):
                await self._end(room, "expired")
                return
            if not room.state.pausing:
                if any(state not in SCHEDULER_KNOWN for state in states):
                    self._wake_later(room)
                    return
                if all(state == SWITCH_OFF for state in states):
                    self._wake(room, now)
                    return
                if not await self._save(room, replace(room.state, pausing=True)):
                    self._fail(room, RESULT_SAVE_FAILED)
                    self._wake_later(room)
                    return
            if await self._continue_pause(room):
                room.last_event = "paused"

    # ---------------------------------------------------------- persistence

    def _store_data(self, changed_room: _Room, state: OverrideState) -> dict[str, Any]:
        return {
            "schema_version": STORE_SCHEMA_VERSION,
            "rooms": {key: asdict(state if room is changed_room else room.state) for key, room in self._rooms.items()},
        }

    async def _save(self, room: _Room, state: OverrideState) -> bool:
        if self._load_error is not None:
            return False  # never replace an unreadable store with defaults
        async with self._save_lock:
            try:
                await self._ports.save(self._store_data(room, state))
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Room override: cannot save the state: %s", err)
                return False
            room.state = state
            return True

    def _load(self, data: Mapping[str, Any]) -> None:
        if not isinstance(data, Mapping) or type(data.get("schema_version")) is not int or data.get("schema_version") != STORE_SCHEMA_VERSION:
            self._load_error = "store_invalid"
            return
        rooms = data.get("rooms")
        if not isinstance(rooms, Mapping):
            self._load_error = "rooms_invalid"
            return
        if any(key not in self._rooms and isinstance(value, Mapping) and value.get("active") is True for key, value in rooms.items()):
            self._load_error = "active_room_removed"
            return
        for key, room in self._rooms.items():
            item = rooms.get(key)
            if item is None:
                continue
            try:
                if not isinstance(item, Mapping) or not isinstance(item.get("active"), bool):
                    raise ValueError("active")
                if any(not isinstance(item.get(flag, False), bool) for flag in ("ending", "pausing", "restored")):
                    raise ValueError("invalid override phase")
                if item["active"]:
                    paused = item.get("paused_schedulers")
                    resumed = item.get("resumed_actions", ())
                    if not isinstance(paused, (list, tuple)) or any(entity_id not in room.cfg.scheduler_entity_ids for entity_id in paused):
                        raise ValueError("invalid saved scheduler binding")
                    if not isinstance(resumed, (list, tuple)) or any(entity_id not in paused for entity_id in resumed):
                        raise ValueError("invalid saved scheduler action")
                room.state = (
                    OverrideState(
                        active=True,
                        started_at=item.get("started_at"),
                        ends_at=item.get("ends_at"),
                        duration_min=item.get("duration_min"),
                        ending=item.get("ending", False),
                        attempts=item.get("attempts", 0),
                        pausing=item.get("pausing", False),
                        restored=item.get("restored", False),
                        end_reason=item.get("end_reason"),
                        paused_schedulers=tuple(item["paused_schedulers"]),
                        resumed_actions=tuple(item.get("resumed_actions", ())),
                    )
                    if item["active"]
                    else IDLE
                )
            except (TypeError, ValueError):
                self._load_error = f"override_invalid:{key}"
                return

    def _notify_store(self) -> None:
        if self._load_error is not None and not self._store_notified:
            self._store_notified = True
            self._ports.notify(
                "room_override_store_unreadable",
                "Override stanza: stato salvato non utilizzabile",
                f"L'override a tempo e' fermo: {self._load_error}. Gli switch dei programmi non vengono toccati. "
                "Verificare quali stanze hanno il programma spento e ripristinare il file di stato da backup.",
            )

    def _notify_stuck(self, room: _Room) -> None:
        notification = f"{room.cfg.room_key}_override_stuck"
        if room.state.active and room.state.attempts >= NOTIFY_AFTER_ATTEMPTS and not room.notified:
            room.notified = True
            self._ports.notify(
                notification,
                "Override stanza: programma non ripreso",
                f"{room.cfg.room_key}: dopo {room.state.attempts} tentativi il programma non risulta ripreso ({room.last_error or 'non confermato'}). Verificare gli switch Scheduler.",
            )
        elif not room.state.active and room.notified:
            room.notified = False
            self._ports.notify(notification, None, None)
