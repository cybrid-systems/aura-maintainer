"""Versioned audit log for aura-maintainer.

Schema aura-maintainer.audit.v1. The commit point is one fsync'd JSONL line.
Snapshot ids are process-local and are not a resume pointer.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

AUDIT_SCHEMA = "aura-maintainer.audit.v1"
CHAMPION_SCHEMA = "aura-maintainer.champion.v1"
REQUEST_SCHEMA = "aura-maintainer.request.v1"

STREAK_LIMIT = 3


def dumps_line(record: dict) -> str:
    return json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"


def atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(dumps_line(record))
        fh.flush()
        os.fsync(fh.fileno())


def load_audit(path: Path) -> list[dict]:
    """Load JSONL. A corrupt trailing line is truncated and dropped."""
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    if raw == "":
        return []
    lines = raw.splitlines()
    while lines and lines[-1].strip() == "":
        lines.pop()
    if lines:
        try:
            json.loads(lines[-1])
        except json.JSONDecodeError:
            lines = lines[:-1]
            body = ("\n".join(lines) + "\n") if lines else ""
            atomic_write(path, body)
    records: list[dict] = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def clears_streak(record: dict) -> bool:
    decision = record.get("decision")
    reason = record.get("reason") or ""
    if decision == "PROBE_FAIL":
        return True
    if decision == "ROLLBACK" and isinstance(reason, str) and reason.startswith("proposal:"):
        return True
    return False


def next_streak(streak: int, record: dict) -> int:
    """PATH_BROKEN increments. PROBE_FAIL and proposal: ROLLBACK reset.

    IDLE, KEEP, and PROBE_OK leave the streak unchanged. An isolated
    PATH_BROKEN does not stop the run; three consecutive ones do.
    """
    if record.get("decision") == "PATH_BROKEN":
        return streak + 1
    if clears_streak(record):
        return 0
    return streak


def exit_code_for(records: list[dict], *, stopped_early: bool) -> int:
    """0 when a finished audit has no PATH_BROKEN. 3 if any remain or we stopped early."""
    if stopped_early:
        return 3
    if any(rec.get("decision") == "PATH_BROKEN" for rec in records):
        return 3
    return 0


def synthetic_path_broken(
    *,
    run_id: str,
    cycle_id: int,
    reason: str,
    proposal_id: str | None = None,
    duration_ms: int = 0,
) -> dict:
    return {
        "schema": AUDIT_SCHEMA,
        "run_id": run_id,
        "cycle_id": cycle_id,
        "decision": "PATH_BROKEN",
        "reason": reason,
        "outcome_class": "path-broken",
        "proposal_id": proposal_id,
        "snapshot_id": None,
        "author": "aura-maintainer",
        "duration_ms": duration_ms,
        "warnings": [],
    }


def extract_cycle_json(text: str) -> dict | None:
    """Return the single CYCLE_JSON object, or None if the contract is broken."""
    lines = [ln for ln in text.splitlines() if ln.startswith("CYCLE_JSON ")]
    if len(lines) != 1:
        return None
    payload = lines[0][len("CYCLE_JSON ") :]
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("schema") != AUDIT_SCHEMA:
        return None
    return obj
