"""Comment-drift hunk and in-memory oracle. No Aura and no disk apply."""

from __future__ import annotations

import unittest
from pathlib import Path

from harness.catalog import extract_policy_string
from harness.discover import discover
from harness.repo_catalog import (
    NEW_LINE,
    OLD_LINE,
    ProposeOutcome,
    comment_drift_erate50,
    oracle_comment,
    propose_comment,
)
from harness.run import sha256_bytes

ROOT = Path(__file__).resolve().parent.parent
CHOOSE = ROOT / "target" / "aura-redis" / "src" / "redis" / "policy" / "choose_normal.aura"


class CommentDriftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = CHOOSE.read_text(encoding="utf-8")
        self.body, self.line = extract_policy_string(self.source, "normal")

    def test_current_file_matches_line_11_and_keeps_the_body(self) -> None:
        self.assertTrue(comment_drift_erate50(self.source, self.body))
        # extract_policy_string reports the define, not the comment.
        self.assertEqual(self.line, 15)
        proposed = propose_comment(self.source)
        self.assertIsInstance(proposed, str)
        assert isinstance(proposed, str)
        self.assertFalse(comment_drift_erate50(proposed, extract_policy_string(proposed, "normal")[0]))
        self.assertEqual(
            sha256_bytes(extract_policy_string(proposed, "normal")[0].encode("utf-8")),
            sha256_bytes(self.body.encode("utf-8")),
        )
        self.assertTrue(oracle_comment(self.source, proposed))
        hits = [i for i, line in enumerate(self.source.splitlines(), start=1) if line == OLD_LINE]
        self.assertEqual(hits, [11])

    def test_missing_soft_gate_fails_the_oracle(self) -> None:
        stripped = self.source.replace(OLD_LINE, "; soft gate removed")
        self.assertFalse(oracle_comment(self.source, stripped))

    def test_leaving_noop_flat_fails_the_oracle(self) -> None:
        kept = self.source.replace(OLD_LINE, NEW_LINE + " noop|flat")
        self.assertFalse(oracle_comment(self.source, kept))

    def test_a_body_byte_change_fails_the_oracle(self) -> None:
        proposed = propose_comment(self.source)
        assert isinstance(proposed, str)
        body = extract_policy_string(proposed, "normal")[0]
        # The define is an escaped Aura string, so the unescaped body
        # is not a literal substring of the file. Flip one byte inside
        # the quoted define.
        needle = "(< ops 40)"
        self.assertEqual(proposed.count(needle), 1)
        self.assertIn(needle, body)
        changed = proposed.replace(needle, "(< ops 41)", 1)
        changed_body = extract_policy_string(changed, "normal")[0]
        self.assertNotEqual(changed_body, body)
        self.assertFalse(oracle_comment(self.source, changed))

    def test_two_old_lines_are_not_unique(self) -> None:
        doubled = self.source.replace(OLD_LINE, OLD_LINE + "\n" + OLD_LINE, 1)
        self.assertIs(propose_comment(doubled), ProposeOutcome.NOT_UNIQUE)
        self.assertIsNot(propose_comment(doubled), ProposeOutcome.ABSENT)

    def test_crlf_neighbors_stay(self) -> None:
        source = "alpha\r\n" + OLD_LINE + "\r\n" + "beta\r\n"
        # The real file has a define; this sample only checks line endings.
        # propose_comment does not need the define.
        proposed = propose_comment(source)
        self.assertIsInstance(proposed, str)
        assert isinstance(proposed, str)
        self.assertTrue(proposed.startswith("alpha\r\n"))
        self.assertTrue(proposed.endswith("beta\r\n"))
        self.assertIn(NEW_LINE + "\r\n", proposed)
        self.assertNotIn(OLD_LINE, proposed)

    def test_discover_still_does_not_propose(self) -> None:
        issues = discover()
        drifted = [row for row in issues if row["kind"] == "drift"]
        self.assertEqual(
            [row["id"] for row in drifted],
            [
                "contract-drift:test_hot_strategy_policy",
                "comment-drift:choose_normal:erate-50",
                "doc-drift:policy-readme-hot-cold",
            ],
        )
        self.assertTrue(all(row["propose"] is False for row in drifted))


if __name__ == "__main__":
    unittest.main()
