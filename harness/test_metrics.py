"""Report and curve tests. No Aura process."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.metrics import champion_curve, main, render_report


def _keep() -> dict:
    return {
        "schema": "aura-maintainer.audit.v1",
        "cycle_id": 1,
        "decision": "KEEP",
        "reason": "fixture_score 4->7",
        "outcome_class": "improvement",
        "proposal_id": "param:min-ops:120->80",
        "keep_eligible": True,
        "tests": [{
            "name": "fixture",
            "baseline_score": 4,
            "trial_score": 7,
            "trial_rows": [{"id": "w110", "got": "lfu|flat|pin"}],
        }],
        "metrics_delta": {"fixture_score": 3},
        "provenance": {"mutation_id": 4},
        "after": {
            "family": "template",
            "profile": "template",
            "params": {"min-ops": 80, "miss-pin": 55, "soft-budget": 25},
            "body_sha256": "a" * 64,
        },
    }


def _drop() -> dict:
    return {
        "schema": "aura-maintainer.audit.v1",
        "cycle_id": 2,
        "decision": "ROLLBACK",
        "reason": "proposal:score-drop",
        "outcome_class": "improvement",
        "proposal_id": "param:min-ops:40->20",
        "tests": [{
            "name": "fixture",
            "baseline_score": 7,
            "trial_score": 5,
            "trial_rows": [{"id": "w30", "got": "throw"}],
        }],
    }


def _broken() -> dict:
    return {
        "schema": "aura-maintainer.audit.v1",
        "cycle_id": 3,
        "decision": "PATH_BROKEN",
        "reason": "timeout",
        "outcome_class": "path-broken",
    }


class CurveTests(unittest.TestCase):
    def test_score_moves_only_on_keep(self) -> None:
        rows = champion_curve([_keep(), _drop(), _broken()])
        self.assertEqual([row["fixture_score"] for row in rows], [7, 7, 7])
        self.assertEqual(rows[1]["contract_pass_rate"], 0.0)
        self.assertEqual(rows[0]["kept_total"], 1)
        self.assertEqual(rows[1]["rolled_total"], 1)
        self.assertEqual(rows[2]["path_broken"], 1)

    def test_report_splits_reasons(self) -> None:
        text = render_report(
            [_keep(), _drop(), _broken()],
            run_id="demo",
            mode="recover",
            aura_sha="abc",
            wall_ms=30,
            tree_note="clean",
        )
        self.assertIn("KEEP: 1", text)
        self.assertIn("ROLLBACK: 1", text)
        self.assertIn("PATH_BROKEN: 1", text)
        self.assertIn("proposal:score-drop", text)
        self.assertIn("timeout", text)
        rejected = text.split("## Rejected proposals")[1].split("## Path broken")[0]
        broken = text.split("## Path broken")[1].split("## Non-claims")[0]
        self.assertIn("proposal:score-drop", rejected)
        self.assertNotIn("timeout", rejected)
        self.assertIn("timeout", broken)
        self.assertNotIn("proposal:score-drop", broken)
        self.assertIn("not a Redis hit-rate", text)
        self.assertIn("choose_*.aura was not edited", text)

    def test_cli_writes_report(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            audit = Path(tmp.name) / "audit.jsonl"
            with audit.open("w", encoding="utf-8") as fh:
                for row in (_keep(), _drop(), _broken()):
                    fh.write(json.dumps(row) + "\n")
            dest = Path(tmp.name) / "out.md"
            rc = main([str(audit), "--report", str(dest), "--decision", "KEEP"])
            self.assertEqual(rc, 0)
            self.assertIn("param:min-ops:120->80", dest.read_text(encoding="utf-8"))
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
