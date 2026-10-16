"""Maintainer-owned git worktree. The human checkout is not the apply surface.

`rev-parse` and `worktree add` run in the source repo. `add`, `commit`,
`checkout`, and `show` run in the new worktree. This module does not prune
worktrees and does not clone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from harness.audit import append_jsonl, atomic_write, load_audit
from harness.repo_audit import BASE_SCHEMA, PENDING_SCHEMA, path_broken_record
from harness.run import sha256_bytes

ALLOW_PATH = "src/redis/policy/choose_normal.aura"
COMMIT_SUBJECT = "maintainer: clear comment-drift choose_normal erate-50"
_git_log: Path | None = None
_GIT_ID = (
    "-c", "user.name=aura-maintainer",
    "-c", "user.email=aura-maintainer@localhost",
    "-c", "commit.gpgsign=false",
)


@dataclass(frozen=True)
class Layout:
    source: Path
    parent: Path
    run_dir: Path
    relpath: str = ALLOW_PATH
    gitlink: str = "target/aura-redis"


@dataclass
class Step:
    ok: bool
    reason: str | None = None
    appended: bool = False


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def worktree_dir(layout: Layout) -> Path:
    return layout.run_dir / "worktree"


def set_git_log(path: Path | None) -> None:
    """Append later git stderr to this file. None turns the log off."""
    global _git_log
    _git_log = path


def _git(cwd: Path, *args: str, identity: bool = False) -> subprocess.CompletedProcess[str]:
    cmd = ["git", *_GIT_ID, *args] if identity else ["git", *args]
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        proc = subprocess.CompletedProcess(cmd, 1, "", str(exc))
    if _git_log is not None and proc.stderr:
        _git_log.parent.mkdir(parents=True, exist_ok=True)
        with _git_log.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(proc.stderr)
            if not proc.stderr.endswith("\n"):
                fh.write("\n")
    return proc


def _closed(records: list[dict]) -> bool:
    return any(rec.get("decision") in {"KEEP", "ROLLBACK", "IDLE"} for rec in records)


def human_tree_status(layout: Layout) -> str | None:
    """None when both porcelain checks are clean. Never treats a failed status as clean."""
    parent = _git(layout.parent, "status", "--porcelain", "--", layout.gitlink)
    if parent.returncode != 0:
        return "tree-status-failed"
    source = _git(layout.source, "status", "--porcelain", "--", ".")
    if source.returncode != 0:
        return "tree-status-failed"
    for proc in (parent, source):
        for line in proc.stdout.splitlines():
            if len(line) < 4:
                continue
            status = line[:2]
            if status == "??":
                continue
            if "M" in status or "D" in status:
                return "tree-dirty"
    return None


def registered(layout: Layout) -> bool:
    dest = worktree_dir(layout).resolve()
    proc = _git(layout.source, "worktree", "list", "--porcelain")
    if proc.returncode != 0:
        return False
    for line in proc.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        listed = Path(line.split(" ", 1)[1]).resolve()
        if listed == dest:
            return True
    return False


def _under_run(layout: Layout, path: Path) -> bool:
    run = layout.run_dir.resolve()
    try:
        path.resolve().relative_to(run)
    except ValueError:
        return False
    return True


def _append_broken(
    layout: Layout,
    reason: str,
    base: str | None,
    *,
    preimage_sha256: str | None = None,
    proposal_id: str | None = None,
) -> None:
    rows = load_audit(layout.run_dir / "audit.jsonl")
    cycle_id = 1 + max((int(rec.get("cycle_id") or 0) for rec in rows), default=0)
    append_jsonl(
        layout.run_dir / "audit.jsonl",
        path_broken_record(
            run_id=layout.run_dir.name,
            cycle_id=cycle_id,
            reason=reason,
            base_git_sha=base,
            proposal_id=proposal_id,
            preimage_sha256=preimage_sha256,
        ),
    )


def _fail(layout: Layout, reason: str, base: str | None, *, may_append: bool) -> Step:
    if may_append:
        _append_broken(layout, reason, base)
        return Step(False, reason, True)
    return Step(False, reason, False)


def read_base(layout: Layout) -> dict | None:
    path = layout.run_dir / "base.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != BASE_SCHEMA:
        return None
    if not isinstance(data.get("git_sha"), str):
        return None
    return data


def write_base(layout: Layout, git_sha: str) -> None:
    payload = {
        "schema": BASE_SCHEMA,
        "git_sha": git_sha,
        "worktree": str(worktree_dir(layout).resolve()),
        "human_checkout": layout.gitlink,
        "path": layout.relpath,
    }
    atomic_write(layout.run_dir / "base.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")


def head_sha(cwd: Path) -> str | None:
    proc = _git(cwd, "rev-parse", "HEAD")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _show_bytes(cwd: Path, rev: str, relpath: str) -> bytes | None:
    proc = subprocess.run(
        ["git", "show", f"{rev}:{relpath}"],
        cwd=str(cwd),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def ensure_worktree(layout: Layout, base_sha: str, *, may_append: bool) -> Step:
    """Reuse a registered worktree. A leftover directory is removed before add."""
    dest = worktree_dir(layout)
    if registered(layout):
        return Step(True)
    if dest.exists():
        if not _under_run(layout, dest):
            return _fail(layout, "apply-outside-worktree", base_sha, may_append=may_append)
        shutil.rmtree(dest)
    proc = _git(layout.source, "worktree", "add", "--detach", str(dest), base_sha)
    if proc.returncode != 0:
        return _fail(layout, "worktree-create-failed", base_sha, may_append=may_append)
    return Step(True)


def create_worktree(layout: Layout) -> Step:
    """First-run setup. Does not commit or checkout inside the source repo."""
    rows = load_audit(layout.run_dir / "audit.jsonl")
    may_append = not _closed(rows)
    status = human_tree_status(layout)
    if status is not None:
        return _fail(layout, status, None, may_append=may_append)
    base = head_sha(layout.source)
    if base is None:
        return _fail(layout, "resume-missing-base", None, may_append=may_append)
    kind = _git(layout.source, "cat-file", "-t", base)
    if kind.returncode != 0 or kind.stdout.strip() != "commit":
        return _fail(layout, "resume-missing-base", base, may_append=may_append)
    made = ensure_worktree(layout, base, may_append=may_append)
    if not made.ok:
        return made
    status = human_tree_status(layout)
    if status is not None:
        if registered(layout):
            _git(layout.source, "worktree", "remove", "--force", str(worktree_dir(layout)))
        return _fail(layout, status, base, may_append=may_append)
    write_base(layout, base)
    live = (worktree_dir(layout) / layout.relpath).read_bytes()
    shown = _show_bytes(worktree_dir(layout), base, layout.relpath)
    if shown is None or shown != live:
        if registered(layout):
            _git(layout.source, "worktree", "remove", "--force", str(worktree_dir(layout)))
        return _fail(layout, "base-mismatch", base, may_append=may_append)
    return Step(True)


def _allow_file(layout: Layout, relpath: str) -> Path | None:
    if relpath != layout.relpath or ".." in Path(relpath).parts:
        return None
    root = worktree_dir(layout).resolve()
    dest = (root / relpath).resolve()
    if not _under_run(layout, dest) or root not in dest.parents:
        return None
    return dest


def arm_pending(layout: Layout, preimage: bytes, *, cycle_id: int = 1, phase: str = "armed") -> None:
    base = read_base(layout)
    payload = {
        "schema": PENDING_SCHEMA,
        "cycle_id": cycle_id,
        "phase": phase,
        "base_sha": None if base is None else base.get("git_sha"),
        "path": layout.relpath,
        "preimage_sha256": sha256_bytes(preimage),
        "proposal_id": "comment:choose_normal:erate-50",
    }
    atomic_write(layout.run_dir / "pending.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
    atomic_write_bytes(layout.run_dir / "preimage" / f"{cycle_id}.bin", preimage)


def apply_bytes(
    layout: Layout,
    data: bytes,
    *,
    relpath: str | None = None,
    cycle_id: int = 1,
) -> Step:
    """Write one allowlisted file in the worktree. The source checkout is not touched."""
    rel = layout.relpath if relpath is None else relpath
    raw = worktree_dir(layout) / rel
    if raw.is_symlink():
        return Step(False, "symlink-refused")
    dest = _allow_file(layout, rel)
    if dest is None:
        return Step(False, "path-not-allowlisted")
    if not dest.is_file():
        return Step(False, "path-not-allowlisted")
    preimage = dest.read_bytes()
    arm_pending(layout, preimage, cycle_id=cycle_id, phase="armed")
    atomic_write_bytes(dest, data)
    pending = json.loads((layout.run_dir / "pending.json").read_text(encoding="utf-8"))
    pending["phase"] = "applied"
    atomic_write(layout.run_dir / "pending.json", json.dumps(pending, indent=2, sort_keys=True) + "\n")
    return Step(True)


def restore_base(layout: Layout, preimage: bytes) -> Step:
    dest = _allow_file(layout, layout.relpath)
    if dest is None:
        return Step(False, "path-not-allowlisted")
    atomic_write_bytes(dest, preimage)
    base = read_base(layout)
    sha = None if base is None else base.get("git_sha")
    if isinstance(sha, str):
        proc = _git(worktree_dir(layout), "checkout", sha, "--", layout.relpath)
        if proc.returncode != 0:
            return Step(False, "restore-mismatch")
    got = dest.read_bytes()
    if sha256_bytes(got) != sha256_bytes(preimage):
        return Step(False, "restore-mismatch")
    return Step(True)


def _pending(layout: Layout) -> dict | None:
    path = layout.run_dir / "pending.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _preimage_bytes(layout: Layout, pending: dict) -> bytes | None:
    cycle_id = pending.get("cycle_id")
    path = layout.run_dir / "preimage" / f"{cycle_id}.bin"
    if not path.is_file():
        return None
    return path.read_bytes()


def diff_names(layout: Layout) -> list[str] | None:
    base = read_base(layout)
    if base is None:
        return None
    proc = _git(worktree_dir(layout), "diff", "--name-only", str(base["git_sha"]))
    if proc.returncode != 0:
        return None
    return [line for line in proc.stdout.splitlines() if line]


def guard_allowlist(layout: Layout) -> Step:
    """Check 6. A second path is PATH_BROKEN, not a proposal rollback."""
    names = diff_names(layout)
    if names is None:
        return Step(False, "path-not-allowlisted")
    if names == [layout.relpath]:
        return Step(True)
    pending = _pending(layout)
    preimage = None if pending is None else _preimage_bytes(layout, pending)
    pre_sha = None if preimage is None else sha256_bytes(preimage)
    proposal_id = None
    if pending is not None and isinstance(pending.get("proposal_id"), str):
        proposal_id = pending["proposal_id"]
    if preimage is not None:
        restore_base(layout, preimage)
    rows = load_audit(layout.run_dir / "audit.jsonl")
    if not _closed(rows):
        base = read_base(layout)
        _append_broken(
            layout,
            "path-not-allowlisted",
            None if base is None else base.get("git_sha"),
            preimage_sha256=pre_sha,
            proposal_id=proposal_id,
        )
        pending_path = layout.run_dir / "pending.json"
        if pending_path.exists():
            pending_path.unlink()
        return Step(False, "path-not-allowlisted", True)
    return Step(False, "path-not-allowlisted", False)


def already_materialized(layout: Layout, *, base_sha: str, after_sha256: str) -> bool:
    wt = worktree_dir(layout)
    head = head_sha(wt)
    if head is None or head == base_sha:
        return False
    shown = _show_bytes(wt, "HEAD", layout.relpath)
    if shown is None or sha256_bytes(shown) != after_sha256:
        return False
    subject = _git(wt, "log", "-1", "--format=%s")
    if subject.returncode != 0 or subject.stdout.strip() != COMMIT_SUBJECT:
        return False
    names = _git(wt, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
    if names.returncode != 0:
        return False
    changed = [line for line in names.stdout.splitlines() if line]
    return changed == [layout.relpath]


def _attempts(layout: Layout) -> int:
    path = layout.run_dir / "materialize-attempts.json"
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    return int(data.get("attempts") or 0) if isinstance(data, dict) else 0


def _bump_attempts(layout: Layout) -> int:
    count = _attempts(layout) + 1
    atomic_write(
        layout.run_dir / "materialize-attempts.json",
        json.dumps({"attempts": count}) + "\n",
    )
    return count


def _write_materialized(layout: Layout, cycle_id: int) -> None:
    head = head_sha(worktree_dir(layout)) or ""
    atomic_write(
        layout.run_dir / "materialized.json",
        json.dumps({"cycle_id": cycle_id, "worktree_git_sha": head}) + "\n",
    )
    pending = layout.run_dir / "pending.json"
    if pending.exists():
        pending.unlink()


def materialize(layout: Layout, record: dict, *, after_commit=None) -> Step:
    """Commit after a fsynced KEEP, or only mark an existing matching commit.

    after_commit runs only after a new commit succeeds and before the marker.
    The already-materialized path does not call it.
    """
    base_sha = str(record.get("base_git_sha") or "")
    after_sha = str(record.get("after_sha256") or "")
    cycle_id = int(record.get("cycle_id") or 1)
    if already_materialized(layout, base_sha=base_sha, after_sha256=after_sha):
        _write_materialized(layout, cycle_id)
        return Step(True)
    head = head_sha(worktree_dir(layout))
    if head != base_sha:
        return Step(False, "materialize-failed")
    blob = layout.run_dir / "blobs" / after_sha
    dest = _allow_file(layout, layout.relpath)
    if dest is None or not blob.is_file():
        return Step(False, "materialize-failed")
    if _attempts(layout) >= 3:
        return Step(False, "materialize-failed")
    atomic_write_bytes(dest, blob.read_bytes())
    _bump_attempts(layout)
    added = _git(worktree_dir(layout), "add", "--", layout.relpath, identity=True)
    if added.returncode != 0:
        return Step(False, "materialize-failed")
    committed = _git(
        worktree_dir(layout),
        "commit", "--no-verify", "-m", COMMIT_SUBJECT, "--", layout.relpath,
        identity=True,
    )
    if committed.returncode != 0:
        return Step(False, "materialize-failed")
    if not already_materialized(layout, base_sha=base_sha, after_sha256=after_sha):
        return Step(False, "materialize-failed")
    if after_commit is not None:
        after_commit()
    _write_materialized(layout, cycle_id)
    return Step(True)


def _keep_record(records: list[dict]) -> dict | None:
    kept = [rec for rec in records if rec.get("decision") == "KEEP"]
    return kept[-1] if kept else None


def reconcile(layout: Layout) -> Step:
    """Resume rules. A later IDLE does not move a KEEP back to base."""
    rows = load_audit(layout.run_dir / "audit.jsonl")
    may_append = not _closed(rows)
    base_doc = read_base(layout)
    if base_doc is None:
        if registered(layout) and not rows and _pending(layout) is None:
            base = head_sha(layout.source)
            live_head = head_sha(worktree_dir(layout))
            if base and live_head == base and human_tree_status(layout) is None:
                shown = _show_bytes(worktree_dir(layout), "HEAD", layout.relpath)
                live = (worktree_dir(layout) / layout.relpath).read_bytes()
                if shown == live:
                    write_base(layout, base)
                    return Step(True)
        if may_append:
            return _fail(layout, "resume-missing-base", None, may_append=True)
        return Step(False, "resume-missing-base", False)

    base_sha = str(base_doc["git_sha"])
    made = ensure_worktree(layout, base_sha, may_append=may_append)
    if not made.ok:
        return made

    kept = _keep_record(rows)
    if kept is not None:
        after_sha = str(kept.get("after_sha256") or "")
        blob = layout.run_dir / "blobs" / after_sha
        dest = worktree_dir(layout) / layout.relpath
        if blob.is_file() and dest.is_file() and sha256_bytes(dest.read_bytes()) != after_sha:
            atomic_write_bytes(dest, blob.read_bytes())
        return materialize(layout, kept)

    pending = _pending(layout)
    if pending is not None:
        covered = any(rec.get("cycle_id") == pending.get("cycle_id") for rec in rows)
        if not covered:
            preimage = _preimage_bytes(layout, pending)
            if preimage is None:
                return Step(False, "restore-mismatch")
            restored = restore_base(layout, preimage)
            if not restored.ok:
                return restored
            (layout.run_dir / "pending.json").unlink()
            return Step(True)

    return Step(True)
