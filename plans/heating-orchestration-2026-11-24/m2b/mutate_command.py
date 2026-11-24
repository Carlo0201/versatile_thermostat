# Mutation check of heating_boiler_command.py (M2-B): python plans/heating-orchestration-2026-11-24/m2b/mutate_command.py
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "custom_components" / "versatile_thermostat" / "heating_boiler_command.py"
PY = sys.executable
original = SRC.read_text(encoding="utf8")

MUTANTS = [
    ("timeout 60->61", "CONFIRMATION_TIMEOUT_S: Final = 60.0", "CONFIRMATION_TIMEOUT_S: Final = 61.0"),
    ("timeout 60->59", "CONFIRMATION_TIMEOUT_S: Final = 60.0", "CONFIRMATION_TIMEOUT_S: Final = 59.0"),
    ("backoff 300->120", "RETRY_DELAYS_S: Final = (60.0, 120.0, 300.0)", "RETRY_DELAYS_S: Final = (60.0, 120.0, 120.0)"),
    ("backoff 60->30", "RETRY_DELAYS_S: Final = (60.0, 120.0, 300.0)", "RETRY_DELAYS_S: Final = (30.0, 120.0, 300.0)"),
    ("backoff 4 delays", "RETRY_DELAYS_S: Final = (60.0, 120.0, 300.0)", "RETRY_DELAYS_S: Final = (60.0, 120.0, 300.0, 600.0)"),
    ("max sends = retries", "MAX_SENDS: Final = 1 + len(RETRY_DELAYS_S)", "MAX_SENDS: Final = len(RETRY_DELAYS_S)"),
    ("service timeout 30->35", "SERVICE_TIMEOUT_S: Final = 30.0", "SERVICE_TIMEOUT_S: Final = 35.0"),
    ("heat persist 5->10", "HEAT_PERSIST_WAIT_S: Final = 5.0", "HEAT_PERSIST_WAIT_S: Final = 10.0"),
    ("settle 30->29", "SETTLE_MIN_INTERVAL_S: Final = 30.0", "SETTLE_MIN_INTERVAL_S: Final = 29.0"),
    ("settle 30->31", "SETTLE_MIN_INTERVAL_S: Final = 30.0", "SETTLE_MIN_INTERVAL_S: Final = 31.0"),
    ("settle >= -> >", "if now - state.anchor_at >= SETTLE_MIN_INTERVAL_S:", "if now - state.anchor_at > SETTLE_MIN_INTERVAL_S:"),
    ("settle invalid does not reset", "    if not valid:\n        return SettlingState()\n", "    if not valid:\n        return state\n"),
    ("settle ignores action change", "state.anchor_at is None or state.anchor_action != action or now < state.anchor_at", "state.anchor_at is None or now < state.anchor_at"),
    ("settle ignores clock back", "state.anchor_at is None or state.anchor_action != action or now < state.anchor_at", "state.anchor_at is None or state.anchor_action != action"),
    ("settled not latched", "    if state.settled:\n        return state\n    if not valid:", "    if not valid:"),
    ("is_off lenient", 'return self.state == "off"', 'return self.state in ("off", None)'),
    ("is_off unknown ok", 'return self.state == "off"', 'return self.state not in ("on",)'),
    ("owner: master check removed", "    if master_mode not in MASTER_KNOWN_MODES:\n        return Ownership(False, BLOCK_MASTER_UNKNOWN, owners)\n", ""),
    ("owner: empty owners ok", "    if not owners:\n        return Ownership(False, BLOCK_NO_EXTERNAL_OWNER, owners)\n", ""),
    ("owner: any->all", "if any(not owner.is_off for owner in owners):", "if all(not owner.is_off for owner in owners):"),
    ("owner: evaluation_valid ignored", "    if not evaluation_valid:\n        return Ownership(False, BLOCK_NO_VALID_EVALUATION, owners)\n", ""),
    ("owner: active ignored", "    if not active:\n        return Ownership(False, BLOCK_NOT_ACTIVE, owners)\n", ""),
    (
        "advance: opposite ignored",
        '    if action in COMMAND_MODES and action != record.mode:\n        # The opposite action replaces the request immediately.\n        return _superseded(record, f"action:{action}")\n',
        "",
    ),
    ("advance: confirm inverted", "    if inputs.master_mode == record.mode:\n        confirmed", "    if inputs.master_mode != record.mode:\n        confirmed"),
    (
        "advance: action none not superseding",
        '    if action != record.mode:\n        return _superseded(record, f"action:{action}")\n    if record.status == STATUS_SENDING',
        "    if record.status == STATUS_SENDING",
    ),
    (
        "advance: timeout >= -> >",
        "now >= (record.service_completed_at if record.service_completed_at is not None else record.at) + CONFIRMATION_TIMEOUT_S:",
        "now > (record.service_completed_at if record.service_completed_at is not None else record.at) + CONFIRMATION_TIMEOUT_S:",
    ),
    (
        "advance: timeout from send start",
        "now >= (record.service_completed_at if record.service_completed_at is not None else record.at) + CONFIRMATION_TIMEOUT_S:",
        "now >= record.at + CONFIRMATION_TIMEOUT_S:",
    ),
    ("advance: stale send >= -> >", "now >= record.at + SEND_STALE_AFTER_S:", "now > record.at + SEND_STALE_AFTER_S:"),
    (
        "fail: exhausted off by one",
        "    if record.attempts >= MAX_SENDS:\n        failed = replace(failed, retry_at=None)",
        "    if record.attempts > MAX_SENDS:\n        failed = replace(failed, retry_at=None)",
    ),
    ("fail: delay index", "return RETRY_DELAYS_S[attempts - 1]", "return RETRY_DELAYS_S[attempts - 2]"),
    ("is_exhausted off by one", "and self.attempts >= MAX_SENDS", "and self.attempts > MAX_SENDS"),
    ("step: retry due > instead of >=", "if inputs.now >= current.retry_at:", "if inputs.now > current.retry_at:"),
    ("step: retry attempt not incremented", "send, attempt = current.mode, current.attempts + 1", "send, attempt = current.mode, current.attempts"),
    (
        "step: master != action ignored",
        "and inputs.master_mode in MASTER_KNOWN_MODES and inputs.master_mode != inputs.action:\n            send, attempt = inputs.action, 1",
        "and inputs.master_mode in MASTER_KNOWN_MODES:\n            send, attempt = inputs.action, 1",
    ),
    (
        "step: known master ignored",
        "inputs.master_mode in MASTER_KNOWN_MODES and inputs.master_mode != inputs.action:\n            send, attempt = inputs.action, 1",
        "inputs.master_mode != inputs.action:\n            send, attempt = inputs.action, 1",
    ),
    ("step: settling gate removed", "    if send is not None and not inputs.settled:\n        send, attempt = None, 0\n", ""),
    ("step: exhausted does not block", "    elif current is None or not (current.is_active or current.is_exhausted):\n", "    elif current is None or not current.is_active:\n"),
    ("step: ownership gate or->and", "if not ownership.is_owner or inputs.shutting_down:", "if not ownership.is_owner and inputs.shutting_down:"),
    ("step: shutdown ignored", "if not ownership.is_owner or inputs.shutting_down:", "if not ownership.is_owner:"),
    ("step: cancel does not cancel", "            current = replace(current, status=STATUS_CANCELLED, retry_at=None)\n", "            pass\n"),
    (
        "step: cancel keeps retry",
        "            current = replace(current, status=STATUS_CANCELLED, retry_at=None)\n",
        "            current = replace(current, status=STATUS_CANCELLED)\n",
    ),
    (
        "step: control status exhausted lost",
        "    if current is not None and current.is_exhausted:\n        status = CONTROL_COMMAND_FAILED",
        "    if False:\n        status = CONTROL_COMMAND_FAILED",
    ),
    ("step: confirmed status needs master", "current.status == STATUS_CONFIRMED and inputs.master_mode == current.mode:", "current.status == STATUS_CONFIRMED:"),
    ("step: wake kept on send", "    wake = None if send is not None else _wake_at(current)", "    wake = _wake_at(current)"),
    (
        "wake: pending uses start",
        "return (record.service_completed_at if record.service_completed_at is not None else record.at) + CONFIRMATION_TIMEOUT_S\n    if record.status == STATUS_FAILED",
        "return record.at + CONFIRMATION_TIMEOUT_S\n    if record.status == STATUS_FAILED",
    ),
    ("wake: retry ignored", "    if record.status == STATUS_FAILED:\n        return record.retry_at\n", "    if record.status == STATUS_FAILED:\n        return None\n"),
    (
        "completed: id check removed",
        "    if record is None or record.send_id != send_id:\n        return record\n    if record.status == STATUS_SENDING:",
        "    if record is None:\n        return record\n    if record.status == STATUS_SENDING:",
    ),
    (
        "completed: becomes confirmed",
        "return replace(record, status=STATUS_PENDING, service_completed_at=now, error=None)",
        "return replace(record, status=STATUS_CONFIRMED, service_completed_at=now, error=None)",
    ),
    ("failed: id check removed", "if record is None or record.send_id != send_id or record.status != STATUS_SENDING:", "if record is None or record.status != STATUS_SENDING:"),
    ("failed: status check removed", "if record is None or record.send_id != send_id or record.status != STATUS_SENDING:", "if record is None or record.send_id != send_id:"),
    (
        "restart: pending kept",
        "if record is None or record.status not in (STATUS_SENDING, STATUS_PENDING, STATUS_FAILED):",
        "if record is None or record.status not in (STATUS_SENDING, STATUS_FAILED):",
    ),
    (
        "restart: failed kept",
        "if record is None or record.status not in (STATUS_SENDING, STATUS_PENDING, STATUS_FAILED):",
        "if record is None or record.status not in (STATUS_SENDING, STATUS_PENDING):",
    ),
    ("restart: confirmed", "restored = replace(record, status=STATUS_UNCONFIRMED, retry_at=None)", "restored = replace(record, status=STATUS_CONFIRMED, retry_at=None)"),
    (
        "start: previous unresolved allowed",
        '        if previous is not None and (previous.is_active or previous.is_exhausted):\n            raise ValueError("a new request cannot replace an unresolved one")\n',
        "",
    ),
    ("start: confirmation not carried", "    last_confirmation = None if previous is None else previous.last_confirmation_at", "    last_confirmation = None"),
    ("start: base master lost on retry", "        base = previous.base_master", "        base = base_master"),
    ("start: retry mode not checked", "previous is None or previous.mode != mode or previous.status", "previous is None or previous.status"),
    ("confirm: last_confirmation not set", "retry_at=None, last_confirmation_at=now, error=None)", "retry_at=None, error=None)"),
    ("diag: last_command_at None", '"last_command_at": None if record is None or record.attempts == 0 else record.at,', '"last_command_at": None,'),
    ("diag: attempts", '"command_attempts": 0 if record is None else record.attempts,', '"command_attempts": 0,'),
    ("diag: owners", '"external_owners": [{"entity_id": owner.entity_id, "state": owner.state} for owner in ownership.external_owners],', '"external_owners": [],'),
    (
        "store: retry persisted",
        '        "base_master": record.base_master,\n        "error": record.error,\n    }',
        '        "base_master": record.base_master,\n        "error": record.error,\n        "retry_at": record.retry_at,\n    }',
    ),
    ("store: v1 setdefault removed", "        migrated.setdefault(STORE_COMMAND_KEY, None)\n", ""),
    (
        "store: future version accepted",
        '    raise NotImplementedError(f"unsupported store version {old_major_version}")',
        "    return dict(data) if isinstance(data, Mapping) else {}",
    ),
    ("store: send_id garbage", "send_id=send_id if isinstance(send_id, int) and not isinstance(send_id, bool) and send_id >= 0 else 0,", "send_id=send_id,"),
    ("sent event mode", "return record, CommandEvent(EVENT_SENT, mode, attempt)", "return record, CommandEvent(EVENT_SENT, mode, attempt + 1)"),
    ("opposite: heat does not supersede off", "if action in COMMAND_MODES and action != record.mode:", "if action == COMMAND_OFF and action != record.mode:"),
]

results = []
env_base = dict(os.environ)
with tempfile.TemporaryDirectory() as tmp:
    for name, old, new in MUTANTS:
        if original.count(old) != 1:
            results.append((name, "NOT-APPLIED(%d)" % original.count(old)))
            continue
        path = Path(tmp) / "mutant.py"
        path.write_text(original.replace(old, new), encoding="utf8")
        env = dict(env_base, VTHERM_COMMAND_PATH=str(path))
        proc = subprocess.run(
            [PY, "-m", "pytest", "-q", "-x", "--noconftest", "--import-mode=importlib", "-p", "no:cacheprovider", "tests/test_heating_boiler_command.py"],
            cwd=str(REPO),
            env=env,
            capture_output=True,
            text=True,
        )
        results.append((name, "killed" if proc.returncode != 0 else "SURVIVED"))

killed = sum(1 for _, r in results if r == "killed")
applied = sum(1 for _, r in results if not r.startswith("NOT-APPLIED"))
for name, r in results:
    if r != "killed":
        print(r, name)
print(f"mutants applied: {applied}, killed: {killed}, survived: {applied - killed}, not applied: {len(results) - applied}")
