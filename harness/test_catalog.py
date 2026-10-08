"""PR2/PR4 catalog tests. They do not start Aura."""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from harness.catalog import (
    LAMBDA_PREFIX,
    extract_policy_string,
    grammar_ok,
    load_catalog,
    seed_body,
    seed_params,
    simulate_ids,
    template,
)
from harness.discover import discover, proposals_from

ROOT = Path(__file__).resolve().parent.parent
RECOVER_PREFIX = [
    "probe:inverted",
    "probe:broken",
    "param:min-ops:120->80",
    "param:min-ops:80->40",
    "param:min-ops:40->20",
    "param:miss-pin:55->30",
    "param:miss-pin:30->15",
    "param:soft-budget:25->20",
    "param:soft-budget:20->15",
    "profile:nosoft",
    "profile:defensive",
    "profile:aggressive",
    "profile:conservative",
]
HOLD_IDS = [
    "probe:inverted",
    "probe:broken",
    "param:min-ops:40->20",
    "param:miss-pin:30->15",
    "param:soft-budget:20->15",
    "profile:nosoft",
    "profile:defensive",
    "profile:aggressive",
    "profile:conservative",
]


class ExtractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = load_catalog(ROOT)

    def test_normal_body_markers(self) -> None:
        body = self.catalog.normal
        self.assertIn(LAMBDA_PREFIX, body)
        self.assertIn("(< ops 40)", body)
        self.assertIn("(>= erate 20)", body)
        self.assertIn("(> miss-pct 30)", body)
        self.assertNotIn("SOFT_BUDGET", body)

    def test_template_gold_matches_file_bytes(self) -> None:
        self.assertEqual(template(self.catalog.normal, 40, 30, 20), self.catalog.normal)
        self.assertEqual(seed_body(self.catalog, "hold"), self.catalog.normal)
        self.assertEqual(seed_params("recover"), {"min-ops": 120, "miss-pin": 55, "soft-budget": 25})

    def test_tampered_pattern_fails(self) -> None:
        bad = self.catalog.normal.replace("(< ops 40)", "(< ops 41)", 1)
        with self.assertRaises(ValueError):
            template(bad, 80, 30, 20)

    def test_extract_ignores_comment_outside_string(self) -> None:
        sample = (
            '; SOFT_BUDGET lives in a comment\n'
            '(define *policy-choose-normal* "(< ops 40) (>= erate 20) (> miss-pct 30)")\n'
        )
        body, line = extract_policy_string(sample, "normal")
        self.assertNotIn("SOFT_BUDGET", body)
        self.assertIn("(< ops 40)", body)
        self.assertEqual(line, 2)


class DiscoverTests(unittest.TestCase):
    def test_drift_issues_are_not_proposals(self) -> None:
        issues = discover(ROOT)
        ids = {row["id"] for row in issues}
        self.assertIn("contract-drift:test_hot_strategy_policy", ids)
        self.assertIn("comment-drift:choose_normal:erate-50", ids)
        self.assertIn("doc-drift:policy-readme-hot-cold", ids)
        proposed = {row["id"] for row in proposals_from(issues)}
        self.assertNotIn("contract-drift:test_hot_strategy_policy", proposed)
        self.assertNotIn("comment-drift:choose_normal:erate-50", proposed)
        self.assertNotIn("doc-drift:policy-readme-hot-cold", proposed)


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = load_catalog(ROOT)

    def test_recover_prefix(self) -> None:
        ids = simulate_ids(self.catalog, "recover")
        self.assertEqual(ids[:len(RECOVER_PREFIX)], RECOVER_PREFIX)
        self.assertNotIn("profile:normal", ids)

    def test_hold_prefix(self) -> None:
        ids = simulate_ids(self.catalog, "hold")
        self.assertEqual(ids, HOLD_IDS)
        self.assertNotIn("param:min-ops:40->40", ids)
        self.assertNotIn("param:min-ops:120->80", ids)

    def test_profile_family_skips_params(self) -> None:
        from harness.catalog import Champion, next_proposal
        body = self.catalog.bodies["aggressive"]
        champion = Champion(
            family="profile",
            profile="aggressive",
            params={"min-ops": 40, "miss-pin": 30, "soft-budget": 20},
            body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            body=body,
        )
        tried = {"probe:inverted|*", "probe:broken|*"}
        seen = []
        for _ in range(12):
            proposal = next_proposal(self.catalog, champion, tried)
            if proposal is None:
                break
            seen.append(proposal["id"])
            tried.add(proposal["id"] + "|*")
            tried.add(proposal["id"] + "|" + champion.body_sha256)
        self.assertTrue(seen)
        self.assertTrue(all(not pid.startswith("param:") for pid in seen))

    def test_nosoft_not_keep_eligible(self) -> None:
        ids = simulate_ids(self.catalog, "hold")
        self.assertIn("profile:nosoft", ids)
        from harness.catalog import Champion, next_proposal
        body = seed_body(self.catalog, "hold")
        champion = Champion(
            family="template",
            profile="template",
            params=seed_params("hold"),
            body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            body=body,
        )
        tried = set()
        found = None
        for _ in range(20):
            proposal = next_proposal(self.catalog, champion, tried)
            self.assertIsNotNone(proposal)
            assert proposal is not None
            tried.add(
                "profile:nosoft|*" if proposal["id"] == "profile:nosoft"
                else proposal["id"] + "|*"
            )
            tried.add(proposal["id"] + "|" + champion.body_sha256)
            if proposal["id"] == "profile:nosoft":
                found = proposal
                break
        self.assertIsNotNone(found)
        assert found is not None
        self.assertFalse(found["keep_eligible"])

    def test_broken_c_func_is_refused(self) -> None:
        broken = self.catalog.bodies["broken"]
        self.assertTrue(grammar_ok(
            broken,
            proposal_id="probe:broken",
            keep_eligible=False,
            broken_body=broken,
        ))
        poisoned = broken[:-1] + " (c-func))"
        self.assertFalse(grammar_ok(
            poisoned,
            proposal_id="probe:broken",
            keep_eligible=False,
            broken_body=broken,
        ))
        self.assertIn("c-func", poisoned)


if __name__ == "__main__":
    unittest.main()
