#!/usr/bin/env python3
"""Placeholder metrics helpers for aura-maintainer."""

from pathlib import Path
import json


def summarize_audit(path: Path) -> dict:
    kept = rolled = 0
    if not path.exists():
        return {"kept": 0, "rolled_back": 0, "total": 0}
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        d = rec.get("decision", "")
        if d == "KEEP":
            kept += 1
        elif d == "ROLLBACK":
            rolled += 1
    return {"kept": kept, "rolled_back": rolled, "total": kept + rolled}


if __name__ == "__main__":
    import sys
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if p:
        print(json.dumps(summarize_audit(p), indent=2))
