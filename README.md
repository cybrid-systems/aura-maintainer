# Aura Maintainer

Long-horizon autonomous code maintenance agent built on [Aura](https://github.com/cybrid-systems/aura).

**Goal**: Demonstrate that an agent can continuously find issues, surgically mutate real code, run tests, and either KEEP or cleanly ROLLBACK changes — with full audit trail — over 24–72+ hours.

This is the highest-signal demo for Aura's unique capabilities (runtime `query:*` / `mutate:*` / `ast:snapshot` / incremental compilation + provenance).

## Why this exists

Current agent frameworks struggle with:
- Long-horizon maintenance (days, not minutes)
- Structured, surgical code changes (not whole-file rewrites)
- Reliable rollback when a change fails tests
- Complete, queryable audit trail of every mutation and reason

Aura's language-level primitives make these first-class.

## Status

Early scaffolding. Target: a reproducible 24–72h run against a real mid-sized open-source codebase with measurable outcomes.

## Planned structure

```
/
├── target/          # The codebase under maintenance (submodule or vendored)
├── agent/           # Aura agent logic (discovery → propose → mutate → test → keep/rollback)
├── harness/         # Runner, metrics collection, audit log
├── reports/         # Generated run reports (success rate, rollbacks, quality curves)
└── docs/
    └── design.md     # Architecture and success criteria
```

## Success criteria (v1)

- Runs continuously for ≥24 hours without human intervention
- Successfully lands multiple verified fixes
- Cleanly rolls back failed mutations (no leftover broken state)
- Produces a queryable audit trail (mutation, reason, test result, decision)
- Shows measurable improvement (or at least non-degradation) in test pass rate / coverage / simple performance metrics

## Related

- Aura runtime: https://github.com/cybrid-systems/aura
- Organization: https://github.com/cybrid-systems

## Quick start (v0 skeleton)

```bash
# Requires an Aura binary. Set AURA_BIN or pass --aura-bin.
export AURA_BIN=/path/to/aura

python3 harness/run.py --cycles 2 --mode hold
# reports/<run-id>/audit.jsonl is the audit. One Aura process per cycle.
```

The spec is `docs/design.md`. Each cycle locates `choose-fn`, applies one
closed-catalog proposal, and keeps it only when the fixture score rises.
`--dry-run` rolls the proposal back without rebinding it. `--proposer llm`
is optional and off by default. Soak (`--hours` with `--cycles 0`) is off
unless you ask for it.
