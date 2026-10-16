"""--mode repo switches. The injected empty registry does not apply.

The CLI default catalog keeps one comment. No soak and no MiniMax.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import run as runmod
from harness.audit import load_audit
from harness.repo_audit import (
    AUDIT_SCHEMA,
    BASE_SCHEMA,
    PENDING_SCHEMA,
    idle_record,
    keep_record,
    path_broken_record,
    rollback_record,
)
from harness.repo_run import main as repo_main

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "target" / "aura-redis"


class RepoModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.reports = Path(self.tmp.name)

    def tearDown(self) -> None:
        try:
            _remove_worktrees_under(self, self.reports)
        finally:
            self.tmp.cleanup()

    def test_parse_defaults(self) -> None:
        hold = runmod.parse_args([])
        self.assertEqual(hold.mode, "hold")
        self.assertIsNone(hold.cycles)
        repo = runmod.parse_args(["--mode", "repo"])
        self.assertEqual(repo.mode, "repo")
        self.assertIsNone(repo.cycles)
        text = _help_text()
        self.assertIn("do not rebind", text)
        self.assertIn("do not write the worktree file", text)

    def test_empty_registry_is_one_idle(self) -> None:
        """Injected empty registry. PR4's real catalog must not turn this red."""
        before = set(sys.modules)
        rc = repo_main(
            runmod.parse_args([
                "--mode", "repo",
                "--reports-dir", str(self.reports),
                "--run-id", "empty-registry",
            ]),
            registry=(),
        )
        self.assertEqual(rc, 0)
        self.assertNotIn("harness.proposers.llm", set(sys.modules) - before)
        run_dir = self.reports / "empty-registry"
        rows = load_audit(run_dir / "audit.jsonl")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["schema"], AUDIT_SCHEMA)
        self.assertEqual(rows[0]["decision"], "IDLE")
        self.assertEqual(rows[0]["reason"], "repo-catalog-empty")
        self.assertEqual(rows[0]["mode"], "repo")
        self.assertIsNone(rows[0]["base_git_sha"])
        self.assertFalse((run_dir / "champion.body").exists())
        self.assertFalse((run_dir / "champion.meta.json").exists())

    def test_cli_omitted_cycles_keeps_then_idles(self) -> None:
        before = set(sys.modules)
        choose = TARGET / "src" / "redis" / "policy" / "choose_normal.aura"
        original = choose.read_bytes()
        rc = runmod.main([
            "--mode", "repo",
            "--reports-dir", str(self.reports),
            "--run-id", "cli-keep",
        ])
        self.assertEqual(rc, 0)
        self.assertNotIn("harness.proposers.llm", set(sys.modules) - before)
        rows = load_audit(self.reports / "cli-keep" / "audit.jsonl")
        self.assertEqual([row["decision"] for row in rows], ["KEEP", "IDLE"])
        self.assertEqual(rows[1]["reason"], "repo-catalog-empty")
        self.assertEqual(choose.read_bytes(), original)

    def test_repo_cycles_zero_exits_2(self) -> None:
        rc = runmod.main([
            "--mode", "repo",
            "--cycles", "0",
            "--hours", "0.01",
            "--reports-dir", str(self.reports),
            "--run-id", "zero-cycles",
        ])
        self.assertEqual(rc, 2)
        self.assertFalse((self.reports / "zero-cycles").exists())

    def test_repo_llm_exits_2_without_import(self) -> None:
        loaded = "harness.proposers.llm" in sys.modules
        rc = runmod.main([
            "--mode", "repo",
            "--proposer", "llm",
            "--reports-dir", str(self.reports),
            "--run-id", "no-llm",
        ])
        self.assertEqual(rc, 2)
        self.assertFalse((self.reports / "no-llm").exists())
        if not loaded:
            self.assertNotIn("harness.proposers.llm", sys.modules)

    def test_hold_resume_of_repo_does_not_append(self) -> None:
        run_dir = self.reports / "repo-saved"
        run_dir.mkdir()
        (run_dir / "environ.json").write_text(
            json.dumps({"mode": "repo", "proposer": "rules"}) + "\n",
            encoding="utf-8",
        )
        audit = run_dir / "audit.jsonl"
        audit.write_text("", encoding="utf-8")
        before = audit.read_bytes()
        rc = runmod.main([
            "--resume", "repo-saved",
            "--mode", "hold",
            "--reports-dir", str(self.reports),
            "--aura-bin", str(self.reports / "missing-aura"),
        ])
        self.assertEqual(rc, 2)
        self.assertEqual(audit.read_bytes(), before)
        self.assertEqual(load_audit(audit), [])
        saved = json.loads((run_dir / "environ.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["mode"], "repo")

    def test_record_constructors_keep_v2_shape(self) -> None:
        self.assertEqual(BASE_SCHEMA, "aura-maintainer.repo-base.v1")
        self.assertEqual(PENDING_SCHEMA, "aura-maintainer.repo-pending.v1")
        idle = idle_record(run_id="r", cycle_id=1, base_git_sha=None)
        self.assertEqual(idle["schema"], "aura-maintainer.audit.v2")
        self.assertNotIn("snapshot_id", idle)
        self.assertNotIn("path", idle)
        rolled = rollback_record(
            run_id="r",
            cycle_id=1,
            reason="proposal:dry-run",
            base_git_sha="abc",
            proposal_id="comment:choose_normal:erate-50",
            path="src/redis/policy/choose_normal.aura",
            preimage_sha256="ff",
        )
        self.assertEqual(rolled["decision"], "ROLLBACK")
        broken = path_broken_record(
            run_id="r", cycle_id=1, reason="tree-dirty", base_git_sha=None,
        )
        self.assertEqual(broken["decision"], "PATH_BROKEN")
        self.assertNotIn("preimage_sha256", broken)
        kept = keep_record(
            run_id="r",
            cycle_id=1,
            base_git_sha="abc",
            path="src/redis/policy/choose_normal.aura",
            preimage_sha256="aa",
            after_sha256="bb",
            body_sha256="cc",
            patch="patches/1.patch",
            issue={"id": "comment-drift:choose_normal:erate-50"},
            oracle={"name": "oracle:comment-drift:erate-50"},
            reason="comment-drift cleared; body unchanged",
            human_tree_clean=True,
        )
        self.assertEqual(kept["operator"], "disk-patch")
        self.assertNotIn("before", kept)
        self.assertNotIn("after", kept)


def _remove_worktrees_under(case: unittest.TestCase, reports: Path) -> None:
    """Drop maintainer worktrees registered under this reports dir. Do not prune."""
    root = reports.resolve()

    def listed() -> list[str]:
        proc = subprocess.run(
            ["git", "-C", str(TARGET), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, check=False,
        )
        found = []
        for line in proc.stdout.splitlines():
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
    case.assertEqual(listed(), [])


def _help_text() -> str:
    from io import StringIO
    buf = StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        with unittest.TestCase().assertRaises(SystemExit):
            runmod.parse_args(["--help"])
    finally:
        sys.stdout = old
    return buf.getvalue()


if __name__ == "__main__":
    unittest.main()
