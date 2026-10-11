"""Property fitness for choose-fn. Not a copy of the training expects.

A row scores when the return string obeys three constraints derived from
the live signals, not from ``choose_normal``'s full answer table:

- ops under 40 must return the empty string
- an eviction rate of at least 20 must return a ``|soft`` kernel
- TTL pressure with eviction rate under 20 must return ``ttl_aware|flat``
  or ``ttl_aware|flat|soft``

The return must also be one of the closed kernel strings. Matching all ten
training rows does not imply a perfect fitness score.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELDOUT_PATH = ROOT / "agent" / "fixtures" / "heldout.json"

ALLOWED = frozenset({
    "",
    "lfu|flat",
    "lfu|flat|pin",
    "lfu|hot_cold",
    "lru|flat",
    "lru|flat|soft",
    "ttl_aware|flat",
    "ttl_aware|flat|soft",
})


def property_ok(args: list, got: str) -> bool:
    if not isinstance(args, list) or len(args) != 9 or not isinstance(got, str):
        return False
    dgets, dsets, _dhits, _dmisses, devicted, nkeys, _dexpired, avg_ttl, keys_ttl = args
    ops = dgets + dsets
    erate = (devicted * 100) // ops if ops else 0
    share = (keys_ttl * 100) // nkeys if nkeys else 0
    if got not in ALLOWED:
        return False
    if ops < 40 and got != "":
        return False
    if erate >= 20 and "|soft" not in got:
        return False
    ttl_pressure = (
        erate < 20
        and keys_ttl > 0
        and avg_ttl < 12000
        and (share >= 20 or keys_ttl > 15)
    )
    if ttl_pressure and got not in ("ttl_aware|flat", "ttl_aware|flat|soft"):
        return False
    return True


def load_fitness_rows() -> list[dict]:
    payload = json.loads(HELDOUT_PATH.read_text(encoding="utf-8"))
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ValueError("fitness rows missing")
    return rows


def fitness_score(rows: list[dict]) -> int:
    total = 0
    for row in rows:
        args = row.get("args")
        got = row.get("got")
        if isinstance(args, list) and isinstance(got, str) and property_ok(args, got):
            total += 1
    return total
