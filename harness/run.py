#!/usr/bin/env python3
"""
Minimal harness for aura-maintainer v0.

Responsibilities:
- Locate Aura binary
- Load agent/maintainer.aura into a workspace
- Drive cycles (or let the Aura program run its own loop)
- Collect stdout/stderr and write a simple audit/metrics log
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "agent" / "maintainer.aura"
REPORTS = ROOT / "reports"


def find_aura_bin() -> str | None:
    candidates = [
        os.environ.get("AURA_BIN"),
        str(ROOT / ".deps" / "aura" / "build" / "aura"),
        str(ROOT / "target" / "aura-redis" / ".deps" / "aura" / "build" / "aura"),
        "aura",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=5, help="max cycles to request")
    ap.add_argument("--aura-bin", default=None)
    args = ap.parse_args()

    aura = args.aura_bin or find_aura_bin()
    if not aura:
        print("error: aura binary not found. Set AURA_BIN or build Aura.", file=sys.stderr)
        return 1

    REPORTS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = REPORTS / f"run-{stamp}.log"
    audit_path = REPORTS / f"audit-{stamp}.jsonl"

    env = os.environ.copy()
    env.setdefault("AURA_SANDBOX", "off")
    env.setdefault("AURA_PIPELINE_STRICT", "0")

    cmd = [aura, str(AGENT)]
    print(f"running: {' '.join(cmd)}")
    print(f"log: {log_path}")

    with open(log_path, "w") as logf:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            env=env,
            stdout=logf,
            stderr=subprocess.STDOUT,
            text=True,
        )

    # Minimal post-processing: extract AUDIT lines into jsonl
    audits = []
    if log_path.exists():
        for line in log_path.read_text().splitlines():
            if line.startswith("AUDIT "):
                parts = line.split(" ", 2)
                audits.append({
                    "decision": parts[1] if len(parts) > 1 else "",
                    "reason": parts[2] if len(parts) > 2 else "",
                    "raw": line,
                })
    with open(audit_path, "w") as f:
        for a in audits:
            f.write(json.dumps(a) + "\n")

    print(f"exit={proc.returncode} audits={len(audits)}")
    print(f"audit: {audit_path}")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
