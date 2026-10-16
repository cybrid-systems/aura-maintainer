"""--mode repo entry. PR1 writes one idle line and does not apply a patch.

The default registry is empty. Later PRs pass the comment-drift class.
This module does not import the LLM proposer and does not spawn Aura.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

from harness.audit import append_jsonl, atomic_write, load_audit
from harness.repo_audit import idle_record


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(args, registry: tuple = ()) -> int:
    """Run repo mode. An empty registry fsyncs one IDLE and returns 0."""
    reports = args.reports_dir
    reports.mkdir(parents=True, exist_ok=True)
    if args.resume:
        run_id = args.resume
        run_dir = reports / run_id
        if not run_dir.is_dir():
            print(f"error: resume directory not found: {run_dir}", file=sys.stderr)
            return 2
    else:
        run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        run_dir = reports / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

    aura_bin = None
    aura_sha = None
    if args.aura_bin and Path(args.aura_bin).is_file():
        aura_bin = str(Path(args.aura_bin).resolve())
        aura_sha = _sha256_file(Path(aura_bin))
    environ = {
        "aura_bin": aura_bin,
        "aura_sha256": aura_sha,
        "AURA_SANDBOX": "off",
        "AURA_PIPELINE_STRICT": "0",
        "mode": "repo",
        "dry_run": bool(args.dry_run),
        "proposer": args.proposer,
        "base_git_sha": None,
    }
    atomic_write(run_dir / "environ.json", json.dumps(environ, indent=2, sort_keys=True) + "\n")

    if registry:
        print("error: repo apply is not implemented", file=sys.stderr)
        return 2

    records = load_audit(run_dir / "audit.jsonl")
    if records:
        print(f"run={run_id} exit=0 cycles_spawned=0 audit={run_dir / 'audit.jsonl'}")
        return 0

    record = idle_record(
        run_id=run_id,
        cycle_id=1,
        base_git_sha=None,
        reason="repo-catalog-empty",
    )
    append_jsonl(run_dir / "audit.jsonl", record)
    print(f"run={run_id} exit=0 cycles_spawned=0 audit={run_dir / 'audit.jsonl'}")
    return 0
