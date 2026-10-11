"""Held-out rows are not the KEEP oracle.

The score-12 LLM specimen matches every training row and still disagrees
with choose_normal off that sheet. Matching the ten rows is not evolution.
"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from harness.catalog import load_catalog
from harness.heldout import (
    SPECIMEN_PATH,
    differing_ids,
    got_map,
    load_heldout,
    score_body,
    training_overlap,
)
from harness.run import load_fixtures

AURA = "/home/dev/code/aura/build/aura"
SPECIMEN_SHA = "6cc96483b060874b2f0f1bcb85ac105db22d06a8ee66aa53ce2d3f5012b3f6ab"
EMPTY = (
    "(lambda (dgets dsets dhits dmisses devicted nkeys dexpired avg_ttl keys_ttl) \"\")"
)

# Locked to the pinned binary. These rows are not in windows.json.
NORMAL_HELDOUT = {
    "ops30-write": "",
    "soft-ttl": "ttl_aware|flat|soft",
    "ttl-low-gets": "ttl_aware|flat",
    "erate-read": "lru|flat|soft",
    "miss3-read": "lru|flat",
    "erate16-read": "lfu|flat|pin",
    "ops40-tie": "",
    "miss30-write": "lfu|flat",
}
SPECIMEN_HELDOUT = {
    "ops30-write": "lfu|flat|pin",
    "soft-ttl": "lru|flat|soft",
    "ttl-low-gets": "lfu|flat|pin",
    "erate-read": "lfu|flat",
    "miss3-read": "",
    "erate16-read": "lfu|flat",
    "ops40-tie": "",
    "miss30-write": "lfu|flat",
}


class HeldoutFileTests(unittest.TestCase):
    def test_heldout_is_disjoint_from_the_keep_fixture(self) -> None:
        training = load_fixtures()
        heldout = load_heldout()
        self.assertEqual(training_overlap(heldout, training), [])
        train_ids = {row["id"] for row in training}
        self.assertTrue(train_ids.isdisjoint({row["id"] for row in heldout}))
        self.assertEqual(
            [row["id"] for row in training],
            ["w110", "w50", "w30", "miss40", "miss36", "miss20", "soft22", "soft16", "read", "ttl"],
        )

    def test_specimen_is_the_score12_champion(self) -> None:
        raw = SPECIMEN_PATH.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), SPECIMEN_SHA)
        self.assertEqual(len(raw), 1217)


@unittest.skipUnless(Path(AURA).is_file(), "aura binary not built")
class HeldoutScoreTests(unittest.TestCase):
    def test_choose_normal_is_stable_and_empty_body_differs(self) -> None:
        catalog = load_catalog()
        heldout = load_heldout()
        first = got_map(score_body(AURA, catalog.normal, heldout, catalog=catalog))
        second = got_map(score_body(AURA, catalog.normal, heldout, catalog=catalog))
        self.assertEqual(first, NORMAL_HELDOUT)
        self.assertEqual(second, first)
        empty = got_map(score_body(AURA, EMPTY, heldout, catalog=catalog))
        self.assertNotEqual(differing_ids(first, empty), [])

    def test_score12_specimen_fits_training_and_splits_heldout(self) -> None:
        catalog = load_catalog()
        training = load_fixtures()
        heldout = load_heldout()
        specimen = SPECIMEN_PATH.read_text(encoding="utf-8")
        trained = got_map(score_body(AURA, specimen, training, catalog=catalog))
        for row in training:
            self.assertEqual(trained[row["id"]], row["expect"], row["id"])
        held = got_map(score_body(AURA, specimen, heldout, catalog=catalog))
        self.assertEqual(held, SPECIMEN_HELDOUT)
        self.assertEqual(
            differing_ids(NORMAL_HELDOUT, held),
            ["ops30-write", "soft-ttl", "ttl-low-gets", "erate-read", "miss3-read", "erate16-read"],
        )


if __name__ == "__main__":
    unittest.main()
