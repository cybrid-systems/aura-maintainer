"""Closed-set comment edit. propose stays false on the v1 discover path.

This module does not write files and does not call replace_once or mark_tried.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from harness.catalog import extract_policy_string
from harness.run import sha256_bytes

OLD_LINE = (
    ";   Prefer ttl_aware when TTL pressure, else lru|flat; "
    "if rate ≥ 50 → noop|flat"
)
NEW_LINE = (
    ";   Prefer ttl_aware|flat|soft when TTL pressure, else "
    "lru|flat|soft. The soft gate is (>= erate 20); "
    "the body has no rate-50 branch."
)
PROPOSAL_ID = "comment:choose_normal:erate-50"
ISSUE_ID = "comment-drift:choose_normal:erate-50"
ALLOW_PATH = "src/redis/policy/choose_normal.aura"


class ProposeOutcome(enum.Enum):
    ABSENT = "absent"
    NOT_UNIQUE = "not-unique"


def comment_drift_erate50(source: str, body: str) -> bool:
    return (
        "noop|flat" in source
        and "noop|flat" not in body
        and "(>= erate 50)" not in body
    )


def propose_comment(source: str) -> str | ProposeOutcome:
    """One line swap. ABSENT, NOT_UNIQUE, or the full new file text."""
    lines = source.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == OLD_LINE]
    if not hits:
        return ProposeOutcome.ABSENT
    if len(hits) != 1:
        return ProposeOutcome.NOT_UNIQUE
    i = hits[0]
    ending = "\r\n" if lines[i].endswith("\r\n") else ("\n" if lines[i].endswith("\n") else "")
    lines[i] = NEW_LINE + ending
    return "".join(lines)


def _body(source: str) -> str:
    return extract_policy_string(source, "normal")[0]


def oracle_comment(before: str, after: str) -> bool:
    """Checks 1–5. Check 6 is a git diff and is not decided here."""
    try:
        before_body = _body(before)
        after_body = _body(after)
    except ValueError:
        return False
    if not comment_drift_erate50(before, before_body):
        return False
    if comment_drift_erate50(after, after_body):
        return False
    if sha256_bytes(before_body.encode("utf-8")) != sha256_bytes(after_body.encode("utf-8")):
        return False
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    if len(before_lines) != len(after_lines):
        return False
    changed = [
        (old, new)
        for old, new in zip(before_lines, after_lines)
        if old != new
    ]
    if len(changed) != 1:
        return False
    old, new = changed[0]
    if old.rstrip("\r\n") != OLD_LINE or new.rstrip("\r\n") != NEW_LINE:
        return False
    if "noop|flat" in after:
        return False
    if "ttl_aware|flat|soft" not in NEW_LINE:
        return False
    if "lru|flat|soft" not in NEW_LINE or "(>= erate 20)" not in NEW_LINE:
        return False
    if "ttl_aware|flat|soft" not in after or "lru|flat|soft" not in after:
        return False
    if "(>= erate 20)" not in after:
        return False
    return True


@dataclass(frozen=True)
class RepoClass:
    issue_id: str
    proposal_id: str
    path: str

    def propose(self, source: str) -> str | ProposeOutcome:
        return propose_comment(source)

    def oracle(self, before: str, after: str) -> bool:
        return oracle_comment(before, after)


COMMENT_CLASS = RepoClass(ISSUE_ID, PROPOSAL_ID, ALLOW_PATH)
