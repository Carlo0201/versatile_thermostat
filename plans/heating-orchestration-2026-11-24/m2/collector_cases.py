"""Deterministic generator of collector snapshots for the parity checks (M2-A).

Pure module, no Home Assistant import. Used by ``verify_m2_collector.py``
(offline Jinja2 emulation) and by ``tests/test_heating_orchestration_parity.py``
(real Home Assistant template engine).

``BASELINE_SECTION`` is the configuration that reproduces the hard-coded slugs
and entity ids of the baseline ``raw`` template. It is test data: these ids
never appear in the runtime code.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PLAN_DIR = Path(__file__).resolve().parents[1]
BASELINE_FILE = PLAN_DIR / "baseline" / "caldaia_controllo_stabile_delle_richieste.json"
OBSERVATION_FILE = PLAN_DIR / "baseline" / "boiler-observation-cases.json"

SLUGS = ["camera_carlo", "ingresso", "cucina", "soggiorno", "camera_laura_livia", "bagno_verde", "bagno_blu", "camera_diego_luisa"]
PILOT_SLUGS = ("camera_carlo", "bagno_blu")
PDC = {
    "camera_carlo": ("climate.camera_carlo_condizionatore", "input_boolean.camera_carlo_pdc_in_gestione"),
    "camera_diego_luisa": ("climate.camera_diego_luisa_condizionatore_aria_stanza", "input_boolean.camera_diego_luisa_pdc_in_gestione"),
}
COUNT = "sensor.nb_device_active_for_boiler"
CENTRAL = "select.central_mode"
MASTER = "climate.termostato_ingresso"
MANUAL = "binary_sensor.caldaia_richiesta_manuale_oikos"
PILOT = "input_boolean.caldaia_filtro_pilota_avatto"
PILOT_READY = "binary_sensor.caldaia_avvio_automatico_avatto"
CONSENTS = ["input_boolean.richiesta_caldaia_vtherm", "binary_sensor.central_boiler"]
LEGACY = "input_text.caldaia_controllo_ciclo"


def baseline_rooms() -> list[dict[str, Any]]:
    """Room bindings equivalent to the template slugs, in the template order."""
    rooms = []
    for slug in SLUGS:
        room: dict[str, Any] = {"room_key": slug, "climate_entity_id": f"climate.trv_vtherm_{slug}", "pilot_filter": slug in PILOT_SLUGS}
        if slug in PDC:
            room["pdc"] = {"split_entity_id": PDC[slug][0], "session_entity_id": PDC[slug][1]}
        rooms.append(room)
    return rooms


BASELINE_SECTION: dict[str, Any] = {
    "mode": "observe",
    "master_entity_id": MASTER,
    "central_mode_entity_id": CENTRAL,
    "active_count_entity_id": COUNT,
    "manual_request_entity_id": MANUAL,
    "pilot_filter_entity_id": PILOT,
    "pilot_ready_entity_id": PILOT_READY,
    "consent_entity_ids": list(CONSENTS),
    "legacy_cycle_entity_id": LEGACY,
    "rooms": baseline_rooms(),
}


def raw_template_source() -> str:
    """The first ``raw`` variable of the baseline automation (normative source)."""
    config = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))["config"]
    first = config["actions"][0]["variables"]["raw"]
    second = config["actions"][3]["variables"]["raw"]
    assert first == second, "the two raw collectors of the baseline differ"
    return first


def valves_of(slug: str) -> list[str]:
    """Physical valves of a generated room: the living room has two."""
    if slug == "soggiorno":
        return ["climate.trv_soggiorno_1", "climate.trv_soggiorno_2"]
    return [f"climate.trv_{slug}"]


def _iso(ts: float, rng: random.Random) -> str:
    offset = rng.choice([0, 1, 2])
    tz = timezone(timedelta(hours=offset))
    text = datetime.fromtimestamp(ts, tz).isoformat()
    return text.replace("+00:00", "Z") if offset == 0 and rng.random() < 0.5 else text


def generate_case(rng: random.Random, now: float) -> dict[str, dict[str, Any]]:
    """One random but plausible snapshot ``{entity_id: {state, attributes}}``.

    Only timezone-aware timestamps are produced: naive timestamps are read in
    the process local time by Home Assistant and are covered by unit tests.
    Temperatures are finite (non finite values are a documented hardening).
    """
    snapshot: dict[str, dict[str, Any]] = {}
    # Nominal cases keep most gates valid so that eligible rooms and the
    # normal/single paths are well covered; the others are fully random.
    nominal = rng.random() < 0.7

    def pick(good: Any, choices: list[Any]) -> Any:
        return good if nominal and rng.random() < 0.92 else rng.choice(choices)

    def put(entity_id: str, state: str, attributes: dict[str, Any] | None = None) -> None:
        snapshot[entity_id] = {"state": state, "attributes": attributes or {}}

    def maybe(entity_id: str, choices: list[str], missing: float = 0.1) -> None:
        if rng.random() >= missing:
            put(entity_id, rng.choice(choices))

    room_valves: dict[str, list[str]] = {slug: valves_of(slug) for slug in SLUGS}
    if rng.random() < 0.1:
        # Inconsistent binding: one valve listed by two VTherms.
        room_valves["cucina"] = room_valves["cucina"] + ["climate.trv_ingresso"]
    all_valves = sorted({valve for valves in room_valves.values() for valve in valves})

    for valve in all_valves:
        if rng.random() < 0.05:
            continue
        put(valve, pick("heat", ["heat", "heat", "auto", "off", "unavailable"]), {"hvac_action": pick("heating", ["heating", "heating", "idle", None])})

    active = [valve for valve in all_valves if rng.random() < (0.85 if nominal else 0.6)]
    ids: Any = list(active)
    if active and rng.random() < 0.15:
        ids.append(rng.choice(active))  # duplicate id
    if active and rng.random() < 0.1:
        ids.append(rng.choice(active).upper())  # case variant: unique() ignores case
    count_choice = rng.random()
    if count_choice < (0.9 if nominal else 0.7):
        count_state = str(len({str(item).lower() for item in ids}))
    elif count_choice < 0.8:
        count_state = str(len(ids))
    elif count_choice < 0.85:
        count_state = rng.choice(["unknown", "unavailable", "-1", "1.5", "abc"])
    else:
        count_state = f"{len(set(ids))}.0"
    id_shape = rng.random()
    attributes: dict[str, Any]
    if nominal or id_shape >= 0.1:
        attributes = {"active_device_ids": ids}
    elif id_shape < 0.05:
        attributes = {}
    elif id_shape < 0.08:
        attributes = {"active_device_ids": ",".join(ids)}
    else:
        attributes = {"active_device_ids": {"a": 1}}
    if rng.random() > 0.03:
        put(COUNT, count_state, attributes)

    maybe(CENTRAL, ["Auto", "Heat only", "Heat only", "Heat only", "Stopped", "Cool only", "unknown"], 0.02 if nominal else 0.1)
    maybe(MASTER, ["off", "off", "heat", "unavailable", "unknown"])
    maybe(MANUAL, ["off", "off", "on", "unknown"])
    maybe(PILOT, ["off", "off", "on", "unavailable"])
    maybe(PILOT_READY, ["on", "off", "unknown"])
    for consent in CONSENTS:
        maybe(consent, ["on", "on", "off", "unavailable"])
    for split, session in PDC.values():
        maybe(split, ["off", "off", "off", "heat", "unavailable", "unknown"], 0.02 if nominal else 0.1)
        maybe(session, ["off", "off", "off", "on", "unavailable"], 0.02 if nominal else 0.1)

    for slug in SLUGS:
        entity_id = f"climate.trv_vtherm_{slug}"
        if rng.random() < 0.04:
            continue
        sensor = f"binary_sensor.window_{slug}"
        if rng.random() > 0.05:
            put(sensor, pick("off", ["off", "off", "off", "on", "unavailable"]))
        window: dict[str, Any] = {"window_state": pick("off", ["off", "off", "off", "on", None])}
        if rng.random() > 0.05:
            window["window_sensor_entity_id"] = sensor
        bypass = rng.random()
        if bypass < 0.1:
            window["is_window_bypass"] = True
        elif bypass < 0.13:
            window["is_window_bypass"] = 1
        elif bypass < 0.3:
            window["is_window_bypass"] = False
        specific: dict[str, Any] = {"is_initialized": pick(True, [True, True, True, False, None, 1])}
        stamp = rng.random()
        if stamp < 0.85:
            age = pick(30, [-1, -0.5, 0, 1, 30, 600, 3599, 3600, 3600.5, 3601, 7200])
            specific["last_temperature_datetime"] = _iso(now - age, rng)
        elif stamp < 0.92:
            specific["last_temperature_datetime"] = rng.choice(["garbage", "", None])
        inhibition = rng.random()
        if inhibition < (0.02 if nominal else 0.1):
            specific["inhibition"] = {"inhibited": rng.choice([True, None, 0, 1])}
        elif inhibition < 0.3:
            specific["inhibition"] = {"inhibited": False}
        elif inhibition < 0.35:
            specific["inhibition"] = {}
        over: dict[str, Any] = {"underlying_entities": list(room_valves[slug])}
        demand = rng.random()
        if demand < 0.5:
            over["avatto_room_demand"] = True
        elif demand < 0.65:
            over["avatto_room_demand"] = False
        elif demand < 0.7:
            over["avatto_room_demand"] = rng.choice([0, 1, None, "true"])
        attrs: dict[str, Any] = {
            "is_ready": pick(True, [True, True, True, False, None, 1]),
            "specific_states": specific,
            "configuration": {"is_used_by_central_boiler": pick(True, [True, True, True, False, None])},
            "vtherm_over_climate": over,
            "window_manager": window,
        }
        if rng.random() > 0.1:
            attrs["safety_manager"] = {"safety_state": pick("off", ["off", "off", "off", "on", "unavailable"])}
        if rng.random() > 0.1:
            attrs["power_manager"] = {"overpowering_state": pick("off", ["off", "off", "off", "on"])}
        target_value = pick(rng.choice([18, 19.5, 20]), [18, 19.5, 20, "20.5", None, True])
        current_value = pick(rng.choice([17, 17.2, 18.75, 19.7, 20.2]), [17, 17.2, 18.75, 19.7, 20.2, "17.1", None])
        if target_value is not None:
            attrs["temperature"] = target_value
        if current_value is not None:
            attrs["current_temperature"] = current_value
        put(entity_id, pick(rng.choice(["heat", "auto"]), ["heat", "heat", "heat", "auto", "off", "unavailable", "unknown"]), attrs)
    return snapshot


def generate_cases(seed: int, number: int) -> list[tuple[float, dict[str, dict[str, Any]]]]:
    """``number`` deterministic ``(now, snapshot)`` pairs."""
    rng = random.Random(seed)
    cases = []
    for _ in range(number):
        now = rng.choice([1795508400.0, 1795508400.25, 1800000000.0])
        cases.append((now, generate_case(rng, now)))
    return cases
