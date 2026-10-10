"""Optional LLM proposer. No network. The default rules path never imports it."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from harness.audit import load_audit
from harness.catalog import call_heads_allowed, grammar_ok, load_catalog
from harness import run as runmod
from harness.proposers import llm as llm_mod

AURA = "/home/dev/code/aura/build/aura"
PREFIX = "(lambda (dgets dsets dhits dmisses devicted nkeys dexpired avg_ttl keys_ttl) "


class HeadTests(unittest.TestCase):
    def test_gold_heads_pass_and_c_func_fails(self) -> None:
        catalog = load_catalog()
        self.assertTrue(call_heads_allowed(catalog.normal))
        self.assertIn("*", catalog.normal)
        bad = PREFIX + "(c-func \"x\"))"
        self.assertFalse(grammar_ok(bad))
        self.assertFalse(call_heads_allowed(bad))
        display = PREFIX + "(string-append \"a\" \"b\"))"
        self.assertTrue(grammar_ok(display))
        self.assertFalse(call_heads_allowed(display))

    def test_bare_quote_does_not_loop(self) -> None:
        from harness.catalog import call_heads
        self.assertEqual(call_heads("(choose 'lfu|flat)"), ["choose"])
        self.assertEqual(call_heads("(choose \\x)"), ["choose"])


class LlmHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.reports = Path(self.tmp.name)
        self._stub = os.environ.pop("AM_LLM_STUB_BODY", None)

    def tearDown(self) -> None:
        if self._stub is None:
            os.environ.pop("AM_LLM_STUB_BODY", None)
        else:
            os.environ["AM_LLM_STUB_BODY"] = self._stub
        self.tmp.cleanup()

    def _fake(self, name: str, source: str) -> str:
        path = Path(self.tmp.name) / name
        path.write_text(source, encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def test_missing_key_is_idle(self) -> None:
        log = Path(self.tmp.name) / "spawns.txt"
        os.environ["AM_SPAWN_LOG"] = str(log)
        aura = self._fake("see.py", """#!/usr/bin/env python3
import os
open(os.environ["AM_SPAWN_LOG"], "a", encoding="utf-8").write("spawned\\n")
print('CYCLE_JSON {"schema":"aura-maintainer.audit.v1","decision":"KEEP","reason":"no","snapshot_id":null}')
""")
        try:
            rc = runmod.main([
                "--aura-bin", aura,
                "--proposer", "llm",
                "--llm-key", str(Path(self.tmp.name) / "missing-key"),
                "--cycles", "1",
                "--reports-dir", str(self.reports),
                "--run-id", "nokey",
                "--timeout-sec", "5",
            ])
        finally:
            os.environ.pop("AM_SPAWN_LOG", None)
        self.assertEqual(rc, 0)
        self.assertFalse(log.exists())
        rows = load_audit(self.reports / "nokey" / "audit.jsonl")
        self.assertEqual(rows[0]["decision"], "IDLE")
        self.assertEqual(rows[0]["reason"], "proposer-unavailable")
        self.assertNotEqual(rows[0]["decision"], "KEEP")
        meta = json.loads((self.reports / "nokey" / "champion.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["generation"], 0)

    def test_reply_without_lambda_does_not_spawn(self) -> None:
        log = Path(self.tmp.name) / "spawns.txt"
        os.environ["AM_SPAWN_LOG"] = str(log)
        os.environ["AM_LLM_STUB_BODY"] = "lambda dgets dsets dhits dmisses: 1"
        aura = self._fake("see.py", """#!/usr/bin/env python3
import os
open(os.environ["AM_SPAWN_LOG"], "a", encoding="utf-8").write("spawned\\n")
print('CYCLE_JSON {"schema":"aura-maintainer.audit.v1","decision":"KEEP","reason":"no","snapshot_id":null}')
""")
        try:
            rc = runmod.main([
                "--aura-bin", aura,
                "--proposer", "llm",
                "--llm-key", str(Path(self.tmp.name) / "missing-key"),
                "--cycles", "1",
                "--reports-dir", str(self.reports),
                "--run-id", "nolambda",
                "--timeout-sec", "5",
            ])
        finally:
            os.environ.pop("AM_SPAWN_LOG", None)
            os.environ.pop("AM_LLM_STUB_BODY", None)
        self.assertEqual(rc, 0)
        self.assertFalse(log.exists())
        rows = load_audit(self.reports / "nolambda" / "audit.jsonl")
        self.assertEqual(rows[0]["decision"], "IDLE")
        self.assertEqual(rows[0]["reason"], "proposer-no-lambda")

    def test_call_head_rejected_without_spawn(self) -> None:
        secret = "sk-test-secret-value"
        key = Path(self.tmp.name) / "key"
        key.write_text(secret + "\n", encoding="utf-8")
        log = Path(self.tmp.name) / "spawns.txt"
        os.environ["AM_SPAWN_LOG"] = str(log)
        os.environ["AM_LLM_STUB_BODY"] = PREFIX + "(string-append \"a\" \"b\"))"
        aura = self._fake("see.py", """#!/usr/bin/env python3
