"""Hold and recover stay off the repo apply path. No soak and no MiniMax."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.audit import load_audit
from harness import run as runmod

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = (
    "mutate:rebind",
    "hot-strategy:swap!",
    "minimax",
    "clone --shared",
)


class RepoIsolationTests(unittest.TestCase):
    def test_repo_run_source_stays_off_v1_mutation(self) -> None:
        text = (ROOT / "harness" / "repo_run.py").read_text(encoding="utf-8")
        for needle in FORBIDDEN:
            self.assertNotIn(needle, text)

    def test_hold_parse_and_spawn_stay_on_the_hold_path(self) -> None:
        self.assertEqual(runmod.parse_args([]).mode, "hold")
        hold = runmod.parse_args(["--mode", "hold", "--cycles", "8"])
        self.assertEqual(hold.mode, "hold")
        self.assertEqual(hold.cycles, 8)
        source = (ROOT / "harness" / "run.py").read_text(encoding="utf-8")
        self.assertIn("def spawn_cycle(", source)
        main_src = source.split("def main(", 1)[1]
        fork = main_src.index('if args.mode == "repo"')
        call = main_src.index("spawn_cycle(")
        self.assertGreater(call, fork)

    def test_hold_resume_of_repo_does_not_append(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            reports = Path(tmp.name)
            run_dir = reports / "repo-saved"
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
                "--reports-dir", str(reports),
                "--aura-bin", str(reports / "missing-aura"),
            ])
            self.assertEqual(rc, 2)
            self.assertEqual(audit.read_bytes(), before)
            self.assertEqual(load_audit(audit), [])
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
