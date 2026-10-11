"""Fitness headroom, matched-row rollback, install, and a gold-blind replay.

The rules catalog is not the proposer under test. These proposals are a
fixed log: ``replay:empty`` then ``replay:normal``. Neither id is a
``param:`` ladder step, and the prompt under test does not carry expects.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from harness.catalog import load_catalog
from harness.fitness import fitness_score, load_fitness_rows
from harness.heldout import SPECIMEN_PATH, load_heldout, score_body
from harness import run as runmod
from harness.test_heldout import NORMAL_HELDOUT, SPECIMEN_HELDOUT

AURA = "/home/dev/code/aura/build/aura"
PREFIX = "(lambda (dgets dsets dhits dmisses devicted nkeys dexpired avg_ttl keys_ttl)"
EMPTY = PREFIX + ' "")'
MATCHED_BODY = PREFIX + ' (if (> dgets 10) "lru|flat" "lfu|flat"))'


def _gots(rows: object) -> list:
    if not isinstance(rows, list):
        return []
    return [row.get("got") if isinstance(row, dict) else None for row in rows]


def _rows_with(gots: dict[str, str]) -> list[dict]:
    scored = []
    for row in load_heldout():
        scored.append({"id": row["id"], "args": row["args"], "got": gots[row["id"]]})
    return scored


def _plant(run_dir: Path, body: str, *, family: str = "llm") -> tuple[object, dict]:
    catalog = load_catalog()
    meta = runmod.ensure_seed(run_dir, "hold", catalog)
    digest = runmod.sha256_bytes(body.encode("utf-8"))
    (run_dir / "bodies").mkdir(parents=True, exist_ok=True)
    (run_dir / "bodies" / f"{digest}.txt").write_text(body, encoding="utf-8")
    (run_dir / "champion.body").write_text(body, encoding="utf-8")
    meta = {
        "schema": meta["schema"],
        "family": family,
        "profile": family,
        "params": {} if family == "llm" else meta.get("params"),
        "body_sha256": digest,
        "generation": 0,
    }
    (run_dir / "champion.meta.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return catalog, meta


def _proposal(pid: str, body: str) -> dict:
    return {
        "id": pid,
        "kind": "llm",
        "outcome_class": "improvement",
        "keep_eligible": True,
        "body": body,
    }


class PropertyTests(unittest.TestCase):
    def test_training_tie_still_splits_on_fitness(self) -> None:
        self.assertEqual(fitness_score(_rows_with(NORMAL_HELDOUT)), 8)
        self.assertEqual(fitness_score(_rows_with(SPECIMEN_HELDOUT)), 5)
        self.assertEqual(len(load_fitness_rows()), 8)

    def test_python_keeps_a_fitness_rise_and_rejects_a_drop(self) -> None:
        import tempfile

        catalog = load_catalog()
        normal = catalog.normal
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "fit"
            run_dir.mkdir()
            _catalog, meta = _plant(run_dir, SPECIMEN_PATH.read_text(encoding="utf-8"))
            kept = runmod.commit_record(
                run_dir,
                _keep("fitness_score 5->8", 12, 12, 5, 8),
                proposal=_proposal("replay:normal", normal),
                meta=meta,
                tried=set(),
                catalog=catalog,
            )
            self.assertEqual(kept["decision"], "KEEP")
            self.assertEqual(kept["metrics_delta"]["fitness_score"], 3)
            self.assertEqual(kept["metrics_delta"]["fixture_score"], 0)
            written = json.loads((run_dir / "champion.meta.json").read_text(encoding="utf-8"))
            self.assertEqual(written["family"], "llm")
            self.assertEqual(written["params"], {})
            self.assertEqual((run_dir / "champion.body").read_text(encoding="utf-8"), normal)

            run_dir = Path(tmp) / "drop"
            run_dir.mkdir()
            _catalog, meta = _plant(run_dir, normal, family="template")
            dropped = runmod.commit_record(
                run_dir,
                _keep("fixture_score 10->11", 10, 11, 8, 5),
                proposal=_proposal("replay:specimen", SPECIMEN_PATH.read_text(encoding="utf-8")),
                meta=meta,
                tried=set(),
                catalog=catalog,
            )
            self.assertEqual(dropped["decision"], "PATH_BROKEN")
            self.assertEqual(dropped["reason"], "kept-ineligible")
            self.assertEqual(
                json.loads((run_dir / "champion.meta.json").read_text(encoding="utf-8"))["generation"],
                0,
            )

            run_dir = Path(tmp) / "row"
            run_dir.mkdir()
            _catalog, meta = _plant(run_dir, EMPTY)
            changed = runmod.commit_record(
                run_dir,
                _keep(
                    "fixture_score 1->2",
                    1,
                    2,
                    0,
                    0,
                    baseline_rows=[{"id": "keep-me", "got": "", "expect": ""}],
                    trial_rows=[{"id": "keep-me", "got": "lfu|flat", "expect": ""}],
                ),
                proposal=_proposal("replay:matched", MATCHED_BODY),
                meta=meta,
                tried=set(),
                catalog=catalog,
            )
            self.assertEqual(changed["reason"], "kept-ineligible")

            run_dir = Path(tmp) / "fixture"
            run_dir.mkdir()
            _catalog, meta = _plant(run_dir, EMPTY)
            fixture_only = runmod.commit_record(
                run_dir,
                {
                    "schema": "aura-maintainer.audit.v1",
                    "run_id": "py",
                    "cycle_id": 1,
                    "decision": "KEEP",
                    "reason": "fixture_score 4->7",
                    "keep_eligible": True,
                    "snapshot_id": 1,
                    "tests": [{
                        "name": "fixture",
                        "baseline_score": 4,
                        "trial_score": 7,
                        "baseline_rows": [{"id": "read", "got": "lru|flat", "expect": "lru|flat"}],
                        "trial_rows": [{"id": "read", "got": "lru|flat", "expect": "lru|flat"}],
                    }],
                },
                proposal=_proposal("replay:up", normal),
                meta=meta,
                tried=set(),
                catalog=catalog,
            )
            self.assertEqual(fixture_only["decision"], "KEEP")
            self.assertEqual(fixture_only["metrics_delta"]["fixture_score"], 3)


def _keep(
    reason: str,
    base: int,
    trial: int,
    base_fit: int,
    trial_fit: int,
    *,
    baseline_rows: list | None = None,
    trial_rows: list | None = None,
) -> dict:
    if baseline_rows is None:
        baseline_rows = [{"id": "w30", "got": "", "expect": ""}]
    if trial_rows is None:
        trial_rows = [{"id": "w30", "got": "", "expect": ""}]
    return {
        "schema": "aura-maintainer.audit.v1",
        "run_id": "py",
        "cycle_id": 1,
        "decision": "KEEP",
        "reason": reason,
        "keep_eligible": True,
        "snapshot_id": 1,
        "tests": [{
            "name": "fixture",
            "baseline_score": base,
            "trial_score": trial,
            "baseline_fitness": base_fit,
            "trial_fitness": trial_fit,
            "baseline_rows": baseline_rows,
            "trial_rows": trial_rows,
        }],
    }


@unittest.skipUnless(Path(AURA).is_file(), "aura binary not built")
class EvolveAuraTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.reports = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _spawn(self, name: str, body: str, proposal: dict, fixtures: list, fitness_rows):
        run_dir = self.reports / name
        run_dir.mkdir()
        catalog, meta = _plant(run_dir, body)
        record, log = runmod.spawn_cycle(
            AURA, run_dir, name, 1, "hold", meta, 40,
            proposal, False, catalog, fixtures, None, 1, False, fitness_rows,
        )
        return run_dir, catalog, meta, record, log

    def test_matched_row_blocks_a_higher_score(self) -> None:
        fixtures = [
            {"id": "keep-me", "args": [0, 0, 0, 0, 0, 0, 0, 0, 0], "expect": "", "weight": 1},
            {"id": "fix-me", "args": [20, 0, 0, 0, 0, 0, 0, 0, 0], "expect": "lru|flat", "weight": 2},
        ]
        _run, _catalog, _meta, record, log = self._spawn(
            "matched",
            EMPTY,
            _proposal("replay:matched", MATCHED_BODY),
            fixtures,
            [],
        )
        self.assertEqual(record.get("decision"), "ROLLBACK", log)
        self.assertEqual(record.get("reason"), "proposal:matched-row-changed", log)
        self.assertFalse(str(record.get("proposal_id", "")).startswith("param:"))
        block = record["tests"][0]
        self.assertEqual(block["baseline_score"], 1, log)
        self.assertEqual(block["trial_score"], 2, log)
        self.assertEqual(_gots(block.get("restored_rows")), _gots(block.get("baseline_rows")), log)

    def test_training_perfect_body_can_lose_on_fitness(self) -> None:
        catalog = load_catalog()
        specimen = SPECIMEN_PATH.read_text(encoding="utf-8")
        _run, _catalog, _meta, record, log = self._spawn(
            "drop",
            catalog.normal,
            _proposal("replay:specimen", specimen),
            runmod.load_fixtures(),
            None,
        )
        self.assertEqual(record.get("decision"), "ROLLBACK", log)
        self.assertEqual(record.get("reason"), "proposal:fitness-drop", log)
        block = record["tests"][0]
        self.assertEqual(block["baseline_score"], 12, log)
        self.assertEqual(block["trial_score"], 12, log)
        self.assertEqual(block["baseline_fitness"], 8, log)
        self.assertEqual(block["trial_fitness"], 5, log)
        self.assertEqual(_gots(block.get("restored_rows")), _gots(block.get("baseline_rows")), log)

    def test_replay_keeps_fitness_without_the_param_ladder(self) -> None:
        catalog = load_catalog()
        specimen = SPECIMEN_PATH.read_text(encoding="utf-8")
        start_fit = fitness_score(_rows_with(SPECIMEN_HELDOUT))
        run_dir, catalog, meta, empty, log = self._spawn(
            "replay",
            specimen,
            _proposal("replay:empty", EMPTY),
            runmod.load_fixtures(),
            None,
        )
        self.assertEqual(empty.get("decision"), "ROLLBACK", log)
        self.assertEqual(empty.get("proposal_id"), "replay:empty", log)
        self.assertFalse(str(empty.get("proposal_id")).startswith("param:"), log)
        written = runmod.commit_record(
            run_dir, empty, proposal=_proposal("replay:empty", EMPTY),
            meta=meta, tried=set(), catalog=catalog,
        )
        self.assertNotEqual(written.get("decision"), "KEEP")
        self.assertEqual(
            runmod.sha256_bytes((run_dir / "champion.body").read_bytes()),
            runmod.sha256_bytes(specimen.encode("utf-8")),
        )
        meta = json.loads((run_dir / "champion.meta.json").read_text(encoding="utf-8"))
        record, log = runmod.spawn_cycle(
            AURA, run_dir, "replay", 2, "hold", meta, 40,
            _proposal("replay:normal", catalog.normal),
            False, catalog, runmod.load_fixtures(), None, 1, False, None,
        )
        self.assertEqual(record.get("decision"), "KEEP", log)
        self.assertEqual(record.get("reason"), "fitness_score 5->8", log)
        self.assertEqual(record.get("proposal_id"), "replay:normal")
        self.assertFalse(str(record.get("proposal_id")).startswith("param:"))
        block = record["tests"][0]
        self.assertEqual(_gots(block.get("installed_rows")), _gots(block.get("trial_rows")), log)
        kept = runmod.commit_record(
            run_dir, record, proposal=_proposal("replay:normal", catalog.normal),
            meta=meta, tried=set(), catalog=catalog,
        )
        self.assertEqual(kept.get("decision"), "KEEP", log)
        self.assertEqual((run_dir / "champion.body").read_text(encoding="utf-8"), catalog.normal)
        held = score_body(
            AURA,
            (run_dir / "champion.body").read_text(encoding="utf-8"),
            load_heldout(),
            catalog=catalog,
        )
        by_id = {row.get("id"): row.get("got") for row in held}
        scored = [
            {"args": row["args"], "got": by_id[row["id"]]}
            for row in load_heldout()
        ]
        self.assertGreaterEqual(fitness_score(scored), start_fit)


if __name__ == "__main__":
    unittest.main()
