"""Audit rows for --mode repo.

Schema aura-maintainer.audit.v2. These constructors are separate from
synthetic_path_broken, which always writes a v1 row.
"""

from __future__ import annotations

import time

AUDIT_SCHEMA = "aura-maintainer.audit.v2"
BASE_SCHEMA = "aura-maintainer.repo-base.v1"
PENDING_SCHEMA = "aura-maintainer.repo-pending.v1"

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


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def repo_record(
    *,
    run_id: str,
    cycle_id: int,
    decision: str,
    reason: str,
    base_git_sha: str | None,
    **extra: object,
) -> dict:
    """One v2 row. base_git_sha may be null, but the key is always present."""
    record: dict = {
        "schema": AUDIT_SCHEMA,
        "run_id": run_id,
        "cycle_id": cycle_id,
        "ts": _now(),
        "mode": "repo",
        "decision": decision,
        "reason": reason,
        "base_git_sha": base_git_sha,
        "author": "aura-maintainer",
        "warnings": [],
    }
    record.update(extra)
    missing = [key for key in _COMMON if key not in record]
    if missing:
        raise ValueError("repo record missing " + ",".join(missing))
    return record


def idle_record(
    *,
    run_id: str,
    cycle_id: int,
    base_git_sha: str | None,
    reason: str = "repo-catalog-empty",
    proposal_id: str | None = None,
) -> dict:
    return repo_record(
        run_id=run_id,
        cycle_id=cycle_id,
        decision="IDLE",
        reason=reason,
        base_git_sha=base_git_sha,
        outcome_class="idle",
        proposal_id=proposal_id,
    )


def rollback_record(
    *,
    run_id: str,
    cycle_id: int,
    reason: str,
    base_git_sha: str | None,
    proposal_id: str,
    path: str,
    preimage_sha256: str | None = None,
    after_sha256: str | None = None,
) -> dict:
    extra: dict = {
        "outcome_class": "rejection",
        "proposal_id": proposal_id,
        "path": path,
    }
    if preimage_sha256 is not None:
        extra["preimage_sha256"] = preimage_sha256
    if after_sha256 is not None:
        extra["after_sha256"] = after_sha256
    return repo_record(
        run_id=run_id,
        cycle_id=cycle_id,
        decision="ROLLBACK",
        reason=reason,
        base_git_sha=base_git_sha,
        **extra,
    )


def path_broken_record(
    *,
    run_id: str,
    cycle_id: int,
    reason: str,
    base_git_sha: str | None,
    proposal_id: str | None = None,
    preimage_sha256: str | None = None,
) -> dict:
    extra: dict = {
        "outcome_class": "path-broken",
        "proposal_id": proposal_id,
    }
    if preimage_sha256 is not None:
        extra["preimage_sha256"] = preimage_sha256
    return repo_record(
        run_id=run_id,
        cycle_id=cycle_id,
        decision="PATH_BROKEN",
        reason=reason,
        base_git_sha=base_git_sha,
        **extra,
    )


def keep_record(
    *,
    run_id: str,
    cycle_id: int,
    base_git_sha: str,
    path: str,
    preimage_sha256: str,
    after_sha256: str,
    body_sha256: str,
    patch: str,
    issue: dict,
    oracle: dict,
    reason: str,
    human_tree_clean: bool,
    proposal_id: str = "comment:choose_normal:erate-50",
    duration_ms: int = 0,
) -> dict:
    return repo_record(
        run_id=run_id,
        cycle_id=cycle_id,
        decision="KEEP",
        reason=reason,
        base_git_sha=base_git_sha,
        duration_ms=duration_ms,
        issue=issue,
        proposal_id=proposal_id,
        outcome_class="improvement",
        keep_eligible=True,
        operator="disk-patch",
        path=path,
        preimage_sha256=preimage_sha256,
        after_sha256=after_sha256,
        body_sha256=body_sha256,
        patch=patch,
        oracle=oracle,
        human_tree_clean=human_tree_clean,
    )
