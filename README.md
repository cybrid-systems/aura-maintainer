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
