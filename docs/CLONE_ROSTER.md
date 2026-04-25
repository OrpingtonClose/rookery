# Clone Roster

The default roster. Not every clone is consulted on every turn; the
routing layer picks 1–3 based on the operator's intent and target
paths. Clones that aren't earning their keep (calibrated via the
scorecard over time) get retired.

Angles are structural. A clone represents a *dimension of risk* in the
codebase, not a persona.

## The eight default clones

### Contract Keeper

Guards the public API surface — exported types, functions, CLIs,
network endpoints, anything downstream code depends on.

- **Reads on pre-hook**: declared public surface, recent SemVer
  history, downstream-dependency hints
- **Checks on critic**: public-API diff, signature changes, exception
  type changes, CLI flag changes
- **Tools**: language-specific public-API differ, type checker

### Invariant Keeper

Guards data models, schemas, migrations, persistence invariants —
anything where "the shape of the data" matters.

- **Reads**: schema files, migration history, ORM models, database
  test fixtures
- **Checks**: missing migrations for schema changes, backward-incompat
  schema changes without versioning, lost-write patterns
- **Tools**: schema-diff, migration linter, fixture sanity runner

### Side-Effect Keeper

Guards I/O, network, filesystem, subprocess, external-service calls.

- **Reads**: I/O call sites, error-handling paths, retry logic,
  timeout configuration
- **Checks**: unhandled errors, missing timeouts, subprocess without
  argument validation, network calls without retries where retries are
  the convention
- **Tools**: grep-based taint tracking, subprocess pattern detector

### Concurrency Keeper

Guards anything with shared state — async, threads, locks, queues,
processes, shared singletons.

- **Reads**: async surface, lock acquisition sites, shared mutable
  state
- **Checks**: nested locks (potential deadlock), async-over-sync
  misuse, task cancellation handling, data-race patterns
- **Tools**: concurrency linters, async-correctness checks

### Test Keeper

Guards the test surface — coverage, fixtures, assertions, what's
tested and what isn't.

- **Reads**: test layout, coverage map, fixtures
- **Checks**: coverage regression, deleted assertions, weakened tests
  (e.g., `==` replaced with `is not None`), test-only code leaking
  into runtime
- **Tools**: coverage diff, `pytest --collect-only`, targeted test
  runs on the proposed patch

### Build/CI Keeper

Guards the toolchain — dependencies, build graph, CI configuration,
release pipeline.

- **Reads**: `pyproject.toml`, lockfiles, CI workflows, Docker files
- **Checks**: dependency upgrades with breaking notes, pinning drift,
  CI job additions/removals, lockfile divergence from manifest
- **Tools**: dependency resolver dry-run, lockfile diff

### History Keeper

The repo's memory. Knows which paths have been touched a lot, which
changes got reverted, which bugs have recurred.

- **Reads**: `git log`, `git blame`, revert detection, bug-marker
  commit messages
- **Checks**: edits to historically bug-prone paths, patterns matching
  previously-reverted changes
- **Tools**: `git log -S`, revert-pair detector, hotspot map

### Convention Keeper

Guards *this* repo's idioms — how things are done here, not in general.

- **Reads**: style guide, recent merged PRs, common patterns
- **Checks**: new code using patterns inconsistent with local idioms
  (naming, error-handling style, layering conventions)
- **Tools**: ruff/eslint with repo-specific rules, pattern-match against
  recent diffs

## Routing heuristics (Stage 3)

The router is a small classifier, not an LLM call. Cheap, deterministic.

```
intent ∈ {edit, read, run, plan}
targets: list of paths + symbols (from the operator's stated intent)

clone eligibility:
  Contract Keeper   ← intent=edit AND targets ∩ public_surface ≠ ∅
  Invariant Keeper  ← intent=edit AND targets ∩ {schema,models,migrations} ≠ ∅
  Side-Effect Keeper← intent=edit AND targets contain I/O call sites
  Concurrency Keeper← intent=edit AND targets contain async/threading
  Test Keeper       ← intent=edit (always candidate)
  Build Keeper      ← targets ∩ {pyproject.toml,lockfiles,CI} ≠ ∅
  History Keeper    ← targets contain historically bug-prone paths
  Convention Keeper ← intent=edit AND diff is non-trivial

score = base_eligibility + recency_boost + calibration_boost
       - staleness_penalty - cold_prefix_penalty

select top 2–3 by score
```

## Spawning new clones

Custom clones can be declared in `rookery.yaml`. A clone spec is a
short YAML block:

```yaml
clones:
  - id: database_query_keeper
    role: |
      Guards against N+1 queries and unindexed lookups in the ORM layer.
    tools:
      - orm_query_explain
      - index_coverage_check
    reads:
      paths: ["app/db/**", "app/models/**"]
      patterns: ["\\.query\\(", "\\.filter\\("]
    checks:
      - {name: n_plus_one,     severity: warn}
      - {name: missing_index,  severity: block}
    activation:
      intent: [edit]
      path_any_of: ["app/db/**", "app/models/**"]
```

At first-run, the comprehension phase materializes any configured
clones. The default eight are materialized automatically unless
disabled.
