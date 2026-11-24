"""Tests of the pure manual-override provenance model of a PDC split (M5).

Run with only pytest installed, from the repository root::

    python -m pytest -q --noconftest --import-mode=importlib tests/test_heating_pdc_provenance.py

The expected behavior comes from the baseline automations
``*_registro_comandi_condizionatore``, ``*_override_manuale_condizionatore`` and
``*_fine_override_condizionatore``. The clock is explicit; nothing sleeps.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "custom_components" / "versatile_thermostat" / "heating_pdc_provenance.py"
MODULE_NAME = "_vtherm_pure_heating_pdc_provenance_under_test"


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


v = _load()
T0 = 1_795_500_000.0
SPLIT = "climate.camera_carlo_condizionatore"
PDC = "climate.camera_carlo_pompa_di_calore"


def dev(state: str = "heat", temperature: float = 22.0, fan: str = "low", preset: str = "none", swing: str = "off") -> object:
    return v.DeviceState(state, temperature, fan, preset, swing)


# --- expected-effects register -------------------------------------------------------------------


def test_register_records_each_field_for_75_s_with_integer_expiry():
    reg = v.register_command(v.EMPTY_REGISTER, "set_hvac_mode", {"hvac_mode": "heat"}, T0 + 0.6)
    assert reg.effects == {"m": v.ExpectedEffect("heat", T0 + 75.0)}
    reg = v.register_command(reg, "set_temperature", {"temperature": "21.5"}, T0)
    assert reg.effects["t"] == v.ExpectedEffect(21.5, T0 + 75.0) and "m" in reg.effects  # fields are independent
    reg = v.register_command(reg, "set_fan_mode", {"fan_mode": "high"}, T0)
    reg = v.register_command(reg, "set_swing_mode", {"swing_mode": "vertical"}, T0)
    assert {k: e.value for k, e in reg.effects.items()} == {"m": "heat", "t": 21.5, "f": "high", "s": "vertical"}


@pytest.mark.parametrize(
    ("service", "data", "expected"),
    [
        ("turn_off", {}, {"m": "off", "p": "none"}),
        ("turn_on", {}, {"m": "heat"}),
        ("set_hvac_mode", {"hvac_mode": "off"}, {"m": "off", "p": "none"}),
        ("set_hvac_mode", {"hvac_mode": "cool"}, {"m": "cool"}),
        ("set_preset_mode", {"preset_mode": "quiet"}, {"p": "quiet", "f": "auto"}),
        ("set_preset_mode", {"preset_mode": "none"}, {"p": "none"}),
        ("set_fan_mode", {"fan_mode": "auto"}, {"f": "auto"}),
    ],
)
def test_smartthings_side_effects_are_expected_quiet_gives_fan_auto_and_off_clears_the_preset(service, data, expected):
    reg = v.register_command(v.EMPTY_REGISTER, service, data, T0)
    assert {k: e.value for k, e in reg.effects.items()} == expected
    assert all(e.expires_at == T0 + 75.0 for e in reg.effects.values())


def test_explicit_fan_wins_over_the_quiet_side_effect_and_other_services_are_ignored():
    reg = v.register_command(v.EMPTY_REGISTER, "set_preset_mode", {"preset_mode": "quiet", "fan_mode": "low"}, T0)
    assert reg.effects["f"].value == "low"
    assert v.register_command(reg, "set_humidity", {"humidity": 50}, T0) is reg
    unparsable = v.register_command(v.register_command(v.EMPTY_REGISTER, "set_temperature", {"temperature": 20}, T0), "set_temperature", {"temperature": "abc"}, T0)
    assert "t" not in unparsable.effects


def test_register_drops_unknown_fields_and_copies_its_input():
    source = {"m": v.ExpectedEffect("heat", T0), "zzz": v.ExpectedEffect("x", T0), "t": 3}
    reg = v.ExpectedRegister(source)
    assert set(reg.effects) == {"m"}
    source.clear()
    assert set(reg.effects) == {"m"}


# --- classification of a device change -----------------------------------------------------------


def test_exact_expected_value_before_expiry_is_not_manual():
    reg = v.register_command(v.EMPTY_REGISTER, "set_hvac_mode", {"hvac_mode": "off"}, T0)
    verdict = v.classify_device_change(reg, dev("heat", preset="comfort"), dev("off", preset="none"), T0 + 5.0)
    assert not verdict.manual and verdict.provenance == v.PROVENANCE_EXPECTED
    assert set(verdict.expected_fields) == {"m", "p"} and verdict.manual_fields == ()


@pytest.mark.parametrize(("delay", "manual"), [(74.9, False), (75.0, False), (75.1, True), (200.0, True)])
def test_expected_effect_window_is_75_s_inclusive(delay, manual):
    reg = v.register_command(v.EMPTY_REGISTER, "set_fan_mode", {"fan_mode": "high"}, T0)
    verdict = v.classify_device_change(reg, dev(fan="low"), dev(fan="high"), T0 + delay)
    assert verdict.manual is manual


def test_a_different_value_in_the_same_field_is_manual_with_unknown_author():
    reg = v.register_command(v.EMPTY_REGISTER, "set_temperature", {"temperature": 22.0}, T0)
    verdict = v.classify_device_change(reg, dev(temperature=20.0), dev(temperature=23.0), T0 + 2.0)
    assert verdict.manual and verdict.manual_fields == ("t",) and verdict.provenance == v.PROVENANCE_UNKNOWN


def test_one_degree_changes_are_detected_and_other_fields_do_not_cover_them():
    reg = v.register_command(v.EMPTY_REGISTER, "set_fan_mode", {"fan_mode": "high"}, T0)
    verdict = v.classify_device_change(reg, dev(temperature=22.0, fan="low"), dev(temperature=23.0, fan="high"), T0 + 1.0)
    assert verdict.manual_fields == ("t",) and verdict.expected_fields == ("f",) and verdict.provenance == v.PROVENANCE_UNKNOWN


def test_no_blind_window_a_command_on_one_field_does_not_excuse_another():
    reg = v.register_command(v.EMPTY_REGISTER, "set_hvac_mode", {"hvac_mode": "heat"}, T0)
    verdict = v.classify_device_change(reg, dev("off"), dev("heat", swing="vertical"), T0 + 1.0)
    assert verdict.manual_fields == ("s",) and verdict.expected_fields == ("m",)
    empty = v.classify_device_change(v.EMPTY_REGISTER, dev("heat"), dev("cool"), T0)
    assert empty.manual and empty.provenance == v.PROVENANCE_UNKNOWN


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (None, dev()),
        (dev("unknown"), dev("heat")),
        (dev("unavailable"), dev("off")),
        (dev("heat"), None),
        (dev("heat"), dev("unavailable")),
        (dev("heat"), dev("heat")),  # plain confirmation: nothing changed
    ],
)
def test_reconnections_missing_states_and_confirmations_are_not_manual(old, new):
    verdict = v.classify_device_change(v.EMPTY_REGISTER, old, new, T0)
    assert not verdict.manual and verdict.provenance == v.PROVENANCE_NONE and verdict.changed == ()


def test_measured_values_are_ignored_by_construction():
    """The model has no field for the measured room temperature: only commanded fields exist."""
    assert v.FIELDS == ("m", "t", "f", "p", "s")
    same = v.classify_device_change(v.EMPTY_REGISTER, dev(), dev(), T0)
    assert not same.manual


def test_the_author_of_a_device_event_is_never_promoted_to_user():
    reg = v.EMPTY_REGISTER
    for new in (dev("off"), dev(temperature=25.0), dev(fan="high"), dev(preset="boost"), dev(swing="vertical")):
        verdict = v.classify_device_change(reg, dev(), new, T0)
        assert verdict.manual and verdict.provenance == v.PROVENANCE_UNKNOWN != v.PROVENANCE_USER


# --- user commands and reconnection ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("user_id", "service", "targets", "expected"),
    [
        ("abc", "set_hvac_mode", SPLIT, True),
        ("abc", "set_temperature", [PDC], True),
        ("abc", "turn_off", (SPLIT, "climate.other"), True),
        (None, "set_hvac_mode", SPLIT, False),  # an automation: registered as expected, not manual
        ("abc", "set_hvac_mode", "climate.other", False),
        ("abc", "set_humidity", SPLIT, False),
        ("abc", "set_hvac_mode", None, False),
        ("abc", "set_hvac_mode", 5, False),
    ],
)
def test_authenticated_user_commands_win(user_id, service, targets, expected):
    assert v.is_user_command(user_id, service, targets, (SPLIT, PDC)) is expected


@pytest.mark.parametrize(
    ("old", "new", "override_off", "managed_off", "found"),
    [
        (None, "heat", True, True, True),
        ("unknown", "cool", True, True, True),
        ("unavailable", "auto", True, True, True),
        ("unavailable", "off", True, True, False),
        ("off", "heat", True, True, False),  # not a reconnection: handled as a device change
        ("unknown", "heat", False, True, False),  # override already on: preserved, not renewed
        ("unknown", "heat", None, True, False),
        ("unknown", "heat", True, False, False),  # managed by the automatic: not a manual start
        ("unknown", "heat", True, None, False),
        ("unknown", "unavailable", True, True, False),
    ],
)
def test_device_found_running_after_a_reconnection(old, new, override_off, managed_off, found):
    assert v.device_found_running(old, new, override_off=override_off, managed_off=managed_off) is found


# --- override lifetime ----------------------------------------------------------------------------


def test_override_lasts_120_minutes_and_is_renewed_by_each_manual_event():
    state = v.start_override(v.OverrideState(), T0, v.SOURCE_DEVICE_CHANGE, v.PROVENANCE_UNKNOWN)
    assert state.active and state.started_at == T0 and state.until == T0 + 7200.0 and state.provenance == v.PROVENANCE_UNKNOWN
    renewed = v.start_override(state, T0 + 3000.0, v.SOURCE_DEVICE_CHANGE, v.PROVENANCE_UNKNOWN)
    assert renewed.started_at == T0 and renewed.until == T0 + 10200.0
    assert not v.override_expired(renewed, T0 + 10199.9) and v.override_expired(renewed, T0 + 10200.0)


def test_unknown_author_stays_unknown_and_never_erases_a_known_user():
    user = v.start_override(v.OverrideState(), T0, v.SOURCE_USER_COMMAND, v.PROVENANCE_USER)
    again = v.start_override(user, T0 + 60.0, v.SOURCE_DEVICE_CHANGE, v.PROVENANCE_UNKNOWN)
    assert again.provenance == v.PROVENANCE_USER and again.source == v.SOURCE_DEVICE_CHANGE
    unknown = v.start_override(v.OverrideState(), T0, v.SOURCE_DEVICE_CHANGE, v.PROVENANCE_UNKNOWN)
    assert v.start_override(unknown, T0 + 60.0, v.SOURCE_DEVICE_CHANGE, v.PROVENANCE_UNKNOWN).provenance == v.PROVENANCE_UNKNOWN
    promoted = v.start_override(unknown, T0 + 60.0, v.SOURCE_USER_COMMAND, v.PROVENANCE_USER)
    assert promoted.provenance == v.PROVENANCE_USER


def test_expiry_resume_and_restore():
    state = v.start_override(v.OverrideState(), T0, v.SOURCE_USER_COMMAND, v.PROVENANCE_USER)
    assert not v.override_expired(v.OverrideState(), T0 + 99999.0)  # inactive never expires
    assert v.end_override(state, T0 + 10.0) == v.OverrideState()
    assert v.restore_override(state, T0 + 100.0) == state  # kept as is, not renewed
    assert v.restore_override(state, T0 + 7200.0) == v.OverrideState()  # expired while HA was down
    assert v.override_expired(dataclasses.replace(state, until=None), T0)  # missing end counts as reached


def test_validation():
    with pytest.raises(ValueError):
        v.ExpectedEffect("heat", float("nan"))
    with pytest.raises(TypeError):
        v.register_command(v.EMPTY_REGISTER, "turn_on", {}, True)
    with pytest.raises(ValueError):
        v.classify_device_change(v.EMPTY_REGISTER, dev(), dev("off"), float("inf"))
    with pytest.raises(ValueError):
        v.OverrideState(True, started_at=float("nan"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.OverrideState().active = True  # type: ignore[misc]


def test_module_is_pure():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "math", "dataclasses", "typing"}, imported
