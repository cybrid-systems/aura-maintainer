# Design Notes — Aura Maintainer

## Core loop

1. **Discover** — Scan target codebase for issues (static analysis, failing tests, simple perf signals, TODOs, complexity hotspots).
2. **Locate** — Use Aura `query:*` primitives to find precise AST nodes / functions / call sites.
3. **Propose** — Generate a candidate mutation (function rewrite, local refactor, bug fix).
4. **Mutate** — Apply via `mutate:*` (with snapshot taken first).
5. **Verify** — Run the target's test suite (and any lightweight perf checks).
6. **Decide**
   - Tests pass + no regression → KEEP, stamp provenance, continue.
   - Tests fail or regression → ROLLBACK to snapshot, log reason, continue.
7. **Record** — Append structured audit entry (timestamp, mutation, reason, test result, decision, metrics delta).

## Key Aura capabilities exercised

- `query:*` for surgical location
- `mutate:*` + incremental compilation for fast feedback
- `ast:snapshot` / `ast:rollback` for reliable undo
- Provenance / audit primitives for the trail
- Agent orchestration if multi-agent discovery/proposal is used later

## Target selection criteria (v1)

- 5k–20k lines
- Has a real, reasonably fast test suite
- Preferably in a language the agent can reason about well (or that Aura can host/edit)
- Public, permissive license
- Not already perfect (room for real fixes)

Candidates to evaluate: small Redis modules, focused C++/Python libraries, or a controlled subset of a larger project.

## Metrics to track

- Mutations attempted / kept / rolled back
- Test pass rate over time
- Simple quality signals (coverage, complexity, runtime of key benchmarks if available)
- Wall-clock time and number of successful autonomous cycles
- Audit log completeness and queryability

## Non-goals (v1)

- Full autonomy on arbitrary large monorepos
- Guaranteed semantic correctness beyond tests
- Multi-day unattended production deployment
- Replacing human review for high-stakes changes
