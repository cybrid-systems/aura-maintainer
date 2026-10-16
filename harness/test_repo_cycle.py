"""One comment KEEP on the real aura-redis worktree. No Aura and no soak."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from harness.audit import load_audit
from harness.catalog import extract_policy_string
from harness.discover import discover
from harness.repo_catalog import OLD_LINE
from harness.run import main, sha256_bytes
from harness.worktree import ALLOW_PATH, Layout, create_worktree, worktree_dir

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "target" / "aura-redis"
CHOOSE = TARGET / "src" / "redis" / "policy" / "choose_normal.aura"
SUBJECT = "maintainer: clear comment-drift choose_normal erate-50"


def _tracked_dirty(porcelain: str) -> bool:
    for line in porcelain.splitlines():
        if len(line) < 4 or line[:2] == "??":
            continue
        if "M" in line[:2] or "D" in line[:2]:
            return True
    return False


def _git_out(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=None if cwd is None else str(cwd),
        capture_output=True, text=True, check=False,
    )
    return proc.stdout


class RepoCycleTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.reports = Path(self.tmp.name)
        self.head = _git_out("-C", str(TARGET), "rev-parse", "HEAD").strip()
        self.source_bytes = CHOOSE.read_bytes()

    def tearDown(self) -> None:
        try:
            self._remove_owned()
            self.assertEqual(_git_out("-C", str(TARGET), "rev-parse", "HEAD").strip(), self.head)
            self.assertEqual(CHOOSE.read_bytes(), self.source_bytes)
            parent = _git_out("status", "--porcelain", "--", "target/aura-redis", cwd=ROOT)
            self.assertEqual(parent, "")
            source = _git_out("-C", str(TARGET), "status", "--porcelain")
            self.assertFalse(_tracked_dirty(source))
        finally:
            self.tmp.cleanup()

    def _remove_owned(self) -> None:
        root = self.reports.resolve()

        def listed() -> list[str]:
            found = []
            for line in _git_out("-C", str(TARGET), "worktree", "list", "--porcelain").splitlines():
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

    def _run(self, *extra: str) -> int:
        return main(["--mode", "repo", "--reports-dir", str(self.reports), *extra])

    def test_keep_then_idle(self) -> None:
        rc = self._run("--run-id", "keep-one")
        self.assertEqual(rc, 0)
        run_dir = self.reports / "keep-one"
        rows = load_audit(run_dir / "audit.jsonl")
        self.assertEqual([row["decision"] for row in rows], ["KEEP", "IDLE"])
        self.assertEqual(rows[1]["reason"], "repo-catalog-empty")
        self.assertTrue(all(row["schema"] == "aura-maintainer.audit.v2" for row in rows))
        body, _line = extract_policy_string(self.source_bytes.decode("utf-8"), "normal")
        self.assertEqual(rows[0]["body_sha256"], sha256_bytes(body.encode("utf-8")))
        self.assertTrue(rows[0]["human_tree_clean"])
        self.assertEqual(rows[0]["operator"], "disk-patch")
        self.assertNotIn("before", rows[0])
        self.assertNotIn("after", rows[0])
        patch = (run_dir / "patches" / "1.patch").read_text(encoding="utf-8")
        minus = [ln for ln in patch.splitlines() if ln.startswith("-") and not ln.startswith("---")]
        plus = [ln for ln in patch.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
        self.assertEqual(minus, ["-" + OLD_LINE])
        self.assertEqual(len(plus), 1)
        self.assertNotIn("noop|flat", plus[0])
        worktree = run_dir / "worktree"
        self.assertNotIn(b"noop|flat", (worktree / ALLOW_PATH).read_bytes())
        self._apply_check(worktree, run_dir / "patches" / "1.patch")
        subject = _git_out("-C", str(worktree), "log", "-1", "--format=%s").strip()
        self.assertEqual(subject, SUBJECT)
        self.assertTrue((run_dir / "materialized.json").is_file())
        self.assertFalse((run_dir / "champion.body").exists())
        curve = [json.loads(line) for line in (run_dir / "curve.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(curve[0]["oracle"], "comment-drift")
        self.assertEqual(curve[0]["drift_open"], 0)
        self.assertNotIn("fixture_score", curve[0])
        drifted = [row for row in discover() if row["id"] == "comment-drift:choose_normal:erate-50"]
        self.assertEqual(len(drifted), 1)
        self.assertIs(drifted[0]["propose"], False)

    def test_not_unique_is_one_rollback(self) -> None:
        run_dir = self.reports / "not-unique"
        run_dir.mkdir()
        layout = Layout(source=TARGET, parent=ROOT, run_dir=run_dir)
        made = create_worktree(layout)
        self.assertTrue(made.ok, made.reason)
        path = worktree_dir(layout) / ALLOW_PATH
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(OLD_LINE, OLD_LINE + "\n" + OLD_LINE, 1), encoding="utf-8")
        (run_dir / "environ.json").write_text(
            json.dumps({"mode": "repo", "proposer": "rules"}) + "\n",
            encoding="utf-8",
        )
        raw = path.read_bytes()
        rc = self._run("--resume", "not-unique")
        self.assertEqual(rc, 0)
        rows = load_audit(run_dir / "audit.jsonl")
        self.assertEqual([row["decision"] for row in rows], ["ROLLBACK"])
        self.assertEqual(rows[0]["reason"], "proposal:pattern-not-unique")
        tried = json.loads((run_dir / "tried.json").read_text(encoding="utf-8"))
        self.assertIn(f"comment:choose_normal:erate-50|{sha256_bytes(raw)}", tried["keys"])
        self.assertEqual(path.read_bytes(), raw)

    def test_dry_run_then_resume_keeps_once(self) -> None:
        rc = self._run("--dry-run", "--run-id", "dry")
        self.assertEqual(rc, 0)
        run_dir = self.reports / "dry"
        rows = load_audit(run_dir / "audit.jsonl")
        self.assertEqual([row["reason"] for row in rows], ["proposal:dry-run"])
        self.assertFalse((run_dir / "tried.json").exists())
        self.assertEqual((run_dir / "worktree" / ALLOW_PATH).read_bytes(), self.source_bytes)
        rc = self._run("--resume", "dry")
        self.assertEqual(rc, 0)
        rows = load_audit(run_dir / "audit.jsonl")
        self.assertEqual([row["decision"] for row in rows], ["ROLLBACK", "KEEP", "IDLE"])
        self.assertEqual(rows[0]["reason"], "proposal:dry-run")
        self.assertEqual(sum(row["decision"] == "KEEP" for row in rows), 1)
        self.assertNotIn("worktree-create-failed", [row["reason"] for row in rows])
        self.assertNotIn(b"noop|flat", (run_dir / "worktree" / ALLOW_PATH).read_bytes())

    def test_two_path_breaks_exit_3(self) -> None:
        with mock.patch(
            "harness.worktree.diff_names",
            return_value=[ALLOW_PATH, "README.md"],
        ):
            rc = self._run("--run-id", "two-broken")
        self.assertEqual(rc, 3)
        run_dir = self.reports / "two-broken"
        rows = load_audit(run_dir / "audit.jsonl")
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["decision"] for row in rows], ["PATH_BROKEN", "PATH_BROKEN"])
        self.assertEqual(
            [row["reason"] for row in rows],
            ["path-not-allowlisted", "path-not-allowlisted"],
        )
        self.assertFalse((run_dir / "tried.json").exists())
        self.assertFalse((run_dir / "champion.body").exists())

    def _apply_check(self, worktree: Path, patch: Path) -> None:
        check = self.reports / "apply-check"
        added = subprocess.run(
            ["git", "-C", str(TARGET), "worktree", "add", "--detach", str(check), self.head],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        try:
            proc = subprocess.run(
                ["git", "-C", str(check), "apply", "--check", str(patch)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
        finally:
            subprocess.run(
                ["git", "-C", str(TARGET), "worktree", "remove", "--force", str(check)],
                check=False,
            )


if __name__ == "__main__":
    unittest.main()