import os
open(os.environ["AM_SPAWN_LOG"], "a", encoding="utf-8").write("spawned\\n")
print('CYCLE_JSON {"schema":"aura-maintainer.audit.v1","decision":"KEEP","reason":"no","snapshot_id":null}')
""")
        before = None
        try:
            rc = runmod.main([
                "--aura-bin", aura,
                "--proposer", "llm",
                "--llm-key", str(key),
                "--cycles", "1",
                "--reports-dir", str(self.reports),
                "--run-id", "heads",
                "--timeout-sec", "5",
            ])
            run_dir = self.reports / "heads"
            before = json.loads((run_dir / "champion.meta.json").read_text(encoding="utf-8"))["body_sha256"]
            blob = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in run_dir.rglob("*") if path.is_file())
        finally:
            os.environ.pop("AM_SPAWN_LOG", None)
        self.assertEqual(rc, 0)
        self.assertFalse(log.exists())
        rows = load_audit(self.reports / "heads" / "audit.jsonl")
        self.assertEqual(rows[0]["reason"], "proposal:call-head")
        self.assertNotEqual(rows[0]["decision"], "KEEP")
        meta = json.loads((self.reports / "heads" / "champion.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["body_sha256"], before)
        self.assertNotIn(secret, blob)


@unittest.skipUnless(Path(AURA).is_file(), "aura binary not built")
class LlmAuraTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.reports = Path(self.tmp.name)
        self._stub = os.environ.pop("AM_LLM_STUB_BODY", None)

    def tearDown(self) -> None:
        if self._stub is None:
            os.environ.pop("AM_LLM_STUB_BODY", None)
        else:
            os.environ["AM_LLM_STUB_BODY"] = self._stub
        self.tmp.cleanup()

    def _run_stub(self, body: str, run_id: str) -> tuple[int, dict, str]:
        os.environ["AM_LLM_STUB_BODY"] = body
        rc = runmod.main([
            "--aura-bin", AURA,
            "--proposer", "llm",
            "--llm-key", str(Path(self.tmp.name) / "missing-key"),
            "--cycles", "1",
            "--mode", "hold",
            "--reports-dir", str(self.reports),
            "--run-id", run_id,
            "--timeout-sec", "40",
        ])
        run_dir = self.reports / run_id
        meta = json.loads((run_dir / "champion.meta.json").read_text(encoding="utf-8"))
        rows = load_audit(run_dir / "audit.jsonl")
        return rc, meta, rows[0]["reason"]

    def test_recover_seed_score_reports_read_got(self) -> None:
        run_dir = self.reports / "score"
        run_dir.mkdir()
        catalog = load_catalog()
        meta = runmod.ensure_seed(run_dir, "recover", catalog)
        rows = runmod.champion_fixture_rows(
            AURA, run_dir, "score", 1, "recover", meta, 40,
            catalog, runmod.load_fixtures(),
        )
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(len(rows), 10)
        self.assertEqual(by_id["read"]["got"], "lru|flat")
        self.assertEqual(by_id["read"]["expect"], "lru|flat")
        self.assertEqual(by_id["w110"]["expect"], "lfu|flat|pin")
        self.assertEqual(by_id["w110"]["got"], "")
        self.assertFalse((run_dir / "audit.jsonl").exists())

    def test_socket_body_is_grammar_rejected(self) -> None:
        rc, meta, reason = self._run_stub(PREFIX + "(socket))", "socket")
        self.assertEqual(rc, 0)
        self.assertEqual(reason, "proposal:grammar")
        self.assertEqual(meta["generation"], 0)
        seed = runmod.sha256_bytes(
            (self.reports / "socket" / "champion.body").read_text(encoding="utf-8").encode("utf-8")
        )
        self.assertEqual(meta["body_sha256"], seed)

    def test_c_func_body_is_rejected(self) -> None:
        rc, meta, reason = self._run_stub(PREFIX + "(c-func \"x\"))", "cfunc")
        self.assertEqual(rc, 0)
        self.assertEqual(reason, "proposal:grammar")
        self.assertNotEqual(reason, "fixture_score")
        self.assertEqual(meta["generation"], 0)


class _Chunks:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._i = 0

    def read(self, n: int) -> bytes:
        chunk = self._data[self._i : self._i + n]
        self._i += len(chunk)
        return chunk


class KeepMetaTests(unittest.TestCase):
    def test_llm_keep_drops_template_params(self) -> None:
        meta = {
            "generation": 1,
            "family": "template",
            "profile": "template",
            "params": {"min-ops": 120, "miss-pin": 55, "soft-budget": 25},
        }
        after, generation = runmod._keep_after(
            meta, {"kind": "llm"}, "a" * 64,
        )
        self.assertEqual(generation, 2)
        self.assertEqual(after["family"], "llm")
        self.assertEqual(after["profile"], "llm")
        self.assertEqual(after["params"], {})
        self.assertEqual(after["body_sha256"], "a" * 64)
        param_after, _gen = runmod._keep_after(
            meta, {"kind": "param", "params": {"min-ops": 80, "miss-pin": 55, "soft-budget": 25}}, "b" * 64,
        )
        self.assertEqual(param_after["family"], "template")
        self.assertEqual(param_after["params"]["min-ops"], 80)


class PromptTests(unittest.TestCase):
    def test_request_names_returns_and_champion(self) -> None:
        champion = '(lambda (dgets dsets dhits dmisses devicted nkeys dexpired avg_ttl keys_ttl) "")'
        text = llm_mod.user_message(champion)
        for item in (
            '""',
            '"lfu|flat|pin"',
            '"lru|flat"',
            '"lru|flat|soft"',
            '"ttl_aware|flat"',
            '"lfu|hot_cold"',
            champion,
        ):
            self.assertIn(item, text)
        self.assertIn("lfu|hot_cold", llm_mod._SYSTEM)
        body, why = llm_mod.propose_body(stub="lambda dgets: 1")
        self.assertIsNone(body)
        self.assertEqual(why, "proposer-no-lambda")
        body, why = llm_mod.propose_body(stub="  ")
        self.assertIsNone(body)
        self.assertEqual(why, "proposer-unavailable")
        body, why = llm_mod.propose_body(stub=champion)
        self.assertEqual(body, champion)
        self.assertEqual(why, "")
        shown = llm_mod.user_message(champion, [{
            "id": "w110",
            "args": [30, 80, 10, 40, 0, 50, 0, 0, 0],
            "expect": "lfu|flat|pin",
            "got": "",
        }])
        self.assertIn("w110", shown)
        self.assertIn("[30, 80, 10, 40, 0, 50, 0, 0, 0]", shown)
        self.assertIn('expect "lfu|flat|pin"', shown)
        self.assertIn('got ""', shown)
        self.assertIn("Leave every row whose got equals expect unchanged.", shown)
        self.assertIn('An expect of "" means that row must return the empty string.', shown)
        self.assertIn("Rows to fix: w110", shown)
        held = llm_mod.user_message(champion, [
            {"id": "read", "args": [500, 10, 480, 20, 0, 50, 0, 0, 0], "expect": "lru|flat", "got": "lru|flat"},
            {"id": "miss40", "args": [40, 50, 10, 30, 0, 40, 0, 0, 0], "expect": "lfu|flat|pin", "got": ""},
        ])
        self.assertIn("Matching rows: read", held)
        self.assertIn("Rows to fix: miss40", held)
        blank = llm_mod.user_message(champion, [{
            "id": "w30",
            "args": [10, 20, 5, 15, 0, 40, 0, 0, 0],
            "expect": "",
            "got": "lfu|flat|pin",
        }])
        self.assertIn("Rows to fix: w30", blank)
        self.assertIn('expect ""', blank)
        self.assertIn('got "lfu|flat|pin"', blank)


class ReadLimitTests(unittest.TestCase):
    def test_short_body_is_kept_and_long_body_is_refused(self) -> None:
        self.assertEqual(llm_mod._read_limited(_Chunks(b"abc"), 3), b"abc")
        self.assertIsNone(llm_mod._read_limited(_Chunks(b"abcd"), 3))


if __name__ == "__main__":
    unittest.main()
