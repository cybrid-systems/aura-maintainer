"""PR1 clock tests. Fake Aura binaries cover the crash window. No soak."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from harness.audit import (
    CHAMPION_SCHEMA,
    exit_code_for,
    load_audit,
    next_streak,
    synthetic_path_broken,
)
from harness import run as runmod


def _write_fake(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


class StreakTests(unittest.TestCase):
    def test_three_strikes_and_reset(self) -> None:
        streak = 0
        streak = next_streak(streak, {"decision": "PATH_BROKEN", "reason": "crash"})
        streak = next_streak(streak, {"decision": "PATH_BROKEN", "reason": "timeout"})
        self.assertEqual(streak, 2)
        streak = next_streak(streak, {"decision": "ROLLBACK", "reason": "proposal:score-drop"})
        self.assertEqual(streak, 0)
        streak = next_streak(streak, {"decision": "PROBE_FAIL", "reason": "probe"})
        self.assertEqual(streak, 0)
        streak = next_streak(streak, {"decision": "IDLE", "reason": "not-implemented"})
        self.assertEqual(streak, 0)
        streak = next_streak(streak, {"decision": "PATH_BROKEN", "reason": "crash"})
        streak = next_streak(streak, {"decision": "IDLE", "reason": "not-implemented"})
        self.assertEqual(streak, 1)
        streak = next_streak(streak, {"decision": "PATH_BROKEN", "reason": "crash"})
        streak = next_streak(streak, {"decision": "PATH_BROKEN", "reason": "crash"})
        self.assertEqual(streak, 3)

    def test_exit_code_keeps_isolated_broken(self) -> None:
        records = [synthetic_path_broken(run_id="r", cycle_id=1, reason="crash")]
        self.assertEqual(exit_code_for(records, stopped_early=False), 3)
        self.assertEqual(exit_code_for([{"decision": "IDLE"}], stopped_early=False), 0)
        self.assertEqual(exit_code_for([{"decision": "IDLE"}], stopped_early=True), 3)


class ClockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.reports = Path(self.tmp.name)
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        self.spawn_log = Path(self.tmp.name) / "spawns.txt"
        os.environ["AM_SPAWN_LOG"] = str(self.spawn_log)

    def tearDown(self) -> None:
        os.environ.pop("AM_SPAWN_LOG", None)
        self.tmp.cleanup()

    def _fake(self, name: str, source: str) -> str:
        path = self.bin / name
        _write_fake(path, source)
        return str(path)

    def _spawns(self) -> list[str]:
        if not self.spawn_log.exists():
            return []
        return [ln for ln in self.spawn_log.read_text(encoding="utf-8").splitlines() if ln]

    def test_hours_over_cap_writes_nothing(self) -> None:
        rc = runmod.main([
            "--hours", "80",
            "--aura-bin", "/bin/true",
            "--reports-dir", str(self.reports),
            "--run-id", "too-long",
        ])
        self.assertEqual(rc, 2)
        self.assertFalse((self.reports / "too-long").exists())

    def test_missing_binary_writes_no_audit(self) -> None:
        rc = runmod.main([
            "--aura-bin", str(self.reports / "missing-aura"),
            "--cycles", "1",
            "--reports-dir", str(self.reports),
            "--run-id", "no-bin",
        ])
        self.assertEqual(rc, 1)
        self.assertFalse((self.reports / "no-bin").exists())

    def test_three_path_broken_stops_before_fourth_spawn(self) -> None:
        aura = self._fake("broken.py", """#!/usr/bin/env python3
import os
cid = os.environ.get("AM_CYCLE_ID", "0")
open(os.environ["AM_SPAWN_LOG"], "a", encoding="utf-8").write(cid + "\\n")
print('CYCLE_JSON {"schema":"aura-maintainer.audit.v1","decision":"PATH_BROKEN","reason":"locate-miss","snapshot_id":null,"cycle_id":%s}' % cid)
""")
        rc = runmod.main([
            "--aura-bin", aura,
            "--cycles", "4",
            "--mode", "hold",
            "--reports-dir", str(self.reports),
            "--run-id", "strikes",
            "--timeout-sec", "5",
        ])
        self.assertEqual(rc, 3)
        self.assertEqual(self._spawns(), ["1", "2", "3"])
        rows = load_audit(self.reports / "strikes" / "audit.jsonl")
        self.assertEqual([r["decision"] for r in rows], ["PATH_BROKEN"] * 3)

    def test_proposal_rollback_resets_streak(self) -> None:
        aura = self._fake("reset.py", """#!/usr/bin/env python3
import os
cid = os.environ.get("AM_CYCLE_ID", "0")
open(os.environ["AM_SPAWN_LOG"], "a", encoding="utf-8").write(cid + "\\n")
if cid == "2":
    decision, reason = "ROLLBACK", "proposal:score-drop"
else:
    decision, reason = "PATH_BROKEN", "locate-miss"
