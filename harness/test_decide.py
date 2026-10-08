"""Fixture keep/rollback on the real Aura binary."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from harness.audit import load_audit
from harness.catalog import load_catalog
from harness import run as runmod

AURA = "/home/dev/code/aura/build/aura"
TARGET = Path(__file__).resolve().parent.parent / "target" / "aura-redis"


def _gots(rows: object) -> list:
    if not isinstance(rows, list):
        return []
    return [row.get("got") if isinstance(row, dict) else None for row in rows]


@unittest.skipUnless(Path(AURA).is_file(), "aura binary not built")
class DecideTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.reports = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_locate_ok(self) -> None:
        run_dir = self.reports / "loc"
        run_dir.mkdir()
        catalog = load_catalog()
        meta = runmod.ensure_seed(run_dir, "hold", catalog)
        record, log = runmod.spawn_cycle(
            AURA, run_dir, "loc", 1, "hold", meta, 40,
            None, False, catalog, runmod.load_fixtures(),
        )
        self.assertEqual(record.get("decision"), "IDLE", log)
        self.assertEqual(record.get("reason"), "locate-ok", log)
        locator = record.get("locator") or {}
        self.assertEqual(locator.get("status"), "hit", log)
        self.assertIsInstance(locator.get("node_id"), int)

    def test_illegal_champion_does_not_rebind(self) -> None:
        run_dir = self.reports / "bad"
        run_dir.mkdir()
        catalog = load_catalog()
        meta = runmod.ensure_seed(run_dir, "hold", catalog)
        (run_dir / "champion.body").write_text("(define (nope) 1)\n", encoding="utf-8")
        record, log = runmod.spawn_cycle(
            AURA, run_dir, "bad", 1, "hold", meta, 40,
            None, False, catalog, runmod.load_fixtures(),
        )
        self.assertEqual(record.get("decision"), "ROLLBACK", log)
        self.assertEqual(record.get("reason"), "proposal:grammar", log)
        self.assertIsNone(record.get("snapshot_id"))
        self.assertEqual(
            json.loads((run_dir / "champion.meta.json").read_text(encoding="utf-8"))["generation"],
            0,
        )

    def test_dry_run_leaves_the_seed(self) -> None:
        rc = runmod.main([
            "--aura-bin", AURA,
            "--mode", "hold",
            "--cycles", "2",
            "--dry-run",
            "--reports-dir", str(self.reports),
            "--run-id", "dry",
            "--timeout-sec", "40",
        ])
        self.assertEqual(rc, 0)
        rows = load_audit(self.reports / "dry" / "audit.jsonl")
        self.assertEqual([row["reason"] for row in rows], ["proposal:dry-run", "proposal:dry-run"])
        meta = json.loads((self.reports / "dry" / "champion.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["generation"], 0)
        self.assertEqual(meta["params"]["min-ops"], 40)
        self.assertEqual(meta["params"]["miss-pin"], 30)
        self.assertEqual(meta["params"]["soft-budget"], 20)

    def test_hold_keeps_nothing(self) -> None:
        rc = runmod.main([
            "--aura-bin", AURA,
            "--mode", "hold",
            "--cycles", "8",
            "--reports-dir", str(self.reports),
            "--run-id", "hold",
            "--timeout-sec", "40",
        ])
        rows = load_audit(self.reports / "hold" / "audit.jsonl")
        decisions = [row["decision"] for row in rows]
        self.assertEqual(rc, 0, decisions)
        self.assertEqual(decisions.count("KEEP"), 0)
        self.assertEqual(decisions.count("PROBE_OK"), 2)
        self.assertNotIn("PATH_BROKEN", decisions)
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--", "."],
            cwd=str(TARGET),
            capture_output=True,
            text=True,
            check=False,
        )
        tracked = [
            line for line in proc.stdout.splitlines()
            if len(line) >= 2 and line[:2] != "??" and ("M" in line[:2] or "D" in line[:2])
        ]
        self.assertEqual(tracked, [])

    def test_recover_keeps_and_restores(self) -> None:
        rc = runmod.main([
            "--aura-bin", AURA,
            "--mode", "recover",
            "--cycles", "24",
            "--reports-dir", str(self.reports),
            "--run-id", "recover",
            "--timeout-sec", "40",
        ])
        rows = load_audit(self.reports / "recover" / "audit.jsonl")
        keeps = [row for row in rows if row["decision"] == "KEEP"]
        self.assertEqual(rc, 0, [row.get("reason") for row in rows])
        self.assertGreaterEqual(len(keeps), 3)
        scores = []
        for row in keeps:
            trial = row["tests"][0]["trial_score"]
            base = row["tests"][0]["baseline_score"]
            self.assertGreater(trial, base)
            scores.append(trial)
        self.assertEqual(scores, sorted(scores))
        drops = [row for row in rows if row.get("reason") == "proposal:score-drop"]
        self.assertTrue(drops)
        block = drops[0]["tests"][0]
        self.assertEqual(_gots(block.get("restored_rows")), _gots(block.get("baseline_rows")))
        curve = [
            json.loads(line)
            for line in (self.reports / "recover" / "curve.jsonl").read_text(encoding="utf-8").splitlines()
            if line
        ]
        fixture_scores = [row["fixture_score"] for row in curve if row["fixture_score"] is not None]
        self.assertEqual(fixture_scores, sorted(fixture_scores))

    def test_resume_after_first_keep(self) -> None:
        rc = runmod.main([
            "--aura-bin", AURA,
            "--mode", "recover",
            "--cycles", "3",
            "--reports-dir", str(self.reports),
            "--run-id", "resume",
            "--timeout-sec", "40",
        ])
        self.assertEqual(rc, 0)
        rows = load_audit(self.reports / "resume" / "audit.jsonl")
        self.assertEqual(rows[-1]["decision"], "KEEP")
        trial = rows[-1]["tests"][0]["trial_score"]
        rc = runmod.main([
            "--aura-bin", AURA,
            "--mode", "recover",
            "--cycles", "1",
            "--resume", "resume",
            "--reports-dir", str(self.reports),
            "--timeout-sec", "40",
        ])
        self.assertEqual(rc, 0)
        rows = load_audit(self.reports / "resume" / "audit.jsonl")
        self.assertEqual(rows[-1]["tests"][0]["baseline_score"], trial)


if __name__ == "__main__":
    unittest.main()
