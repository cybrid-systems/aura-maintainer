#!/usr/bin/env python3
"""Outer clock for aura-maintainer.

Python owns the cycle loop, the timeout, and the audit commit.
Each Aura process runs one cycle and exits. Champion bytes change only
after a fsync'd KEEP whose fixture score rose.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.audit import (
    AUDIT_SCHEMA,
    CHAMPION_SCHEMA,
    REQUEST_SCHEMA,
    STREAK_LIMIT,
    append_jsonl,
    atomic_write,
    exit_code_for,
    extract_cycle_json,
    load_audit,
    next_streak,
    synthetic_path_broken,
)
from harness.catalog import (
    Catalog,
    Champion,
    call_heads_allowed,
    grammar_ok,
    load_catalog,
    mark_tried,
    seed_body,
    seed_params,
)
from harness.metrics import champion_curve
from harness.proposers.rules import RulesProposer

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "agent" / "maintainer.aura"
DEFAULT_REPORTS = ROOT / "reports"
TARGET = ROOT / "target" / "aura-redis"
FIXTURES_PATH = ROOT / "agent" / "fixtures" / "windows.json"

REBIND_BROKEN = {
    "restore-eval-failed",
    "restore-mismatch",
    "proposal-eval",
    "locate-miss-after-mutate",
    "provenance-missing",
}
EPOCH_SILENT_LIMIT = 5
IDLE_SLICE_SEC = 60.0


class Heartbeat:
    def __init__(self) -> None:
        self.writes = 0

    def write(self, run_dir: Path, phase: str, cycle_id: int, pid: int | None) -> None:
        self.writes += 1
        payload = {
            "phase": phase,
            "cycle_id": cycle_id,
            "pid": pid,
            "writes": self.writes,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        atomic_write(run_dir / "heartbeat.json", json.dumps(payload, sort_keys=True) + "\n")


def find_aura_bin() -> str | None:
    candidates = [
        os.environ.get("AURA_BIN"),
        str(ROOT / ".deps" / "aura" / "build" / "aura"),
        str(ROOT / "target" / "aura-redis" / ".deps" / "aura" / "build" / "aura"),
        "aura",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def load_fixtures() -> list:
    payload = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ValueError("fixtures rows missing")
    return rows


def as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def champion_meta(mode: str, body_sha: str, generation: int = 0) -> dict:
    return {
        "schema": CHAMPION_SCHEMA,
        "family": "template",
        "profile": "template",
        "params": seed_params(mode),
        "body_sha256": body_sha,
        "generation": generation,
    }


def ensure_seed(run_dir: Path, mode: str, catalog: Catalog) -> dict:
    meta_path = run_dir / "champion.meta.json"
    existing = read_json(meta_path)
    if existing is not None:
        return existing
    body = seed_body(catalog, mode)
    digest = sha256_bytes(body.encode("utf-8"))
    (run_dir / "bodies").mkdir(parents=True, exist_ok=True)
    atomic_write(run_dir / "bodies" / f"{digest}.txt", body)
    atomic_write(run_dir / "champion.body", body)
    meta = champion_meta(mode, digest, 0)
    atomic_write(meta_path, json.dumps(meta, indent=2, sort_keys=True) + "\n")
    return meta


def reconcile_resume(run_dir: Path) -> str | None:
    """Align champion to the last KEEP line. Return a PATH_BROKEN reason to stop, or None."""
    meta_path = run_dir / "champion.meta.json"
    meta = read_json(meta_path)
    if meta is None or meta.get("schema") != CHAMPION_SCHEMA:
        return "resume-bad-schema"
    records = load_audit(run_dir / "audit.jsonl")
    if not records:
        return None
    last = records[-1]
    if last.get("decision") != "KEEP":
        return None
    if last.get("keep_eligible") is False:
        return "kept-ineligible"
    after = last.get("after")
    if not isinstance(after, dict):
        return "resume-missing-body"
    digest = after.get("body_sha256")
    if not isinstance(digest, str) or digest == "":
        return "resume-missing-body"
    if digest == meta.get("body_sha256"):
        return None
    body_path = run_dir / "bodies" / f"{digest}.txt"
    if not body_path.is_file():
        return "resume-missing-body"
    body = body_path.read_text(encoding="utf-8")
    generation = last.get("champion_generation")
    if not isinstance(generation, int):
        prev = meta.get("generation")
        generation = (prev + 1) if isinstance(prev, int) else 1
    rewritten = {
        "schema": CHAMPION_SCHEMA,
        "family": after.get("family", meta.get("family")),
        "profile": after.get("profile", meta.get("profile")),
        "params": after.get("params", meta.get("params")),
        "body_sha256": digest,
        "generation": generation,
    }
    atomic_write(run_dir / "champion.body", body)
    atomic_write(meta_path, json.dumps(rewritten, indent=2, sort_keys=True) + "\n")
    return None


def allowed_untracked(path: str) -> bool:
    name = Path(path).name
    if name.startswith(".ar-agent-booted-") and name.endswith(".flag"):
        return True
    if name.startswith(".ar-policy-") or path.startswith(".ar-policy-"):
        return True
    if path.startswith("native/build/"):
        return True
    return False


def tree_dirty() -> bool:
    """True when a tracked file under target/aura-redis is modified or deleted."""
    if not (TARGET / ".git").exists() and not (ROOT / ".gitmodules").exists():
        return False
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--", "."],
            cwd=str(TARGET),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    if proc.returncode != 0:
        return False
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        status, path = line[:2], line[3:]
        if status == "??":
            continue
        if "M" in status or "D" in status:
            return True
    return False


def aura_lib_path(aura: str) -> str | None:
    lib = Path(aura).resolve().parent.parent / "lib"
    if (lib / "std" / "mutate.aura").is_file():
        return str(lib)
    return None


def champion_from(meta: dict, body: str) -> Champion:
    params = meta.get("params") if isinstance(meta.get("params"), dict) else {}
    return Champion(
        family=str(meta.get("family") or "template"),
        profile=str(meta.get("profile") or "template"),
        params={str(k): int(v) for k, v in params.items()},
        body_sha256=str(meta.get("body_sha256") or ""),
        generation=int(meta.get("generation") or 0),
        body=body,
    )


def issue_for(proposal: dict | None, catalog: Catalog) -> dict | None:
    if not proposal:
        return None
    kind = proposal.get("kind")
    profile = proposal.get("profile")
    if kind in ("profile", "probe") and isinstance(profile, str) and profile:
        path = f"target/aura-redis/src/redis/policy/choose_{profile}.aura"
        line = catalog.lines.get(profile)
    else:
        path = "target/aura-redis/src/redis/policy/choose_normal.aura"
        line = catalog.lines.get("normal")
    return {
        "id": proposal.get("id"),
        "kind": kind,
        "path": path,
        "line": line,
        "summary": proposal.get("id"),
    }


def proposal_for_agent(proposal: dict | None) -> dict | None:
    if proposal is None:
        return None
    sent = dict(proposal)
    sent.pop("health_key", None)
    return sent


def load_tried(run_dir: Path) -> set[str]:
    obj = read_json(run_dir / "tried.json") or {}
    keys = obj.get("keys") if isinstance(obj, dict) else None
    if not isinstance(keys, list):
        return set()
    return {key for key in keys if isinstance(key, str)}


def save_tried(run_dir: Path, tried: set[str]) -> None:
    atomic_write(
        run_dir / "tried.json",
        json.dumps({"keys": sorted(tried)}, indent=2) + "\n",
    )


def meta_view(meta: dict, digest: str | None = None) -> dict:
    params = meta.get("params") if isinstance(meta.get("params"), dict) else {}
    return {
        "family": meta.get("family"),
        "profile": meta.get("profile"),
        "params": dict(params),
        "body_sha256": digest if digest is not None else meta.get("body_sha256"),
    }


def score_pair(record: dict) -> tuple[int | None, int | None]:
    tests = record.get("tests")
    if not isinstance(tests, list) or not tests or not isinstance(tests[0], dict):
        return None, None
    return as_int(tests[0].get("baseline_score")), as_int(tests[0].get("trial_score"))


def real_body_change(record: dict) -> bool:
    if not record.get("trial_body_changed"):
        return False
    reason = str(record.get("reason") or "")
    if reason in ("proposal:dry-run", "proposal:grammar", "proposal:call-head", "broken-rejected"):
        return False
    decision = record.get("decision")
    if decision in ("KEEP", "PROBE_OK", "PROBE_FAIL", "ROLLBACK"):
        return True
    if decision == "PATH_BROKEN" and reason in REBIND_BROKEN:
        return True
    return False


def silent_epoch_streak(records: list[dict]) -> int:
    streak = 0
    for rec in records:
        if not real_body_change(rec):
            continue
        if rec.get("compile_epoch_delta") == 0:
            streak += 1
        else:
            streak = 0
    return streak


def build_request(
    run_dir: Path,
    run_id: str,
    cycle_id: int,
    mode: str,
    meta: dict,
    proposal: dict | None,
    catalog: Catalog,
    fixtures: list,
    score_only: bool = False,
) -> Path:
    body = (run_dir / "champion.body").read_text(encoding="utf-8")
    broken = catalog.bodies["broken"]
    request = {
        "schema": REQUEST_SCHEMA,
        "run_id": run_id,
        "cycle_id": cycle_id,
        "mode": mode,
        "issue": issue_for(proposal, catalog),
        "proposal": proposal_for_agent(proposal),
        "champion_body": body,
        "champion_meta": meta,
        "controls": {
            "broken_body": broken,
            "broken_sha256": sha256_bytes(broken.encode("utf-8")),
        },
        "fixtures": fixtures,
    }
    if score_only:
        request["score_only"] = True
    path = run_dir / "request.json"
    atomic_write(path, json.dumps(request, indent=2, sort_keys=True) + "\n")
    return path


def spawn_cycle(
    aura: str,
    run_dir: Path,
    run_id: str,
    cycle_id: int,
    mode: str,
    meta: dict,
    timeout_sec: float,
    proposal: dict | None,
    dry_run: bool,
    catalog: Catalog,
    fixtures: list,
    beats: Heartbeat | None = None,
    crash_retries: int = 1,
    score_only: bool = False,
) -> tuple[dict, str]:
    """Run one Aura process. Return (audit record, combined log)."""
    request_path = build_request(
        run_dir, run_id, cycle_id, mode, meta, proposal, catalog, fixtures,
        score_only=score_only,
    )
    reentry = run_dir / "reentry.flag"
    if reentry.exists():
        reentry.unlink()
    log_dir = run_dir / "cycles"
    log_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["AURA_SANDBOX"] = "off"
    env["AURA_PIPELINE_STRICT"] = "0"
    env["AM_REQUEST"] = str(request_path.resolve())
    env["AM_REENTRY_FLAG"] = str(reentry.resolve())
    env["AM_RUN_ID"] = run_id
    env["AM_CYCLE_ID"] = str(cycle_id)
    if dry_run:
        env["AM_DRY_RUN"] = "1"
    else:
        env.pop("AM_DRY_RUN", None)
    lib = aura_lib_path(aura)
    if lib:
        prev = env.get("AURA_PATH", "")
        env["AURA_PATH"] = lib if not prev else lib + ":" + prev
    if beats is not None:
        beats.write(run_dir, "launch", cycle_id, None)
    started = time.monotonic()
    proc = subprocess.Popen(
        [aura, str(AGENT)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    if beats is not None:
        beats.write(run_dir, "launch", cycle_id, proc.pid)
    timed_out = False
    try:
        output, _ = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            output, _ = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            output, _ = proc.communicate()
    duration_ms = int((time.monotonic() - started) * 1000)
    text = output or ""
    (log_dir / f"{cycle_id}.log").write_text(text, encoding="utf-8")
    proposal_id = proposal.get("id") if isinstance(proposal, dict) else None
    if not isinstance(proposal_id, str):
        proposal_id = None
    if timed_out:
        record = synthetic_path_broken(
            run_id=run_id,
            cycle_id=cycle_id,
            reason="timeout",
            proposal_id=proposal_id,
            duration_ms=duration_ms,
        )
        return record, text
    parsed = extract_cycle_json(text)
    if parsed is not None:
        parsed["schema"] = AUDIT_SCHEMA
        parsed["run_id"] = run_id
        parsed["cycle_id"] = cycle_id
        parsed["duration_ms"] = duration_ms
        parsed.setdefault("author", "aura-maintainer")
        parsed.setdefault("snapshot_id", None)
        return parsed, text
    # A complete CYCLE_JSON line is the contract. A nonzero exit with no
    # line is a crash. Retry once: this runtime has exited before printing.
    if crash_retries > 0 and proc.returncode not in (0, None):
        return spawn_cycle(
            aura, run_dir, run_id, cycle_id, mode, meta, timeout_sec,
            proposal, dry_run, catalog, fixtures, beats, crash_retries - 1,
            score_only,
        )
    reason = "crash" if proc.returncode not in (0, None) else "bad-json"
    record = synthetic_path_broken(
        run_id=run_id,
        cycle_id=cycle_id,
        reason=reason,
        proposal_id=proposal_id,
        duration_ms=duration_ms,
    )
    return record, text


def champion_fixture_rows(
    aura: str,
    run_dir: Path,
    run_id: str,
    cycle_id: int,
    mode: str,
    meta: dict,
    timeout_sec: float,
    catalog: Catalog,
    fixtures: list,
) -> list[dict]:
    """Score the live champion. The record is not an audit cycle."""
    record, _log = spawn_cycle(
        aura, run_dir, run_id, f"score-{cycle_id}", mode, meta, timeout_sec,
        None, False, catalog, fixtures, None, 1, True,
    )
    scored: list = []
    tests = record.get("tests")
    if isinstance(tests, list) and tests and isinstance(tests[0], dict):
        raw = tests[0].get("baseline_rows")
        if isinstance(raw, list):
            scored = raw
    by_id: dict = {}
    for row in scored:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            by_id[row["id"]] = row.get("got")
    view = []
    for row in fixtures:
        if not isinstance(row, dict):
            continue
        view.append({
            "id": row.get("id"),
            "args": row.get("args"),
            "expect": row.get("expect", ""),
            "got": by_id.get(row.get("id"), "?"),
        })
    return view


def _keep_after(meta: dict, proposal: dict, digest: str) -> tuple[dict, int]:
    generation = int(meta.get("generation") or 0) + 1
    kind = proposal.get("kind")
    if kind == "param":
        params = proposal.get("params") if isinstance(proposal.get("params"), dict) else meta.get("params")
        return {
            "family": "template",
            "profile": "template",
            "params": dict(params),
            "body_sha256": digest,
        }, generation
    if kind == "profile":
        params = meta.get("params") if isinstance(meta.get("params"), dict) else {}
        return {
            "family": "profile",
            "profile": proposal.get("profile") or "profile",
            "params": dict(params),
            "body_sha256": digest,
        }, generation
    params = meta.get("params") if isinstance(meta.get("params"), dict) else {}
    return {
        "family": meta.get("family"),
        "profile": meta.get("profile"),
        "params": dict(params),
        "body_sha256": digest,
    }, generation


def commit_record(
    run_dir: Path,
    record: dict,
    *,
    proposal: dict | None,
    meta: dict,
    tried: set[str],
    catalog: Catalog,
) -> dict:
    """Body file, fsync the audit line, then champion rename on a legal KEEP."""
    before = meta_view(meta)
    record["before"] = before
    record["parent_body_sha256"] = meta.get("body_sha256")
    record["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if proposal and not record.get("proposal_id"):
        record["proposal_id"] = proposal.get("id")
    if proposal and "issue" not in record:
        record["issue"] = issue_for(proposal, catalog)
    snap = record.get("snapshot_id")
    changed = False
    if proposal and isinstance(snap, int) and not isinstance(snap, bool):
        trial_sha = sha256_bytes(str(proposal.get("body") or "").encode("utf-8"))
        changed = trial_sha != meta.get("body_sha256")
    record["trial_body_changed"] = changed
    generation = int(meta.get("generation") or 0)

    if record.get("decision") == "KEEP":
        base, trial = score_pair(record)
        scores_ok = base is not None and trial is not None and trial > base
        proposal_ok = isinstance(proposal, dict) and proposal.get("keep_eligible") is True
        eligible = record.get("keep_eligible") is True and proposal_ok
        if not (eligible and scores_ok and isinstance(proposal, dict)):
            record = synthetic_path_broken(
                run_id=str(record.get("run_id") or ""),
                cycle_id=int(record.get("cycle_id") or 0),
                reason="kept-ineligible",
                proposal_id=record.get("proposal_id") if isinstance(record.get("proposal_id"), str) else None,
            )
            record["before"] = before
            record["after"] = before
            record["parent_body_sha256"] = meta.get("body_sha256")
            record["trial_body_changed"] = False
        else:
            body = str(proposal.get("body") or "")
            digest = sha256_bytes(body.encode("utf-8"))
            atomic_write(run_dir / "bodies" / f"{digest}.txt", body)
            after, generation = _keep_after(meta, proposal, digest)
            record["after"] = after
            record["champion_generation"] = generation
            record["metrics_delta"] = {"fixture_score": (trial or 0) - (base or 0)}
            record["keep_eligible"] = True
    else:
        record["after"] = before
        record["champion_generation"] = generation

    append_jsonl(run_dir / "audit.jsonl", record)
    if record.get("decision") == "KEEP":
        after = record["after"]
        digest = after["body_sha256"]
        body = (run_dir / "bodies" / f"{digest}.txt").read_text(encoding="utf-8")
        rewritten = {
            "schema": CHAMPION_SCHEMA,
            "family": after.get("family", "template"),
            "profile": after.get("profile", "template"),
            "params": after.get("params", meta.get("params")),
            "body_sha256": digest,
            "generation": record.get("champion_generation", generation),
        }
        atomic_write(run_dir / "champion.body", body)
        atomic_write(
            run_dir / "champion.meta.json",
            json.dumps(rewritten, indent=2, sort_keys=True) + "\n",
        )
    if proposal and record.get("decision") != "PATH_BROKEN":
        key_id = proposal.get("health_key") or proposal.get("id")
        if isinstance(key_id, str) and key_id:
            tried.add(mark_tried(key_id, str(meta.get("body_sha256") or "")))
    save_tried(run_dir, tried)
    rows = champion_curve(load_audit(run_dir / "audit.jsonl"))
    atomic_write(
        run_dir / "curve.jsonl",
        "".join(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n" for row in rows),
    )
    return record


def next_cycle_id(records: list[dict]) -> int:
    ids = [rec.get("cycle_id") for rec in records if isinstance(rec.get("cycle_id"), int)]
    return (max(ids) + 1) if ids else 1


def streak_from(records: list[dict]) -> int:
    streak = 0
    for rec in records:
        streak = next_streak(streak, rec)
        if streak >= STREAK_LIMIT:
            return streak
    return streak


def idle_record(run_id: str, cycle_id: int, reason: str) -> dict:
    return {
        "schema": AUDIT_SCHEMA,
        "run_id": run_id,
        "cycle_id": cycle_id,
        "decision": "IDLE",
        "reason": reason,
        "outcome_class": "idle",
        "proposal_id": None,
        "snapshot_id": None,
        "author": "aura-maintainer",
        "duration_ms": 0,
        "warnings": [],
    }


def rejection_record(run_id: str, cycle_id: int, proposal: dict, reason: str) -> dict:
    return {
        "schema": AUDIT_SCHEMA,
        "run_id": run_id,
        "cycle_id": cycle_id,
        "decision": "ROLLBACK",
        "reason": reason,
        "outcome_class": "rejection",
        "proposal_id": proposal.get("id"),
        "keep_eligible": False,
        "snapshot_id": None,
        "author": "aura-maintainer",
        "duration_ms": 0,
        "warnings": [],
    }


def health_proposal(catalog: Catalog, index: int) -> dict:
    return {
        "id": "probe:inverted",
        "kind": "probe",
        "outcome_class": "probe",
        "keep_eligible": False,
        "body": catalog.bodies["inverted"],
        "profile": "inverted",
        "health_key": f"health-{index}",
    }


def llm_proposal(body: str, cycle_id: int) -> dict:
    return {
        "id": f"llm:{cycle_id}",
        "kind": "llm",
        "outcome_class": "improvement",
        "keep_eligible": True,
        "body": body,
    }


def idle_wait(run_dir: Path, beats: Heartbeat, deadline: float, cycle_id: int) -> None:
    """Refresh the heartbeat once a second, and do not spawn Aura."""
    end = min(deadline, time.monotonic() + IDLE_SLICE_SEC)
    while time.monotonic() < end:
        beats.write(run_dir, "idle", cycle_id, None)
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Run the aura-maintainer outer loop")
    ap.add_argument("--mode", choices=("hold", "recover"), default="hold")
    ap.add_argument("--cycles", type=int, default=None, help="Aura process cap. 0 means no cap")
    ap.add_argument("--hours", type=float, default=None, help="Wall clock cap. Must be <= 72")
    ap.add_argument("--aura-bin", default=None)
    ap.add_argument("--resume", default=None, help="Existing reports/<run-id> directory name")
    ap.add_argument("--run-id", default=None, help="Run id to create. Default is a UTC timestamp")
    ap.add_argument("--timeout-sec", type=float, default=20)
    ap.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS)
    ap.add_argument("--dry-run", action="store_true", help="Seed and locate, do not rebind the proposal")
    ap.add_argument("--proposer", choices=("rules", "llm"), default="rules")
    ap.add_argument("--health-every", type=float, default=1800, help="Seconds between soak health probes. 0 runs one on every idle slot")
    ap.add_argument("--llm-key", type=Path, default=None, help="Override ~/code/keys/minimax")
    return ap.parse_args(argv)


def _finish_cycle(
    run_dir: Path,
    run_id: str,
    record: dict,
    proposal: dict | None,
    meta: dict,
    tried: set[str],
    catalog: Catalog,
    beats: Heartbeat,
    streak: int,
) -> tuple[dict, int, bool]:
    written = commit_record(
        run_dir, record, proposal=proposal, meta=meta, tried=tried, catalog=catalog,
    )
    beats.write(run_dir, "recorded", written.get("cycle_id") or 0, None)
    streak = next_streak(streak, written)
    stopped = False
    if tree_dirty():
        dirty_id = next_cycle_id(load_audit(run_dir / "audit.jsonl"))
        dirty = synthetic_path_broken(run_id=run_id, cycle_id=dirty_id, reason="tree-dirty")
        written = commit_record(
            run_dir, dirty, proposal=None, meta=meta, tried=tried, catalog=catalog,
        )
        stopped = True
        print("cycle tree-dirty; stopping", file=sys.stderr)
        return written, streak, stopped
    if silent_epoch_streak(load_audit(run_dir / "audit.jsonl")) >= EPOCH_SILENT_LIMIT:
        silent_id = next_cycle_id(load_audit(run_dir / "audit.jsonl"))
        silent = synthetic_path_broken(
            run_id=run_id, cycle_id=silent_id, reason="incremental-compile-silent",
        )
        written = commit_record(
            run_dir, silent, proposal=None, meta=meta, tried=tried, catalog=catalog,
        )
        stopped = True
        print("cycle incremental-compile-silent; stopping", file=sys.stderr)
    return written, streak, stopped


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.hours is not None and args.hours > 72:
        print("error: --hours must be <= 72", file=sys.stderr)
        return 2
    if args.cycles is None:
        args.cycles = 24 if args.mode == "recover" else 8
    if args.cycles < 0:
        print("error: --cycles must be >= 0", file=sys.stderr)
        return 2
    if args.cycles == 0 and args.hours is None:
        print("error: --cycles 0 requires --hours", file=sys.stderr)
        return 2
    if args.timeout_sec <= 0:
        print("error: --timeout-sec must be > 0", file=sys.stderr)
        return 2

    aura = args.aura_bin or find_aura_bin()
    if not aura or not Path(aura).is_file():
        print("error: aura binary not found. Set AURA_BIN or pass --aura-bin.", file=sys.stderr)
        return 1

    catalog = load_catalog(ROOT)
    fixtures = load_fixtures()
    rules = RulesProposer(catalog)
    llm_mod = None
    if args.proposer == "llm":
        from harness.proposers import llm as llm_mod

    reports = args.reports_dir
    reports.mkdir(parents=True, exist_ok=True)
    if args.resume:
        run_id = args.resume
        run_dir = reports / run_id
        if not run_dir.is_dir():
            print(f"error: resume directory not found: {run_dir}", file=sys.stderr)
            return 2
    else:
        run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        run_dir = reports / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

    environ = {
        "aura_bin": str(Path(aura).resolve()),
        "aura_sha256": sha256_file(Path(aura)),
        "AURA_SANDBOX": "off",
        "AURA_PIPELINE_STRICT": "0",
        "mode": args.mode,
        "dry_run": bool(args.dry_run),
        "proposer": args.proposer,
    }
    atomic_write(run_dir / "environ.json", json.dumps(environ, indent=2, sort_keys=True) + "\n")

    if args.resume:
        reason = reconcile_resume(run_dir)
        if reason is not None:
            records = load_audit(run_dir / "audit.jsonl")
            cycle_id = next_cycle_id(records)
            record = synthetic_path_broken(run_id=run_id, cycle_id=cycle_id, reason=reason)
            append_jsonl(run_dir / "audit.jsonl", record)
            print(f"resume stopped: {reason}", file=sys.stderr)
            return 3
    meta = ensure_seed(run_dir, args.mode, catalog)
    if meta.get("schema") != CHAMPION_SCHEMA:
        records = load_audit(run_dir / "audit.jsonl")
        record = synthetic_path_broken(
            run_id=run_id,
            cycle_id=next_cycle_id(records),
            reason="resume-bad-schema",
        )
        append_jsonl(run_dir / "audit.jsonl", record)
        return 3

    records = load_audit(run_dir / "audit.jsonl")
    streak = streak_from(records)
    if streak >= STREAK_LIMIT:
        print(f"run={run_id} already at PATH_BROKEN streak {streak}", file=sys.stderr)
        return 3

    tried = load_tried(run_dir)
    beats = Heartbeat()
    deadline = None
    if args.hours is not None:
        deadline = time.monotonic() + (args.hours * 3600.0)
    spawned = 0
    stopped_early = False
    health_index = 1 + sum(1 for key in tried if key.startswith("health-"))
    last_health = time.monotonic()
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            break
        if args.cycles != 0 and spawned >= args.cycles:
            break
        if streak >= STREAK_LIMIT:
            stopped_early = True
            break
        cycle_id = next_cycle_id(load_audit(run_dir / "audit.jsonl"))
        meta = read_json(run_dir / "champion.meta.json") or meta
        body = (run_dir / "champion.body").read_text(encoding="utf-8")
        champion = champion_from(meta, body)
        proposal: dict | None
        if args.proposer == "llm":
            assert llm_mod is not None
            stub = os.environ.get("AM_LLM_STUB_BODY") if "AM_LLM_STUB_BODY" in os.environ else None
            fixture_rows = None
            if stub is None and llm_mod.key_available(args.llm_key):
                fixture_rows = champion_fixture_rows(
                    aura, run_dir, run_id, cycle_id, args.mode, meta,
                    args.timeout_sec, catalog, fixtures,
                )
            produced, why = llm_mod.propose_body(
                key_file=args.llm_key,
                stub=stub,
                champion_body=body,
                fixture_rows=fixture_rows,
            )
            if produced is None:
                proposal = None
                record = idle_record(run_id, cycle_id, why)
                record, streak, stopped_early = _finish_cycle(
                    run_dir, run_id, record, None, meta, tried, catalog, beats, streak,
                )
                spawned += 1
                print(
                    f"cycle={cycle_id} decision={record.get('decision')} reason={record.get('reason')}",
                    flush=True,
                )
                if stopped_early or streak >= STREAK_LIMIT:
                    stopped_early = True
                    break
                continue
            if grammar_ok(produced) and not call_heads_allowed(produced):
                proposal = llm_proposal(produced, cycle_id)
                record = rejection_record(run_id, cycle_id, proposal, "proposal:call-head")
                record, streak, stopped_early = _finish_cycle(
                    run_dir, run_id, record, proposal, meta, tried, catalog, beats, streak,
                )
                spawned += 1
                print(
                    f"cycle={cycle_id} decision={record.get('decision')} reason={record.get('reason')}",
                    flush=True,
                )
                if stopped_early or streak >= STREAK_LIMIT:
                    stopped_early = True
                    break
                continue
            proposal = llm_proposal(produced, cycle_id)
        else:
            proposal = rules.propose(champion, tried)
        if proposal is None:
            if deadline is None:
                break
            elapsed = time.monotonic() - last_health
            if args.health_every == 0 or elapsed >= args.health_every:
                proposal = health_proposal(catalog, health_index)
                health_index += 1
                last_health = time.monotonic()
            else:
                idle_wait(run_dir, beats, deadline, cycle_id)
                continue
        record, _log = spawn_cycle(
            aura, run_dir, run_id, cycle_id, args.mode, meta, args.timeout_sec,
            proposal, bool(args.dry_run), catalog, fixtures, beats,
        )
        record, streak, stopped_early = _finish_cycle(
            run_dir, run_id, record, proposal, meta, tried, catalog, beats, streak,
        )
        spawned += 1
        print(
            f"cycle={cycle_id} decision={record.get('decision')} reason={record.get('reason')}",
            flush=True,
        )
        if stopped_early or streak >= STREAK_LIMIT:
            stopped_early = True
            break

    final = load_audit(run_dir / "audit.jsonl")
    code = exit_code_for(final, stopped_early=stopped_early)
    print(f"run={run_id} exit={code} cycles_spawned={spawned} audit={run_dir / 'audit.jsonl'}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
