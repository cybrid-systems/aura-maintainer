#!/usr/bin/env python3
"""Count decisions and render the demo report for an audit JSONL file."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.audit import load_audit


def summarize_audit(
    path: Path,
    *,
    decision: str | None = None,
    outcome_class: str | None = None,
    proposal_prefix: str | None = None,
) -> dict:
    records = load_audit(path)
    selected = []
    for rec in records:
        if decision and rec.get("decision") != decision:
            continue
        if outcome_class and rec.get("outcome_class") != outcome_class:
            continue
        proposal = rec.get("proposal_id") or ""
        if proposal_prefix and not str(proposal).startswith(proposal_prefix):
            continue
        selected.append(rec)

    counts: dict[str, int] = {}
    for rec in records:
        key = str(rec.get("decision") or "")
        counts[key] = counts.get(key, 0) + 1

    keeps = []
    for rec in records:
        if rec.get("decision") != "KEEP":
            continue
        tests = rec.get("tests") or []
        delta = None
        if tests and isinstance(tests, list) and isinstance(tests[0], dict):
            base = tests[0].get("baseline_score")
            trial = tests[0].get("trial_score")
            if isinstance(base, int) and isinstance(trial, int):
                delta = trial - base
        prov = rec.get("provenance") or {}
        keeps.append({
            "cycle_id": rec.get("cycle_id"),
            "proposal_id": rec.get("proposal_id"),
            "score_delta": delta,
            "mutation_id": prov.get("mutation_id") if isinstance(prov, dict) else None,
        })

    broken = [
        {"cycle_id": rec.get("cycle_id"), "reason": rec.get("reason")}
        for rec in records
        if rec.get("decision") == "PATH_BROKEN"
    ]
    return {
        "total": len(records),
        "matched": len(selected),
        "counts": counts,
        "keeps": keeps,
        "path_broken": broken,
    }


def _score_pair(record: dict) -> tuple[int | None, int | None]:
    tests = record.get("tests") or []
    if not tests or not isinstance(tests, list) or not isinstance(tests[0], dict):
        return None, None
    return _as_int(tests[0].get("baseline_score")), _as_int(tests[0].get("trial_score"))


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def contract_pass_rate(record: dict) -> float | None:
    """Share of trial rows that returned a normal string."""
    tests = record.get("tests") or []
    if not tests or not isinstance(tests, list) or not isinstance(tests[0], dict):
        return None
    rows = tests[0].get("trial_rows") or []
    if not isinstance(rows, list) or not rows:
        return None
    ok = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        got = row.get("got")
        if isinstance(got, str) and got not in ("throw", "non-string"):
            ok += 1
    return ok / len(rows)


def champion_curve(records: list[dict]) -> list[dict]:
    """One curve row per cycle. fixture_score is the champion score.

    It changes only when the decision is KEEP. A rolled-back trial does not
    move it. contract_pass_rate is the trial row, which may be below 1.
    """
    kept = rolled = probe_ok = broken = 0
    score: int | None = None
    rows: list[dict] = []
    for rec in records:
        decision = str(rec.get("decision") or "")
        if decision == "KEEP":
            kept += 1
        elif decision == "ROLLBACK":
            rolled += 1
        elif decision == "PROBE_OK":
            probe_ok += 1
        elif decision == "PATH_BROKEN":
            broken += 1
        base, trial = _score_pair(rec)
        if decision == "KEEP" and trial is not None:
            score = trial
        elif score is None and base is not None:
            score = base
        rate = contract_pass_rate(rec)
        rows.append({
            "cycle_id": rec.get("cycle_id"),
            "ts": rec.get("ts"),
            "decision": decision,
            "outcome_class": rec.get("outcome_class"),
            "kept_total": kept,
            "rolled_total": rolled,
            "probe_ok": probe_ok,
            "path_broken": broken,
            "fixture_score": score,
            "contract_pass_rate": rate,
            "oracle": "fixture",
        })
    return rows


def _row_got(rows: object, row_id: str) -> str | None:
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("id") == row_id:
            got = row.get("got")
            return got if isinstance(got, str) else None
    return None


def _fixture_block(record: dict) -> dict:
    tests = record.get("tests") or []
    if tests and isinstance(tests, list) and isinstance(tests[0], dict):
        return tests[0]
    return {}


def render_report(
    records: list[dict],
    *,
    run_id: str = "unknown",
    mode: str = "unknown",
    aura_sha: str = "",
    wall_ms: int = 0,
    tree_note: str = "not recorded",
) -> str:
    """Demo report. Counts come only from the audit."""
    counts: dict[str, int] = {}
    for rec in records:
        key = str(rec.get("decision") or "")
        counts[key] = counts.get(key, 0) + 1
    curve = champion_curve(records)
    considered = []
    for rec in records:
        decision = rec.get("decision")
        reason = str(rec.get("reason") or "")
        if decision in ("ROLLBACK", "PROBE_OK", "PROBE_FAIL") or reason == "restore-mismatch":
            considered.append(rec)
    clean = [
        rec for rec in considered
        if rec.get("reason") != "restore-mismatch" and rec.get("decision") != "PATH_BROKEN"
    ]
    if considered:
        rate = f"{len(clean)}/{len(considered)}"
    else:
        rate = "n/a"

    lines = [
        f"# aura-maintainer report {run_id}",
        "",
        f"- mode: {mode}",
        f"- cycles: {len(records)}",
        f"- wall_ms: {wall_ms}",
        f"- aura_sha256: {aura_sha}",
        f"- tree: {tree_note}",
        "",
        "## Counts",
        "",
        f"- KEEP: {counts.get('KEEP', 0)}",
        f"- ROLLBACK: {counts.get('ROLLBACK', 0)}",
        f"- PROBE_OK: {counts.get('PROBE_OK', 0)}",
        f"- PROBE_FAIL: {counts.get('PROBE_FAIL', 0)}",
        f"- IDLE: {counts.get('IDLE', 0)}",
        f"- PATH_BROKEN: {counts.get('PATH_BROKEN', 0)}",
        f"- clean_rollback: {rate}",
        "",
        "## Champion fixture score",
        "",
        "| cycle | decision | fixture_score | contract_pass_rate |",
        "| --- | --- | --- | --- |",
    ]
    for row in curve:
        lines.append(
            f"| {row['cycle_id']} | {row['decision']} | {row['fixture_score']} | {row['contract_pass_rate']} |"
        )
    lines.extend(["", "## KEEP", ""])
    keeps = [rec for rec in records if rec.get("decision") == "KEEP"]
    if not keeps:
        lines.append("No KEEP.")
    for rec in keeps:
        _base, trial = _score_pair(rec)
        prov = rec.get("provenance") if isinstance(rec.get("provenance"), dict) else {}
        after = rec.get("after") if isinstance(rec.get("after"), dict) else {}
        delta = rec.get("metrics_delta") if isinstance(rec.get("metrics_delta"), dict) else {}
        lines.append(
            f"- {rec.get('proposal_id')} params={after.get('params')} profile={after.get('profile')} "
            f"score_delta={delta.get('fixture_score')} trial={trial} "
            f"mutation_id={(prov or {}).get('mutation_id')} sha={after.get('body_sha256')}"
        )
    lines.extend(["", "## Probes", ""])
    probes = [rec for rec in records if str(rec.get("proposal_id") or "").startswith("probe:")]
    if not probes:
        lines.append("No probes.")
    for rec in probes:
        block = _fixture_block(rec)
        w110 = _row_got(block.get("trial_rows"), "w110")
        read = _row_got(block.get("trial_rows"), "read")
        base_rows = block.get("baseline_rows")
        restored = block.get("restored_rows")
        same = None
        if isinstance(base_rows, list) and isinstance(restored, list):
            same = [
                (row.get("got") if isinstance(row, dict) else None) for row in base_rows
            ] == [
                (row.get("got") if isinstance(row, dict) else None) for row in restored
            ]
        lines.append(
            f"- {rec.get('proposal_id')} decision={rec.get('decision')} reason={rec.get('reason')} "
            f"w110={w110} read={read} restored_matches_baseline={same}"
        )
    lines.extend(["", "## Rejected proposals", ""])
    rejected = [
        rec for rec in records
        if str(rec.get("reason") or "").startswith("proposal:")
    ]
    if not rejected:
        lines.append("None.")
    for rec in rejected:
        lines.append(f"- cycle {rec.get('cycle_id')}: {rec.get('reason')} ({rec.get('proposal_id')})")
    lines.extend(["", "## Path broken", ""])
    broken_rows = [rec for rec in records if rec.get("decision") == "PATH_BROKEN"]
    if not broken_rows:
        lines.append("None.")
    for rec in broken_rows:
        lines.append(f"- cycle {rec.get('cycle_id')}: {rec.get('reason')}")
    lines.extend([
        "",
        "## Non-claims",
        "",
        "- This run is not a Redis hit-rate result.",
        "- KEEP is the fixture oracle. It is not an LLM score, and the default proposer is rules.",
        "- The C data plane was not edited.",
        "- choose_*.aura was not edited.",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Summarize an audit JSONL file")
    ap.add_argument("audit", type=Path)
    ap.add_argument("--decision", default=None)
    ap.add_argument("--class", dest="outcome_class", default=None)
    ap.add_argument("--proposal", default=None, help="proposal_id prefix")
    ap.add_argument(
        "--report",
        nargs="?",
        const="",
        default=None,
        help="Write report.md next to the audit, or to this path",
    )
    args = ap.parse_args(argv)
    report = summarize_audit(
        args.audit,
        decision=args.decision,
        outcome_class=args.outcome_class,
        proposal_prefix=args.proposal,
    )
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    if args.report is not None:
        records = load_audit(args.audit)
        dest = args.audit.parent / "report.md" if args.report == "" else Path(args.report)
        environ_path = args.audit.parent / "environ.json"
        mode = "unknown"
        aura_sha = ""
        run_id = args.audit.parent.name
        if environ_path.is_file():
            try:
                environ = json.loads(environ_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                environ = {}
            if isinstance(environ, dict):
                mode = str(environ.get("mode") or mode)
                aura_sha = str(environ.get("aura_sha256") or "")
        text = render_report(records, run_id=run_id, mode=mode, aura_sha=aura_sha)
        dest.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
