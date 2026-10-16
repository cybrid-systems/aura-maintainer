"""--mode repo entry. One comment edit in a maintainer worktree.

An empty injected registry still fsyncs one IDLE and does not apply.
The default registry is the comment-drift class. This module does not
import the LLM proposer and does not spawn Aura.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from harness.audit import (
    STREAK_LIMIT,
    append_jsonl,
    atomic_write,
    exit_code_for,
    load_audit,
    next_streak,
)
from harness.catalog import extract_policy_string
from harness.repo_audit import (
    AUDIT_SCHEMA,
    idle_record,
    keep_record,
    path_broken_record,
    rollback_record,
)
from harness.repo_catalog import COMMENT_CLASS, OLD_LINE, ProposeOutcome
from harness.run import (
    ROOT,
    TARGET,
    Heartbeat,
    load_tried,
    next_cycle_id,
    save_tried,
    sha256_bytes,
)
from harness.worktree import (
    Layout,
    apply_bytes as apply_worktree,
    atomic_write_bytes,
    create_worktree,
    guard_allowlist,
    human_tree_status,
    materialize,
    read_base,
    reconcile,
    restore_base,
    set_git_log,
    worktree_dir,
)

_COMMON = (
    "schema",
    "run_id",
    "cycle_id",
    "ts",
    "mode",
    "decision",
    "reason",
    "base_git_sha",
    "author",
    "warnings",
)
_TRIED_REASONS = {
    "proposal:oracle-failed",
    "proposal:pattern-not-unique",
    "proposal:diff-rejected",
}
_IMMEDIATE = {"tree-dirty", "tree-status-failed"}


@dataclass
class _Cycle:
    rows: list[dict]
    stopped_early: bool = False
    materialize_failed: bool = False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_environ(run_dir: Path, args, aura_bin: str | None, aura_sha: str | None, base_sha: str | None) -> None:
    environ = {
        "aura_bin": aura_bin,
        "aura_sha256": aura_sha,
        "AURA_SANDBOX": "off",
        "AURA_PIPELINE_STRICT": "0",
        "mode": "repo",
        "dry_run": bool(args.dry_run),
        "proposer": args.proposer,
        "base_git_sha": base_sha,
    }
    atomic_write(run_dir / "environ.json", json.dumps(environ, indent=2, sort_keys=True) + "\n")


def _base_sha(layout: Layout) -> str | None:
    doc = read_base(layout)
    sha = None if doc is None else doc.get("git_sha")
    return sha if isinstance(sha, str) else None


def _bad_schema(rows: list[dict]) -> bool:
    for rec in rows:
        if rec.get("schema") != AUDIT_SCHEMA:
            return True
        if any(key not in rec for key in _COMMON):
            return True
    return False


def _streak(rows: list[dict]) -> int:
    streak = 0
    for rec in rows:
        streak = next_streak(streak, rec)
    return streak


def _budget_used(rows: list[dict]) -> int:
    return sum(1 for rec in rows if rec.get("reason") != "proposal:dry-run")


def _stop(rows: list[dict], cycles: int, dry_run: bool) -> bool:
    """Stop without appending. The exit code is still exit_code_for."""
    if not rows:
        return False
    last = rows[-1]
    if last.get("decision") == "IDLE":
        return True
    if last.get("reason") == "proposal:pattern-not-unique":
        return True
    if dry_run and any(rec.get("reason") == "proposal:dry-run" for rec in rows):
        return True
    return _budget_used(rows) >= cycles


def _rewrite_curve(run_dir: Path, rows: list[dict]) -> None:
    seen_keep = False
    lines: list[str] = []
    for rec in rows:
        if rec.get("decision") == "KEEP":
            seen_keep = True
        row = {
            "cycle_id": rec.get("cycle_id"),
            "decision": rec.get("decision"),
            "outcome_class": rec.get("outcome_class"),
            "oracle": "comment-drift",
            "drift_open": 0 if seen_keep else 1,
            "human_tree_clean": rec.get("human_tree_clean") is True,
        }
        lines.append(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
    atomic_write(run_dir / "curve.jsonl", "".join(lines))


def _remember(run_dir: Path, proposal_id: str, preimage_sha: str) -> None:
    tried = load_tried(run_dir)
    tried.add(f"{proposal_id}|{preimage_sha}")
    save_tried(run_dir, tried)


def _commit_row(layout: Layout, record: dict, *, remember: tuple[str, str] | None = None) -> list[dict]:
    append_jsonl(layout.run_dir / "audit.jsonl", record)
    if remember is not None:
        _remember(layout.run_dir, remember[0], remember[1])
    rows = load_audit(layout.run_dir / "audit.jsonl")
    _rewrite_curve(layout.run_dir, rows)
    return rows


def _broken(
    layout: Layout,
    *,
    reason: str,
    cycle_id: int,
    proposal_id: str | None = None,
    preimage_sha256: str | None = None,
) -> list[dict]:
    record = path_broken_record(
        run_id=layout.run_dir.name,
        cycle_id=cycle_id,
        reason=reason,
        base_git_sha=_base_sha(layout),
        proposal_id=proposal_id,
        preimage_sha256=preimage_sha256,
    )
    return _commit_row(layout, record)


def _drop_pending(layout: Layout) -> None:
    pending = layout.run_dir / "pending.json"
    if pending.exists():
        pending.unlink()


def _patch_text(relpath: str, before: str, after: str) -> str:
    # Content lines keep their own newlines. lineterm is only the headers.
    body = "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{relpath}",
        tofile=f"b/{relpath}",
        lineterm="\n",
    ))
    if body and not body.endswith("\n"):
        body += "\n"
    return f"diff --git a/{relpath} b/{relpath}\n{body}"


def _comment_line(source: str) -> int:
    hits = [i for i, line in enumerate(source.splitlines(), start=1) if line == OLD_LINE]
    return hits[0] if len(hits) == 1 else 0


def _finish(run_id: str, run_dir: Path, rows: list[dict], *, stopped_early: bool) -> int:
    rc = exit_code_for(rows, stopped_early=stopped_early)
    print(f"run={run_id} exit={rc} cycles_spawned=0 audit={run_dir / 'audit.jsonl'}")
    return rc


def _one_cycle(layout: Layout, args, registry: tuple, rows: list[dict], beat: Heartbeat) -> _Cycle:
    cycle_id = next_cycle_id(rows)
    set_git_log(layout.run_dir / "cycles" / f"{cycle_id}.log")
    base = _base_sha(layout)
    dest = worktree_dir(layout) / layout.relpath
    if dest.is_symlink():
        return _Cycle(_broken(layout, reason="symlink-refused", cycle_id=cycle_id))
    try:
        raw = dest.read_bytes()
    except OSError:
        return _Cycle(_broken(layout, reason="path-not-allowlisted", cycle_id=cycle_id))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _Cycle(_broken(layout, reason="decode-failed", cycle_id=cycle_id))
    preimage_sha = sha256_bytes(raw)
    tried = load_tried(layout.run_dir)
    chosen = None
    for cls in registry:
        key = f"{cls.proposal_id}|{preimage_sha}"
        if key in tried:
            continue
        proposed = cls.propose(text)
        if proposed is ProposeOutcome.NOT_UNIQUE:
            record = rollback_record(
                run_id=layout.run_dir.name,
                cycle_id=cycle_id,
                reason="proposal:pattern-not-unique",
                base_git_sha=base,
                proposal_id=cls.proposal_id,
                path=cls.path,
                preimage_sha256=preimage_sha,
            )
            beat.write(layout.run_dir, "recorded", cycle_id, os.getpid())
            return _Cycle(_commit_row(
                layout, record, remember=(cls.proposal_id, preimage_sha),
            ))
        if proposed is ProposeOutcome.ABSENT or not isinstance(proposed, str):
            continue
        chosen = (cls, proposed)
        break
    if chosen is None:
        record = idle_record(
            run_id=layout.run_dir.name,
            cycle_id=cycle_id,
            base_git_sha=base,
            reason="repo-catalog-empty",
        )
        beat.write(layout.run_dir, "recorded", cycle_id, os.getpid())
        return _Cycle(_commit_row(layout, record))

    cls, proposed = chosen
    started = time.monotonic()
    try:
        extract_policy_string(text, "normal")
    except ValueError:
        return _Cycle(_broken(
            layout,
            reason="extract-failed",
            cycle_id=cycle_id,
            proposal_id=cls.proposal_id,
            preimage_sha256=preimage_sha,
        ))
    try:
        passed = cls.oracle(text, proposed)
    except Exception:
        fresh = _broken(
            layout,
            reason="oracle-crash",
            cycle_id=cycle_id,
            proposal_id=cls.proposal_id,
            preimage_sha256=preimage_sha,
        )
        return _Cycle(fresh, stopped_early=_streak(fresh) >= STREAK_LIMIT)
    after_sha = sha256_bytes(proposed.encode("utf-8"))
    if not passed:
        record = rollback_record(
            run_id=layout.run_dir.name,
            cycle_id=cycle_id,
            reason="proposal:diff-rejected",
            base_git_sha=base,
            proposal_id=cls.proposal_id,
            path=cls.path,
            preimage_sha256=preimage_sha,
            after_sha256=after_sha,
        )
        beat.write(layout.run_dir, "recorded", cycle_id, os.getpid())
        return _Cycle(_commit_row(layout, record, remember=(cls.proposal_id, preimage_sha)))
    if args.dry_run:
        record = rollback_record(
            run_id=layout.run_dir.name,
            cycle_id=cycle_id,
            reason="proposal:dry-run",
            base_git_sha=base,
            proposal_id=cls.proposal_id,
            path=cls.path,
            preimage_sha256=preimage_sha,
        )
        beat.write(layout.run_dir, "oracle", cycle_id, os.getpid())
        beat.write(layout.run_dir, "recorded", cycle_id, os.getpid())
        return _Cycle(_commit_row(layout, record))

    beat.write(layout.run_dir, "arm", cycle_id, os.getpid())
    applied = apply_worktree(layout, proposed.encode("utf-8"), cycle_id=cycle_id)
    if not applied.ok:
        reason = applied.reason or "path-not-allowlisted"
        fresh = _broken(
            layout,
            reason=reason,
            cycle_id=cycle_id,
            proposal_id=cls.proposal_id,
        )
        early = reason in _IMMEDIATE or _streak(fresh) >= STREAK_LIMIT
        return _Cycle(fresh, stopped_early=early)
    beat.write(layout.run_dir, "apply", cycle_id, os.getpid())
    try:
        live = dest.read_bytes()
        live_text = live.decode("utf-8")
        extract_policy_string(live_text, "normal")
        passed = cls.oracle(text, live_text)
    except ValueError:
        restore_base(layout, raw)
        _drop_pending(layout)
        return _Cycle(_broken(
            layout,
            reason="extract-failed",
            cycle_id=cycle_id,
            proposal_id=cls.proposal_id,
            preimage_sha256=preimage_sha,
        ))
    except UnicodeDecodeError:
        restore_base(layout, raw)
        _drop_pending(layout)
        return _Cycle(_broken(
            layout,
            reason="decode-failed",
            cycle_id=cycle_id,
            proposal_id=cls.proposal_id,
            preimage_sha256=preimage_sha,
        ))
    except Exception:
        restore_base(layout, raw)
        _drop_pending(layout)
        return _Cycle(_broken(
            layout,
            reason="oracle-crash",
            cycle_id=cycle_id,
            proposal_id=cls.proposal_id,
            preimage_sha256=preimage_sha,
        ), stopped_early=True)
    beat.write(layout.run_dir, "oracle", cycle_id, os.getpid())
    if time.monotonic() - started > 5:
        restore_base(layout, raw)
        _drop_pending(layout)
        return _Cycle(_broken(
            layout,
            reason="timeout",
            cycle_id=cycle_id,
            proposal_id=cls.proposal_id,
            preimage_sha256=preimage_sha,
        ))
    if not passed:
        restored = restore_base(layout, raw)
        _drop_pending(layout)
        if not restored.ok:
            return _Cycle(_broken(
                layout,
                reason="restore-mismatch",
                cycle_id=cycle_id,
                proposal_id=cls.proposal_id,
                preimage_sha256=preimage_sha,
            ), stopped_early=True)
        record = rollback_record(
            run_id=layout.run_dir.name,
            cycle_id=cycle_id,
            reason="proposal:oracle-failed",
            base_git_sha=base,
            proposal_id=cls.proposal_id,
            path=cls.path,
            preimage_sha256=preimage_sha,
            after_sha256=sha256_bytes(live),
        )
        beat.write(layout.run_dir, "recorded", cycle_id, os.getpid())
        return _Cycle(_commit_row(layout, record, remember=(cls.proposal_id, preimage_sha)))

    guard = guard_allowlist(layout)
    if not guard.ok:
        fresh = load_audit(layout.run_dir / "audit.jsonl")
        if not guard.appended:
            fresh = _broken(
                layout,
                reason="path-not-allowlisted",
                cycle_id=cycle_id,
                proposal_id=cls.proposal_id,
                preimage_sha256=preimage_sha,
            )
        else:
            _rewrite_curve(layout.run_dir, fresh)
        early = _streak(fresh) >= STREAK_LIMIT
        return _Cycle(fresh, stopped_early=early)

    status = human_tree_status(layout)
    if status is not None:
        restore_base(layout, raw)
        _drop_pending(layout)
        fresh = _broken(
            layout,
            reason=status,
            cycle_id=cycle_id,
            proposal_id=cls.proposal_id,
            preimage_sha256=preimage_sha,
        )
        return _Cycle(fresh, stopped_early=True)

    relpath = cls.path
    patch_rel = f"patches/{cycle_id}.patch"
    atomic_write(layout.run_dir / patch_rel, _patch_text(relpath, text, live_text))
    after_sha = sha256_bytes(live)
    atomic_write_bytes(layout.run_dir / "blobs" / after_sha, live)
    try:
        body = extract_policy_string(live_text, "normal")[0]
        body_sha = sha256_bytes(body.encode("utf-8"))
    except ValueError:
        restore_base(layout, raw)
        _drop_pending(layout)
        return _Cycle(_broken(
            layout,
            reason="extract-failed",
            cycle_id=cycle_id,
            proposal_id=cls.proposal_id,
            preimage_sha256=preimage_sha,
        ))
    record = keep_record(
        run_id=layout.run_dir.name,
        cycle_id=cycle_id,
        base_git_sha=base or "",
        path=relpath,
        preimage_sha256=preimage_sha,
        after_sha256=after_sha,
        body_sha256=body_sha,
        patch=patch_rel,
        issue={
            "id": cls.issue_id,
            "kind": "drift",
            "path": relpath,
            "line": _comment_line(text),
            "summary": "comment claims erate >= 50 returns noop|flat; the cond has no such branch",
        },
        oracle={
            "name": "oracle:comment-drift:erate-50",
            "before_pass": False,
            "after_pass": True,
        },
        reason="comment-drift cleared; body unchanged",
        human_tree_clean=True,
        proposal_id=cls.proposal_id,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    fresh = _commit_row(layout, record, remember=(cls.proposal_id, preimage_sha))
    beat.write(layout.run_dir, "recorded", cycle_id, os.getpid())
    materialized = materialize(layout, record)
    if not materialized.ok:
        return _Cycle(fresh, materialize_failed=True)
    return _Cycle(fresh)


def _run_catalog(args, registry: tuple, run_id: str, run_dir: Path, aura_bin, aura_sha) -> int:
    layout = Layout(source=TARGET, parent=ROOT, run_dir=run_dir)
    cycles = 2 if args.cycles is None else args.cycles
    beat = Heartbeat()
    set_git_log(run_dir / "cycles" / "0.log")
    try:
        rows = load_audit(run_dir / "audit.jsonl")
        if rows and _bad_schema(rows):
            if not any(rec.get("decision") in {"KEEP", "ROLLBACK", "IDLE"} for rec in rows):
                _broken(layout, reason="resume-bad-schema", cycle_id=next_cycle_id(rows))
                rows = load_audit(run_dir / "audit.jsonl")
            return _finish(run_id, run_dir, rows, stopped_early=True)

        if args.resume:
            step = reconcile(layout)
            rows = load_audit(run_dir / "audit.jsonl")
            _rewrite_curve(run_dir, rows)
            _write_environ(run_dir, args, aura_bin, aura_sha, _base_sha(layout))
            if not step.ok:
                print(f"run={run_id} exit=3 cycles_spawned=0 audit={run_dir / 'audit.jsonl'}")
                return 3
        else:
            started = time.monotonic()
            made = create_worktree(layout)
            rows = load_audit(run_dir / "audit.jsonl")
            _rewrite_curve(run_dir, rows)
            _write_environ(run_dir, args, aura_bin, aura_sha, _base_sha(layout))
            if not made.ok:
                early = made.reason in _IMMEDIATE or _streak(rows) >= STREAK_LIMIT
                rc = exit_code_for(rows, stopped_early=early)
                if rc == 0:
                    rc = 3
                print(f"run={run_id} exit={rc} cycles_spawned=0 audit={run_dir / 'audit.jsonl'}")
                return rc
            if time.monotonic() - started > 30:
                rows = _broken(layout, reason="timeout", cycle_id=next_cycle_id(rows))
                return _finish(run_id, run_dir, rows, stopped_early=True)

        while not _stop(rows, cycles, bool(args.dry_run)):
            outcome = _one_cycle(layout, args, registry, rows, beat)
            rows = outcome.rows
            if outcome.materialize_failed:
                print(f"run={run_id} exit=3 cycles_spawned=0 audit={run_dir / 'audit.jsonl'}")
                return 3
            if outcome.stopped_early:
                return _finish(run_id, run_dir, rows, stopped_early=True)
        return _finish(run_id, run_dir, rows, stopped_early=False)
    finally:
        set_git_log(None)


def main(args, registry: tuple | None = None) -> int:
    """Run repo mode. An empty registry fsyncs one IDLE and returns 0."""
    if registry is None:
        registry = (COMMENT_CLASS,)
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

    aura_bin = None
    aura_sha = None
    if args.aura_bin and Path(args.aura_bin).is_file():
        aura_bin = str(Path(args.aura_bin).resolve())
        aura_sha = _sha256_file(Path(aura_bin))

    if not registry:
        _write_environ(run_dir, args, aura_bin, aura_sha, None)
        records = load_audit(run_dir / "audit.jsonl")
        if records:
            print(f"run={run_id} exit=0 cycles_spawned=0 audit={run_dir / 'audit.jsonl'}")
            return 0
        record = idle_record(
            run_id=run_id,
            cycle_id=1,
            base_git_sha=None,
            reason="repo-catalog-empty",
        )
        append_jsonl(run_dir / "audit.jsonl", record)
        print(f"run={run_id} exit=0 cycles_spawned=0 audit={run_dir / 'audit.jsonl'}")
        return 0

    return _run_catalog(args, registry, run_id, run_dir, aura_bin, aura_sha)