print('CYCLE_JSON {"schema":"aura-maintainer.audit.v1","decision":"%s","reason":"%s","snapshot_id":null,"cycle_id":%s}' % (decision, reason, cid))
""")
        rc = runmod.main([
            "--aura-bin", aura,
            "--cycles", "5",
            "--reports-dir", str(self.reports),
            "--run-id", "reset",
            "--timeout-sec", "5",
        ])
        self.assertEqual(self._spawns(), ["1", "2", "3", "4", "5"])
        self.assertEqual(rc, 3)
        rows = load_audit(self.reports / "reset" / "audit.jsonl")
        self.assertEqual(rows[1]["decision"], "ROLLBACK")
        self.assertTrue(str(rows[1]["reason"]).startswith("proposal:"))

    def test_timeout_does_not_touch_champion(self) -> None:
        aura = self._fake("sleep.py", """#!/usr/bin/env python3
import os, time
open(os.environ["AM_SPAWN_LOG"], "a", encoding="utf-8").write("slept\\n")
time.sleep(30)
print('CYCLE_JSON {"schema":"aura-maintainer.audit.v1","decision":"KEEP","reason":"too-late","snapshot_id":null}')
""")
        rc = runmod.main([
            "--aura-bin", aura,
            "--cycles", "1",
            "--reports-dir", str(self.reports),
            "--run-id", "slow",
            "--timeout-sec", "1",
        ])
        self.assertEqual(rc, 3)
        meta = json.loads((self.reports / "slow" / "champion.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["schema"], CHAMPION_SCHEMA)
        self.assertEqual(meta["generation"], 0)
        self.assertEqual(meta["params"]["min-ops"], 40)
        rows = load_audit(self.reports / "slow" / "audit.jsonl")
        self.assertEqual(rows[0]["reason"], "timeout")
        self.assertEqual(rows[0]["decision"], "PATH_BROKEN")
        body = (self.reports / "slow" / "champion.body").read_text(encoding="utf-8")
        self.assertIn("(< ops 40)", body)

    def test_resume_does_not_reuse_cycle_id(self) -> None:
        aura = self._fake("idle.py", """#!/usr/bin/env python3
import os
cid = os.environ.get("AM_CYCLE_ID", "0")
open(os.environ["AM_SPAWN_LOG"], "a", encoding="utf-8").write(cid + "\\n")
print('CYCLE_JSON {"schema":"aura-maintainer.audit.v1","decision":"IDLE","reason":"not-implemented","snapshot_id":null,"cycle_id":%s}' % cid)
""")
        rc = runmod.main([
            "--aura-bin", aura,
            "--cycles", "2",
            "--reports-dir", str(self.reports),
            "--run-id", "cont",
            "--timeout-sec", "5",
        ])
        self.assertEqual(rc, 0)
        self.spawn_log.write_text("", encoding="utf-8")
        rc = runmod.main([
            "--aura-bin", aura,
            "--cycles", "1",
            "--resume", "cont",
            "--reports-dir", str(self.reports),
            "--timeout-sec", "5",
        ])
        self.assertEqual(rc, 0)
        rows = load_audit(self.reports / "cont" / "audit.jsonl")
        self.assertEqual([r["cycle_id"] for r in rows], [1, 2, 3])
        self.assertEqual(self._spawns(), ["3"])

    def test_resume_rewrites_champion_from_keep(self) -> None:
        aura = self._fake("see.py", """#!/usr/bin/env python3
import json, os
req = json.load(open(os.environ["AM_REQUEST"], encoding="utf-8"))
open(os.environ["AM_SPAWN_LOG"], "a", encoding="utf-8").write(req["champion_meta"]["body_sha256"] + "\\n")
cid = req["cycle_id"]
print('CYCLE_JSON {"schema":"aura-maintainer.audit.v1","decision":"IDLE","reason":"not-implemented","snapshot_id":null,"cycle_id":%s}' % cid)
""")
        rc = runmod.main([
            "--aura-bin", aura,
            "--cycles", "1",
            "--reports-dir", str(self.reports),
            "--run-id", "keepgap",
            "--timeout-sec", "5",
        ])
        self.assertEqual(rc, 0)
        run_dir = self.reports / "keepgap"
        meta = json.loads((run_dir / "champion.meta.json").read_text(encoding="utf-8"))
        new_body = b"kept-body\n"
        digest = runmod.sha256_bytes(new_body)
        (run_dir / "bodies").mkdir(exist_ok=True)
        (run_dir / "bodies" / f"{digest}.txt").write_bytes(new_body)
        keep = {
            "schema": "aura-maintainer.audit.v1",
            "run_id": "keepgap",
            "cycle_id": 2,
            "decision": "KEEP",
            "reason": "fixture_score 4->7",
            "keep_eligible": True,
            "outcome_class": "improvement",
            "snapshot_id": None,
            "author": "aura-maintainer",
            "after": {
                "family": "template",
                "profile": "template",
                "params": {"min-ops": 80, "miss-pin": 55, "soft-budget": 25},
                "body_sha256": digest,
            },
            "tests": [{"name": "fixture", "baseline_score": 4, "trial_score": 7}],
            "champion_generation": 1,
        }
        with open(run_dir / "audit.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(keep) + "\n")
        self.spawn_log.write_text("", encoding="utf-8")
        rc = runmod.main([
            "--aura-bin", aura,
            "--cycles", "1",
            "--resume", "keepgap",
            "--reports-dir", str(self.reports),
            "--timeout-sec", "5",
        ])
        self.assertEqual(rc, 0)
        rewritten = json.loads((run_dir / "champion.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(rewritten["body_sha256"], digest)
        self.assertEqual(rewritten["generation"], 1)
        self.assertEqual((run_dir / "champion.body").read_bytes(), new_body)
        self.assertEqual(self._spawns(), [digest])
        self.assertNotEqual(digest, meta["body_sha256"])

    def test_resume_missing_body_does_not_spawn(self) -> None:
        aura = self._fake("nope.py", """#!/usr/bin/env python3
