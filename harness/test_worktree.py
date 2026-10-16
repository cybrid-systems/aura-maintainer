"""Worktree apply and resume. Temp git repos only. No Aura and no soak."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness.audit import append_jsonl, load_audit
from harness.repo_audit import idle_record, keep_record
from harness.run import sha256_bytes
from harness.worktree import (
    COMMIT_SUBJECT,
    Layout,
    already_materialized,
    apply_bytes,
    atomic_write_bytes,
    create_worktree,
    guard_allowlist,
    reconcile,
    worktree_dir,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init(path: Path) -> None:
    path.mkdir(parents=True)
    _git(path, "init")
    _git(path, "config", "user.email", "aura-maintainer@localhost")
    _git(path, "config", "user.name", "aura-maintainer")
    _git(path, "config", "commit.gpgsign", "false")
    _git(path, "config", "core.autocrlf", "false")


class WorktreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.parent = root / "parent"
        self.source = root / "source"
        self.run_dir = root / "reports" / "run-1"
        _init(self.parent)
        (self.parent / "README").write_text("parent\n", encoding="utf-8")
        _git(self.parent, "add", "README")
        _git(self.parent, "commit", "-m", "parent")
        _init(self.source)
        policy = self.source / "src" / "redis" / "policy"
        policy.mkdir(parents=True)
        self.rel = "src/redis/policy/choose_normal.aura"
        self.base_body = b";; base\n(define x \"\")\n"
        (self.source / self.rel).write_bytes(self.base_body)
        (self.source / "other.txt").write_text("other\n", encoding="utf-8")
        _git(self.source, "add", ".")
        _git(self.source, "commit", "-m", "base")
        self.run_dir.mkdir(parents=True)
        self.layout = Layout(self.source, self.parent, self.run_dir)
        self.after = b";; edited\n(define x \"\")\n"

    def tearDown(self) -> None:
        wt = worktree_dir(self.layout)
        if wt.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt)],
                cwd=str(self.source),
                capture_output=True,
                check=False,
            )
        self.tmp.cleanup()

    def _source_bytes(self) -> bytes:
        return (self.source / self.rel).read_bytes()

    def _source_porcelain(self) -> str:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--", "."],
            cwd=str(self.source),
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout

    def _commit_count(self) -> int:
        proc = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(worktree_dir(self.layout)),
            capture_output=True,
            text=True,
            check=True,
        )
        return int(proc.stdout.strip())

    def _write_keep(self) -> None:
        after_sha = sha256_bytes(self.after)
        atomic_write_bytes(self.run_dir / "blobs" / after_sha, self.after)
        base = json.loads((self.run_dir / "base.json").read_text(encoding="utf-8"))["git_sha"]
        append_jsonl(
            self.run_dir / "audit.jsonl",
            keep_record(
                run_id="run-1",
                cycle_id=1,
                base_git_sha=base,
                path=self.rel,
                preimage_sha256=sha256_bytes(self.base_body),
                after_sha256=after_sha,
                body_sha256="body",
                patch="patches/1.patch",
                issue={"id": "comment-drift:choose_normal:erate-50"},
                oracle={"name": "oracle:comment-drift:erate-50"},
                reason="comment-drift cleared; body unchanged",
                human_tree_clean=True,
            ),
        )

    def test_apply_leaves_the_source_checkout_clean(self) -> None:
        self.assertTrue(create_worktree(self.layout).ok)
        before = self._source_bytes()
        self.assertTrue(apply_bytes(self.layout, self.after).ok)
        self.assertEqual(self._source_bytes(), before)
        self.assertEqual(self._source_porcelain(), "")
        live = (worktree_dir(self.layout) / self.rel).read_bytes()
        self.assertEqual(live, self.after)

    def test_pending_without_audit_restores_base(self) -> None:
        self.assertTrue(create_worktree(self.layout).ok)
        self.assertTrue(apply_bytes(self.layout, self.after).ok)
        step = reconcile(self.layout)
        self.assertTrue(step.ok, step.reason)
        live = (worktree_dir(self.layout) / self.rel).read_bytes()
        self.assertEqual(live, self.base_body)
        self.assertEqual(load_audit(self.run_dir / "audit.jsonl"), [])
        self.assertFalse((self.run_dir / "pending.json").exists())

    def test_keep_materializes_once(self) -> None:
        self.assertTrue(create_worktree(self.layout).ok)
        self.assertTrue(apply_bytes(self.layout, self.after).ok)
        self._write_keep()
        first = reconcile(self.layout)
        self.assertTrue(first.ok, first.reason)
        count = self._commit_count()
        subject = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=str(worktree_dir(self.layout)),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(subject, COMMIT_SUBJECT)
        base = json.loads((self.run_dir / "base.json").read_text(encoding="utf-8"))["git_sha"]
        self.assertTrue(already_materialized(
            self.layout,
            base_sha=base,
            after_sha256=sha256_bytes(self.after),
        ))
        second = reconcile(self.layout)
        self.assertTrue(second.ok, second.reason)
        self.assertEqual(self._commit_count(), count)
        live = (worktree_dir(self.layout) / self.rel).read_bytes()
        self.assertEqual(sha256_bytes(live), sha256_bytes(self.after))

    def test_registered_clean_tree_can_apply_again(self) -> None:
        self.assertTrue(create_worktree(self.layout).ok)
        step = reconcile(self.layout)
        self.assertTrue(step.ok, step.reason)
        self.assertNotEqual(step.reason, "worktree-create-failed")
        text = (self.run_dir / "audit.jsonl").read_text(encoding="utf-8") if (self.run_dir / "audit.jsonl").exists() else ""
        self.assertNotIn("worktree-create-failed", text)
        self.assertTrue(apply_bytes(self.layout, self.after).ok)

    def test_unregistered_leftover_is_replaced(self) -> None:
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(self.source),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        leftover = self.run_dir / "worktree"
        leftover.mkdir()
        (leftover / "junk").write_text("nope\n", encoding="utf-8")
        (self.run_dir / "base.json").write_text(json.dumps({
            "schema": "aura-maintainer.repo-base.v1",
            "git_sha": base,
            "worktree": str(leftover.resolve()),
            "human_checkout": "target/aura-redis",
            "path": self.rel,
        }), encoding="utf-8")
        step = reconcile(self.layout)
        self.assertTrue(step.ok, step.reason)
        text = (self.run_dir / "audit.jsonl").read_text(encoding="utf-8") if (self.run_dir / "audit.jsonl").exists() else ""
        self.assertNotIn("worktree-create-failed", text)
        self.assertTrue((worktree_dir(self.layout) / self.rel).is_file())
        self.assertFalse((worktree_dir(self.layout) / "junk").exists())

    def test_second_path_is_path_broken_and_not_tried(self) -> None:
        self.assertTrue(create_worktree(self.layout).ok)
        self.assertTrue(apply_bytes(self.layout, self.after).ok)
        other = worktree_dir(self.layout) / "other.txt"
        other.write_text("changed\n", encoding="utf-8")
        step = guard_allowlist(self.layout)
        self.assertFalse(step.ok)
        self.assertEqual(step.reason, "path-not-allowlisted")
        live = (worktree_dir(self.layout) / self.rel).read_bytes()
        self.assertEqual(live, self.base_body)
        rows = load_audit(self.run_dir / "audit.jsonl")
        self.assertEqual([rec["decision"] for rec in rows], ["PATH_BROKEN"])
        self.assertEqual(rows[0]["reason"], "path-not-allowlisted")
        self.assertFalse(any(rec["decision"] == "KEEP" for rec in rows))
        tried = self.run_dir / "tried.json"
        self.assertFalse(tried.exists())

    def test_idle_after_keep_does_not_restore_base(self) -> None:
        self.assertTrue(create_worktree(self.layout).ok)
        self.assertTrue(apply_bytes(self.layout, self.after).ok)
        self._write_keep()
        self.assertTrue(reconcile(self.layout).ok)
        count = self._commit_count()
        append_jsonl(
            self.run_dir / "audit.jsonl",
            idle_record(run_id="run-1", cycle_id=2, base_git_sha=None),
        )
        again = reconcile(self.layout)
        self.assertTrue(again.ok, again.reason)
        self.assertEqual(self._commit_count(), count)
        live = (worktree_dir(self.layout) / self.rel).read_bytes()
        self.assertEqual(sha256_bytes(live), sha256_bytes(self.after))
        self.assertEqual(len(load_audit(self.run_dir / "audit.jsonl")), 2)

    def test_crlf_preimage_sha_roundtrips(self) -> None:
        payload = b"line\r\nline\r\n"
        path = self.run_dir / "preimage" / "crlf.bin"
        atomic_write_bytes(path, payload)
        self.assertEqual(sha256_bytes(path.read_bytes()), sha256_bytes(payload))
        policy = self.source / self.rel
        policy.write_bytes(payload)
        _git(self.source, "add", self.rel)
        _git(self.source, "commit", "-m", "crlf")
        self.base_body = payload
        self.assertTrue(create_worktree(self.layout).ok)
        original = (worktree_dir(self.layout) / self.rel).read_bytes()
        self.assertEqual(sha256_bytes(original), sha256_bytes(payload))
        self.assertTrue(apply_bytes(self.layout, b"changed\r\n").ok)
        from harness.worktree import restore_base
        step = restore_base(self.layout, original)
        self.assertTrue(step.ok, step.reason)
        restored = (worktree_dir(self.layout) / self.rel).read_bytes()
        self.assertEqual(sha256_bytes(restored), sha256_bytes(payload))

    def test_commit_without_marker_is_not_repeated(self) -> None:
        self.assertTrue(create_worktree(self.layout).ok)
        self.assertTrue(apply_bytes(self.layout, self.after).ok)
        self._write_keep()
        self.assertTrue(reconcile(self.layout).ok)
        count = self._commit_count()
        (self.run_dir / "materialized.json").unlink()
        again = reconcile(self.layout)
        self.assertTrue(again.ok, again.reason)
        self.assertEqual(self._commit_count(), count)
        self.assertTrue((self.run_dir / "materialized.json").is_file())

    def test_outside_path_does_not_change_the_source(self) -> None:
        self.assertTrue(create_worktree(self.layout).ok)
        before = self._source_bytes()
        step = apply_bytes(self.layout, b"nope", relpath="../outside.txt")
        self.assertFalse(step.ok)
        self.assertEqual(self._source_bytes(), before)

    def test_status_failure_is_not_clean(self) -> None:
        from harness.worktree import human_tree_status
        broken = Layout(self.source, self.run_dir / "not-a-repo", self.run_dir)
        self.assertEqual(human_tree_status(broken), "tree-status-failed")


if __name__ == "__main__":
    unittest.main()
