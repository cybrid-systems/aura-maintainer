"""Crash windows around a repo KEEP. The parent removes the worktree."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness.audit import load_audit
from harness.run import main, sha256_bytes
from harness.worktree import ALLOW_PATH, Layout, create_worktree

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "target" / "aura-redis"
CHOOSE = TARGET / "src" / "redis" / "policy" / "choose_normal.aura"
SUBJECT = "maintainer: clear comment-drift choose_normal erate-50"
RUN_PY = ROOT / "harness" / "run.py"


def _tracked_dirty(porcelain: str) -> bool:
    for line in porcelain.splitlines():
        if len(line) < 4 or line[:2] == "??":
            continue
        if "M" in line[:2] or "D" in line[:2]:
            return True
    return False


def _git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=None if cwd is None else str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout


class RepoResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.reports = Path(self.tmp.name)
        self.head = _git("-C", str(TARGET), "rev-parse", "HEAD").strip()
        self.source_bytes = CHOOSE.read_bytes()
        self.assertNotIn("AM_REPO_CRASH", os.environ)

    def tearDown(self) -> None:
        try:
            self._remove_owned()
            self.assertEqual(_git("-C", str(TARGET), "rev-parse", "HEAD").strip(), self.head)
            self.assertEqual(CHOOSE.read_bytes(), self.source_bytes)
            parent = _git("status", "--porcelain", "--", "target/aura-redis", cwd=ROOT)
            self.assertEqual(parent, "")
            source = _git("-C", str(TARGET), "status", "--porcelain")
            self.assertFalse(_tracked_dirty(source))
        finally:
            self.tmp.cleanup()

    def _remove_owned(self) -> None:
        root = self.reports.resolve()

        def listed() -> list[str]:
            found = []
            for line in _git("-C", str(TARGET), "worktree", "list", "--porcelain").splitlines():
                if not line.startswith("worktree "):
                    continue
                path = Path(line.split(" ", 1)[1])
                try:
                    path.resolve().relative_to(root)
                except ValueError:
                    continue
                found.append(str(path))
            return found

        for path in listed():
            subprocess.run(
                ["git", "-C", str(TARGET), "worktree", "remove", "--force", path],
                check=False,
            )
        self.assertEqual(listed(), [])

    def _child(self, run_id: str, point: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["AM_REPO_CRASH"] = point
        return subprocess.run(
            [
                sys.executable,
                str(RUN_PY),
                "--mode", "repo",
                "--cycles", "1",
                "--reports-dir", str(self.reports),
                "--run-id", run_id,
            ],
            env=env,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )

    def _resume(self, run_id: str) -> int:
        return main([
            "--mode", "repo",
            "--resume", run_id,
            "--reports-dir", str(self.reports),
        ])

    def _worktree(self, run_id: str) -> Path:
        return self.reports / run_id / "worktree"

    def _commit_count(self, run_id: str) -> str:
        return _git("-C", str(self._worktree(run_id)), "rev-list", "--count", "HEAD").strip()

    def _subjects(self, run_id: str) -> list[str]:
        text = _git("-C", str(self._worktree(run_id)), "log", "--format=%s")
        return [line for line in text.splitlines() if line == SUBJECT]

    def test_after_apply_resumes_into_one_keep(self) -> None:
        child = self._child("apply", "after-apply")
        self.assertEqual(child.returncode, 99, child.stderr)
        run_dir = self.reports / "apply"
        rows = load_audit(run_dir / "audit.jsonl")
        self.assertFalse(any(row.get("decision") == "KEEP" for row in rows))
        dirty = (self._worktree("apply") / ALLOW_PATH).read_bytes()
        self.assertNotEqual(sha256_bytes(dirty), sha256_bytes(self.source_bytes))
        rc = self._resume("apply")
        self.assertEqual(rc, 0)
        rows = load_audit(run_dir / "audit.jsonl")
        keeps = [row for row in rows if row["decision"] == "KEEP"]
        self.assertEqual(len(keeps), 1)
        self.assertEqual(keeps[0]["preimage_sha256"], sha256_bytes(self.source_bytes))
        self.assertNotIn("worktree-create-failed", [row.get("reason") for row in rows])
        self.assertNotIn(b"noop|flat", (self._worktree("apply") / ALLOW_PATH).read_bytes())

    def test_after_audit_commits_once(self) -> None:
        child = self._child("audit", "after-audit")
        self.assertEqual(child.returncode, 99, child.stderr)
        run_dir = self.reports / "audit"
        rows = load_audit(run_dir / "audit.jsonl")
        self.assertEqual([row["decision"] for row in rows], ["KEEP"])
        self.assertFalse((run_dir / "materialized.json").exists())
        self.assertEqual(self._subjects("audit"), [])
        rc = self._resume("audit")
        self.assertEqual(rc, 0)
        rows = load_audit(run_dir / "audit.jsonl")
        self.assertEqual(sum(row["decision"] == "KEEP" for row in rows), 1)
        self.assertEqual(self._subjects("audit"), [SUBJECT])
        self.assertEqual(
            _git("-C", str(self._worktree("audit")), "log", "-1", "--format=%s").strip(),
            SUBJECT,
        )
        self.assertTrue((run_dir / "materialized.json").is_file())

    def test_after_commit_only_writes_the_marker(self) -> None:
        child = self._child("commit", "after-commit")
        self.assertEqual(child.returncode, 99, child.stderr)
        run_dir = self.reports / "commit"
        rows = load_audit(run_dir / "audit.jsonl")
        self.assertEqual([row["decision"] for row in rows], ["KEEP"])
        self.assertFalse((run_dir / "materialized.json").exists())
        after = rows[0]["after_sha256"]
        shown = subprocess.run(
            ["git", "-C", str(self._worktree("commit")), "show", f"HEAD:{ALLOW_PATH}"],
            capture_output=True,
            check=False,
        )
        self.assertEqual(shown.returncode, 0)
        self.assertEqual(sha256_bytes(shown.stdout), after)
        count = self._commit_count("commit")
        rc = self._resume("commit")
        self.assertEqual(rc, 0)
        self.assertEqual(self._commit_count("commit"), count)
        self.assertTrue((run_dir / "materialized.json").is_file())
        rows = load_audit(run_dir / "audit.jsonl")
        self.assertEqual(sum(row["decision"] == "KEEP" for row in rows), 1)
        shown = subprocess.run(
            ["git", "-C", str(self._worktree("commit")), "show", f"HEAD:{ALLOW_PATH}"],
            capture_output=True,
            check=False,
        )
        self.assertEqual(sha256_bytes(shown.stdout), after)

    def test_registered_base_resume_keeps_without_create_failure(self) -> None:
        run_dir = self.reports / "fresh"
        run_dir.mkdir()
        made = create_worktree(Layout(source=TARGET, parent=ROOT, run_dir=run_dir))
        self.assertTrue(made.ok, made.reason)
        (run_dir / "environ.json").write_text(
            json.dumps({"mode": "repo", "proposer": "rules"}) + "\n",
            encoding="utf-8",
        )
        rc = self._resume("fresh")
        self.assertEqual(rc, 0)
        rows = load_audit(run_dir / "audit.jsonl")
        self.assertNotIn("worktree-create-failed", [row.get("reason") for row in rows])
        self.assertEqual(sum(row["decision"] == "KEEP" for row in rows), 1)

    def test_keep_and_idle_resume_does_not_add_a_row(self) -> None:
        rc = main([
            "--mode", "repo",
            "--reports-dir", str(self.reports),
            "--run-id", "closed",
        ])
        self.assertEqual(rc, 0)
        run_dir = self.reports / "closed"
        before = load_audit(run_dir / "audit.jsonl")
        self.assertEqual([row["decision"] for row in before], ["KEEP", "IDLE"])
        after_sha = before[0]["after_sha256"]
        live = (self._worktree("closed") / ALLOW_PATH).read_bytes()
        self.assertEqual(sha256_bytes(live), after_sha)
        count = self._commit_count("closed")
        rc = self._resume("closed")
        self.assertEqual(rc, 0)
        after = load_audit(run_dir / "audit.jsonl")
        self.assertEqual([row["decision"] for row in after], ["KEEP", "IDLE"])
        self.assertEqual(len(after), 2)
        self.assertEqual(sha256_bytes((self._worktree("closed") / ALLOW_PATH).read_bytes()), after_sha)
        self.assertEqual(self._commit_count("closed"), count)


if __name__ == "__main__":
    unittest.main()