import os
open(os.environ["AM_SPAWN_LOG"], "a", encoding="utf-8").write("spawned\\n")
print('CYCLE_JSON {"schema":"aura-maintainer.audit.v1","decision":"IDLE","reason":"not-implemented","snapshot_id":null}')
""")
        rc = runmod.main([
            "--aura-bin", aura,
            "--cycles", "1",
            "--reports-dir", str(self.reports),
            "--run-id", "nobody",
            "--timeout-sec", "5",
        ])
        self.assertEqual(rc, 0)
        run_dir = self.reports / "nobody"
        meta_before = (run_dir / "champion.meta.json").read_text(encoding="utf-8")
        keep = {
            "schema": "aura-maintainer.audit.v1",
            "decision": "KEEP",
            "keep_eligible": True,
            "cycle_id": 2,
            "after": {"body_sha256": "a" * 64, "family": "template", "profile": "template", "params": {}},
            "champion_generation": 1,
        }
        with open(run_dir / "audit.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(keep) + "\n")
        self.spawn_log.write_text("", encoding="utf-8")
        rc = runmod.main([
            "--aura-bin", aura,
            "--cycles", "1",
            "--resume", "nobody",
            "--reports-dir", str(self.reports),
            "--timeout-sec", "5",
        ])
        self.assertEqual(rc, 3)
        self.assertEqual(self._spawns(), [])
        self.assertEqual((run_dir / "champion.meta.json").read_text(encoding="utf-8"), meta_before)
        rows = load_audit(run_dir / "audit.jsonl")
        self.assertEqual(rows[-1]["reason"], "resume-missing-body")
        self.assertEqual(rows[-1]["decision"], "PATH_BROKEN")


class IneligibleKeepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.reports = Path(self.tmp.name)
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_false_keep_does_not_rename_champion(self) -> None:
        aura = self.bin / "keep.py"
        aura.write_text(
            "#!/usr/bin/env python3\n"
            "print('CYCLE_JSON {\"schema\":\"aura-maintainer.audit.v1\","
            "\"decision\":\"KEEP\",\"reason\":\"nope\",\"keep_eligible\":false,"
            "\"snapshot_id\":null,\"tests\":[{\"baseline_score\":1,\"trial_score\":9}]}')\n",
            encoding="utf-8",
        )
        aura.chmod(0o755)
        rc = runmod.main([
            "--aura-bin", str(aura),
            "--cycles", "1",
            "--reports-dir", str(self.reports),
            "--run-id", "badkeep",
            "--timeout-sec", "5",
        ])
        self.assertEqual(rc, 3)
        run_dir = self.reports / "badkeep"
        meta = json.loads((run_dir / "champion.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["generation"], 0)
        self.assertEqual(meta["params"]["min-ops"], 40)
        rows = load_audit(run_dir / "audit.jsonl")
        self.assertEqual(rows[-1]["decision"], "PATH_BROKEN")
        self.assertEqual(rows[-1]["reason"], "kept-ineligible")


class RulesImportTests(unittest.TestCase):
    def test_rules_path_does_not_import_llm(self) -> None:
        import sys
        sys.modules.pop("harness.proposers.llm", None)
        tmp = tempfile.TemporaryDirectory()
        try:
            aura = Path(tmp.name) / "idle.py"
            aura.write_text(
                "#!/usr/bin/env python3\n"
                "print('CYCLE_JSON {\"schema\":\"aura-maintainer.audit.v1\","
                "\"decision\":\"ROLLBACK\",\"reason\":\"proposal:dry-run\",\"snapshot_id\":null}')\n",
                encoding="utf-8",
            )
            aura.chmod(0o755)
            rc = runmod.main([
                "--aura-bin", str(aura),
                "--cycles", "1",
                "--proposer", "rules",
                "--reports-dir", tmp.name,
                "--run-id", "rules",
                "--timeout-sec", "5",
            ])
            self.assertEqual(rc, 0)
            self.assertNotIn("harness.proposers.llm", sys.modules)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
