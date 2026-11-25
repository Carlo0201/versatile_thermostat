"""The timers of the room runtimes must run in the event loop (regression of ``no running event loop``).

``async_track_point_in_utc_time`` runs a plain function in an executor thread; a ``@callback`` runs in the loop.
The journal save timer and the override wake timer start asyncio tasks, so they only work from the loop.
The component module is loaded with a minimal stand-in for the few Home Assistant names it imports; no Home
Assistant code runs. Run with only pytest installed::

    python -m pytest -q --noconftest --import-mode=importlib -p no:cacheprovider tests/test_room_timers_callback.py
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import threading
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_DIR = REPO_ROOT / "custom_components" / "versatile_thermostat"
PKG = "_vtherm_room_timers_pkg_under_test"
STUBS = (
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.persistent_notification",
    "homeassistant.core",
    "homeassistant.exceptions",
    "homeassistant.helpers",
    "homeassistant.helpers.event",
    "homeassistant.helpers.storage",
    "homeassistant.util",
    "homeassistant.util.dt",
)


def _install_stubs(monkeypatch):
    """Same marker as Home Assistant: ``callback`` sets ``_hass_callback``; ``track`` records the action."""
    recorded: list = []

    def callback(func):
        func._hass_callback = True  # noqa: SLF001 - what homeassistant.core.callback does
        return func

    modules = {name: types.ModuleType(name) for name in STUBS}
    modules["homeassistant.components"].persistent_notification = modules["homeassistant.components.persistent_notification"]
    core = modules["homeassistant.core"]
    core.callback, core.Event, core.HomeAssistant = callback, object, object
    modules["homeassistant.exceptions"].HomeAssistantError = Exception
    event = modules["homeassistant.helpers.event"]
    event.async_track_point_in_utc_time = lambda hass, action, when: recorded.append((action, when)) or (lambda: None)
    event.async_track_state_change_event = lambda *a, **k: (lambda: None)
    modules["homeassistant.helpers.storage"].Store = object
    modules["homeassistant.util.dt"].utcnow = lambda: None
    modules["homeassistant.util"].dt = modules["homeassistant.util.dt"]
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    package = types.ModuleType(PKG)
    package.__path__ = [str(COMPONENT_DIR)]
    monkeypatch.setitem(sys.modules, PKG, package)
    for name in list(sys.modules):
        if name.startswith(PKG + "."):
            monkeypatch.delitem(sys.modules, name)
    return importlib.import_module(f"{PKG}.feature_heating_room_manager"), recorded


def test_the_timer_action_is_a_home_assistant_callback(monkeypatch):
    manager, recorded = _install_stubs(monkeypatch)
    ran: list[int] = []
    manager._RoomPorts(object(), object()).call_at(1_800_000_000.0, lambda: ran.append(1))  # noqa: SLF001
    ((action, when),) = recorded
    assert getattr(action, "_hass_callback", False) is True, "a plain function would run in an executor thread"
    action(None)
    assert ran == [1]
    assert when.timestamp() == 1_800_000_000.0


def test_a_task_can_be_started_from_the_timer_action(monkeypatch):
    asyncio.run(_task_scenario(monkeypatch))


async def _task_scenario(monkeypatch):
    """What the journal does in ``_on_save_timer``: it needs the running loop of the thread that calls it."""
    manager, recorded = _install_stubs(monkeypatch)
    started: list[bool] = []

    async def work():
        started.append(True)

    def on_timer():
        asyncio.get_running_loop().create_task(work())

    manager._RoomPorts(object(), object()).call_at(1_800_000_000.0, on_timer)  # noqa: SLF001
    action, _ = recorded[0]
    # Home Assistant calls a ``@callback`` in the loop thread; this is the case that failed in production.
    action(None)
    await asyncio.sleep(0)
    assert started == [True]
    # And it is the failure the fix removes: the same function from another thread has no running loop.
    error: list[BaseException] = []

    def from_thread():
        try:
            on_timer()
        except RuntimeError as err:
            error.append(err)

    worker = threading.Thread(target=from_thread)
    worker.start()
    worker.join()
    assert error and "no running event loop" in str(error[0])
