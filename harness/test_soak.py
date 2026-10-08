"""Short soak. A fake Aura drains the hold catalog, then the clock idles."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from harness.audit import load_audit
from harness import run as runmod


class SoakTests(unittest.TestCase):
    def test_idle_heartbeats_outnumber_spawns(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            reports = Path(tmp.name)
            log = reports / "spawns.txt"
            os.environ["AM_SPAWN_LOG"] = str(log)
            aura = reports / "fast.py"
            aura.write_text(
                "#!/usr/bin/env python3\n"
                "import os\n"
                "open(os.environ['AM_SPAWN_LOG'], 'a', encoding='utf-8').write("
                "os.environ.get('AM_CYCLE_ID', '?') + '\\n')\n"
                "print('CYCLE_JSON {\"schema\":\"aura-maintainer.audit.v1\","
                "\"decision\":\"ROLLBACK\",\"reason\":\"proposal:score-drop\",\"snapshot_id\":null}')\n",
                encoding="utf-8",
            )
            aura.chmod(0o755)
            rc = runmod.main([
                "--aura-bin", str(aura),
                "--mode", "hold",
                "--hours", "0.02",
                "--cycles", "0",
                "--reports-dir", str(reports),
                "--run-id", "soak",
                "--timeout-sec", "5",
            ])
            self.assertEqual(rc, 0)
            spawns = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln]
            beat = json.loads((reports / "soak" / "heartbeat.json").read_text(encoding="utf-8"))
            rows = load_audit(reports / "soak" / "audit.jsonl")
            self.assertTrue(spawns)
            self.assertGreater(beat["writes"], len(spawns) * 4)
            self.assertEqual(beat["phase"], "idle")
            self.assertFalse(any(row.get("decision") == "PATH_BROKEN" for row in rows))
        finally:
            os.environ.pop("AM_SPAWN_LOG", None)
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
