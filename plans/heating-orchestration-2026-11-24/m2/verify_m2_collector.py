"""Offline parity check of the M2-A collector against the real ``raw`` template.

Renders the ``raw`` Jinja template of the baseline automation with plain Jinja2,
emulating the Home Assistant helpers it uses (``states``, ``states[...]``,
``is_state``, ``state_attr``, ``has_value``, ``as_timestamp``, ``now``), and
compares the rendered dictionary with ``collect_boiler_inputs(...).raw`` for:

* the 17 baseline observation fixtures (rendered and saved results);
* generated cases from ``collector_cases.py``.

No Home Assistant import. Requires Jinja2. From the repository root::

    python -I plans/heating-orchestration-2026-11-24/m2/verify_m2_collector.py [--cases N] [--seed S]

The output JSON is written next to this script (``verify_m2_collector-result.json``).
The real Home Assistant template engine is exercised separately by
``tests/test_heating_orchestration_parity.py``.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jinja2

HERE = Path(__file__).resolve().parent
PLAN_DIR = HERE.parent
ROOT = PLAN_DIR.parents[1]
COLLECTOR_PATH = ROOT / "custom_components" / "versatile_thermostat" / "heating_boiler_collector.py"
FIXTURE_NOW = 2000.0  # the fixtures do not record ``now``; any value in [1980, 5580] gives the saved results


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


collector = _load("_m2_collector_under_review", COLLECTOR_PATH)
cases_module = _load("_m2_collector_cases", HERE / "collector_cases.py")


class _TemplateState:
    def __init__(self, entity_id: str, data: dict[str, Any]) -> None:
        self.entity_id = entity_id
        self.state = data.get("state")
        self.attributes = data.get("attributes") or {}


class _States:
    """``states('x')`` and ``states['x']`` like the Home Assistant ``AllStates``."""

    def __init__(self, snapshot: dict[str, dict[str, Any]]) -> None:
        self._snapshot = snapshot

    def get(self, entity_id: str) -> _TemplateState | None:
        data = self._snapshot.get(entity_id)
        return None if data is None else _TemplateState(entity_id, data)

    def __call__(self, entity_id: str) -> str:
        state = self.get(entity_id)
        return "unknown" if state is None else state.state

    def __getitem__(self, entity_id: str) -> _TemplateState | None:
        return self.get(entity_id)


def render_raw(template: jinja2.Template, snapshot: dict[str, dict[str, Any]], now: float) -> dict[str, Any]:
    """Render the baseline ``raw`` with emulated Home Assistant helpers."""
    states = _States(snapshot)

    def is_state(entity_id: str, value: Any) -> bool:
        state = states.get(entity_id)
        if state is None:
            return False
        return state.state == value if isinstance(value, str) else state.state in value

    def state_attr(entity_id: str, name: str) -> Any:
        state = states.get(entity_id)
        return None if state is None else state.attributes.get(name)

    def has_value(entity_id: str) -> bool:
        state = states.get(entity_id)
        return state is not None and state.state not in ("unknown", "unavailable")

    def as_timestamp(value: Any, default: Any = None) -> Any:
        if isinstance(value, datetime):
            return value.timestamp()
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            if default is None:
                raise
            return default
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    rendered = template.render(
        states=states,
        is_state=is_state,
        state_attr=state_attr,
        has_value=has_value,
        as_timestamp=as_timestamp,
        now=lambda: datetime.fromtimestamp(now, timezone.utc),
    )
    return ast.literal_eval(rendered.strip())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=20261124)
    args = parser.parse_args()

    template = jinja2.Environment().from_string(cases_module.raw_template_source())
    config = collector.parse_orchestration_config(cases_module.BASELINE_SECTION)

    fixtures = json.loads(cases_module.OBSERVATION_FILE.read_text(encoding="utf-8"))
    saved = {entry["name"]: entry["result"] for entry in fixtures["results"]}
    fixture_report = []
    for fixture in fixtures["fixtures"]:
        jinja_raw = render_raw(template, fixture["snapshot"], FIXTURE_NOW)
        python_raw = collector.collect_boiler_inputs(fixture["snapshot"], config, FIXTURE_NOW).raw
        assert python_raw == jinja_raw, {"fixture": fixture["name"], "python": python_raw, "jinja": jinja_raw}
        expected = saved[fixture["name"]]
        assert {key: python_raw[key] for key in expected} == expected, fixture["name"]
        fixture_report.append({"name": fixture["name"], "count": python_raw["count"], "ok": python_raw["ok"]})

    stats = {"ok_true": 0, "count_ge_1": 0, "count_ge_2": 0, "rooms_ge_1": 0, "count_invalid": 0}
    for now, snapshot in cases_module.generate_cases(args.seed, args.cases):
        jinja_raw = render_raw(template, snapshot, now)
        collection = collector.collect_boiler_inputs(snapshot, config, now)
        assert collection.raw == jinja_raw, {"now": now, "snapshot": snapshot, "python": collection.raw, "jinja": jinja_raw}
        stats["ok_true"] += jinja_raw["ok"]
        stats["count_ge_1"] += jinja_raw["count"] >= 1
        stats["count_ge_2"] += jinja_raw["count"] >= 2
        stats["rooms_ge_1"] += len(jinja_raw["rooms"]) >= 1
        stats["count_invalid"] += not collection.count_raw_valid

    report = {
        "collector_sha256": hashlib.sha256(COLLECTOR_PATH.read_bytes()).hexdigest(),
        "template_sha256": hashlib.sha256(cases_module.raw_template_source().encode("utf-8")).hexdigest(),
        "jinja2": jinja2.__version__,
        "fixtures_matching_rendered_and_saved": len(fixture_report),
        "fixture_now": FIXTURE_NOW,
        "generated_cases_matching": args.cases,
        "seed": args.seed,
        "generated_coverage": stats,
    }
    (HERE / "verify_m2_collector-result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
