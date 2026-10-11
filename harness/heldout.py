"""Score choose-fn bodies on rows the KEEP gate does not see.

The training fixture remains agent/fixtures/windows.json. This module
only compares return strings. It does not decide KEEP.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from harness.catalog import Catalog, load_catalog
from harness import run as runmod

ROOT = Path(__file__).resolve().parent.parent
HELDOUT_PATH = ROOT / "agent" / "fixtures" / "heldout.json"
SPECIMEN_PATH = ROOT / "agent" / "fixtures" / "specimens" / "llm-score12.txt"


def load_heldout() -> list[dict]:
    payload = json.loads(HELDOUT_PATH.read_text(encoding="utf-8"))
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("held-out rows missing")
    return rows


def args_key(row: dict) -> tuple:
    args = row.get("args")
    if not isinstance(args, list):
        raise ValueError(f"held-out row {row.get('id')} has no args")
    return tuple(args)


def training_overlap(heldout: list[dict], training: list[dict]) -> list[str]:
    """Ids or arg lists that appear in both sets."""
    train_ids = {row.get("id") for row in training}
    train_args = {args_key(row) for row in training}
    overlap: list[str] = []
    for row in heldout:
        row_id = row.get("id")
        if row_id in train_ids or args_key(row) in train_args:
            overlap.append(str(row_id))
    return overlap


def score_body(
    aura: str,
    body: str,
    rows: list[dict],
    *,
    timeout_sec: float = 40,
    catalog: Catalog | None = None,
) -> list[dict]:
    """Return the tally rows for one body. One Aura process, no audit write."""
    catalog = catalog or load_catalog()
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / "score"
        run_dir.mkdir()
        meta = runmod.ensure_seed(run_dir, "hold", catalog)
        (run_dir / "champion.body").write_text(body, encoding="utf-8")
        record, log = runmod.spawn_cycle(
            aura, run_dir, "heldout", 1, "hold", meta, timeout_sec,
            None, False, catalog, rows, None, 1, True,
        )
    if record.get("reason") != "score-champion":
        raise RuntimeError(f"score failed: {record.get('decision')} {record.get('reason')}\n{log}")
    tests = record.get("tests")
    if not isinstance(tests, list) or not tests or not isinstance(tests[0], dict):
        raise RuntimeError(f"score record has no rows\n{log}")
    got_rows = tests[0].get("baseline_rows")
    if not isinstance(got_rows, list):
        raise RuntimeError(f"score record has no baseline_rows\n{log}")
    return got_rows


def got_map(rows: list[dict]) -> dict[str, str]:
    return {str(row.get("id")): str(row.get("got")) for row in rows}


def differing_ids(left: dict[str, str], right: dict[str, str]) -> list[str]:
    return [row_id for row_id in left if left[row_id] != right.get(row_id)]
