# Design Notes — Aura Maintainer

## v1 Target: aura-redis

**Primary target**: [cybrid-systems/aura-redis](https://github.com/cybrid-systems/aura-redis) (added as submodule under `target/aura-redis`).

### Why aura-redis

- Written in Aura / Soft (policy agent, control plane, RESP handling)
- Has real tests, benchmarks, and CI
- Already explores runtime policy mutation (eviction strategies via hot-strategy)
- Mid-sized and has clear improvement surface (bugs, edge cases, performance, incomplete features)
- Strong narrative: “Agent continuously maintains and improves a Redis-compatible service”

Focus for v1: **Aura policy / control-plane code** (`src/redis/policy_agent.aura`, choose-fn bodies, related tests), not the C data plane.

## Core loop

1. **Discover** — Scan target for issues (failing tests, static signals, TODOs, complexity, perf regressions, incomplete command coverage).
2. **Locate** — Use Aura `query:*` primitives to find precise AST nodes / functions / call sites inside the Aura/Soft code.
3. **Propose** — Generate a candidate mutation (function rewrite, local refactor, bug fix, better policy logic).
4. **Mutate** — Apply via `mutate:*` (snapshot taken first).
5. **Verify** — Run the relevant test suite / smoke / lightweight bench.
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

## Metrics to track

- Mutations attempted / kept / rolled back
- Test pass rate over time
- Simple quality signals (coverage, complexity, key benchmark deltas if available)
- Wall-clock time and number of successful autonomous cycles
- Audit log completeness and queryability

## Non-goals (v1)

- Full autonomy on the entire Aura runtime
- Guaranteed semantic correctness beyond tests
- Multi-day unattended production deployment of a public Redis
- Replacing human review for high-stakes changes
- Modifying the C data plane as the primary path

## Next steps

1. Stabilize a minimal harness that can:
   - Load aura-redis Aura sources into an Aura workspace
   - Run a discovery → mutate → test → keep/rollback cycle
   - Emit a structured audit log
2. Identify 5–10 concrete, testable improvement opportunities in the policy/control plane
3. Run a first 4–8 hour autonomous session and publish the report
