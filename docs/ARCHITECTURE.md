# Architecture

## 1. Thesis

**A codebase is a better substrate for a specialist swarm than research
text is.** Two reasons:

1. *Angles are structural*: modules, call graphs, I/O boundaries, test
   surface, git-history hotspots. They can be detected mechanically
   rather than guessed by an LLM.
2. *Claims are verifiable*: "function X is pure", "this change breaks
   API Y", "this edit introduces a race" can all be checked by tooling
   (AST, types, tests, static analyzers, the program itself). Swarm
   gossip therefore has a deterministic tie-breaker — when two clones
   disagree, we run the tool and the tool wins.

These two properties make the MiroThinker swarm architecture work
better on code than it does on its original domain.

## 2. Two modes

Rookery runs in exactly two modes, and they are different pipelines
with different budgets.

### 2.1 Comprehension (cold, minutes, rare)

Triggered on a repository for the first time, after a major merge, or
when staleness detection flags a set of clones as out of date.

1. **Angle detection** — structural, not personal. Read the repo graph;
   cluster by module, call neighborhood, churn density, test surface,
   I/O locality, concurrency usage, config surface, historical
   bug-proneness (derived from `git log` + `git blame`).
2. **Worker assignment** — one tear-down worker per angle. Each gets a
   heavy toolkit: AST walkers, `rg`/`ast-grep`, git history queries,
   type-checker, test runner.
3. **Full-corpus gossip with verification** — workers exchange
   intermediate summaries and also retain their raw section (the
   MiroThinker "full-corpus gossip" pattern). When workers disagree
   and the disagreement is computable, a dedicated verifier runs the
   tool and writes the verdict to the datalake.
4. **Clone materialization** — each angle becomes a persistent clone:
   a long-context prefix (targeting 500K–1M tokens) containing relevant
   code, tests, blame slices, ontologies built during tear-down, a
   small exemplar set, and a scorecard.
5. **Artifacts written to the datalake**:
   - Repo graph (Neo4j or JSON for early stages)
   - Clones (B2 or local blob store, content-addressed)
   - Comprehension report (optional, layered like MiroThinker's
     rapporteur/translator output)

Cold-path budget: minutes. Run rarely. Cache aggressively.

### 2.2 Guidance (hot, seconds, every operator turn)

Triggered on every operator action in an OpenHands session.

```
1.  Operator emits intent: "I'm about to X" (edit/run/plan/read)
2.  Routing layer picks 1–3 relevant clones based on:
      - target paths and symbols
      - intent class
      - recent verdicts (clones that have recently caught something
        get temporary priority)
3.  PRE-HOOK (parallel, budget ≤ 10s):
      Each selected clone answers "before X, what matters?"
      Returns typed advisory: prerequisites, pitfalls, invariants,
      tests to run, files to inspect.
4.  Operator receives the advisory, incorporated into its next step.
5.  Operator produces an action (patch, tool call, plan step).
6.  CRITIC (parallel, budget ≤ 15s):
      Each selected clone returns a typed verdict: APPROVE / WARN /
      BLOCK, with evidence references (file:line, test name, git SHA).
      When clones disagree, tooling is invoked as tie-breaker.
7.  Verdicts are logged to the datalake and surfaced to the operator.
      BLOCK is advisory, not enforced: operators see it and revise.
```

Hot-path budget discipline:
- Pre-hook > 10s → discarded, operator proceeds uninformed (logged)
- Critic > 15s → discarded, operator proceeds, verdict filed late for
  training/calibration only
- Hot-path never re-prefills a clone. If a clone is not warm in vLLM
  prefix cache, it is **not consulted** this turn; the operator is
  informed the clone is warming and the verdict will be filed late.

## 3. Clones

See [CLONE_ROSTER.md](CLONE_ROSTER.md) for the default roster.

Clone object (conceptual):

```
Clone {
    id:             "invariant_keeper:myrepo:v7"
    role_prompt:    <immutable, short — who am I and what do I guard>
    corpus:         <append-only segment: raw code, tests, blame slices>
    residue:        <append-only segment: reasoning trail, ontologies>
    exemplars:      [<worked-example decisions>, ...]
    scorecard: {
        domains_strong: [...],
        domains_weak:   [...],
        calibration:    {approve_precision, warn_precision, block_precision},
        recent_verdicts: [...]
    }
    prefix_sha256:  <hash of immutable prefix segments>
    size_tokens:    <estimated, tracked over time>
    version:        <monotonic integer>
    predecessors:   [...]  # DAG of earlier versions
}
```

### Append-only discipline

Clones are versioned and within a version their prefix is strictly
append-only. New evidence, new reasoning, new exemplars append at the
tail. The head and middle never change. This is what keeps vLLM prefix
caching valid across rounds and sessions.

When a clone approaches its size budget (~950K tokens on a 1M model),
the orchestrator spawns a successor: a curated extraction of the parent
clone, designed to be high-signal in the same domain. The parent
remains available until the successor passes a smoke test.

## 4. The datalake

See [DATALAKE.md](DATALAKE.md).

Three stores:

- **Blob store** (B2 or local): content-addressed raw corpus, sources,
  clone prefix files, reports. Immutable.
- **SQL index** (DuckDB): structured facts — symbols, files, findings,
  verdicts, score dimensions. Queryable. Indexed.
- **Graph** (Neo4j, optional for early stages): relationships — clone
  versions, finding lineage, call graph, coverage graph.

Cross-run federation: every row carries `run_id` and `repo_id`, and
lineage links span runs. Subsequent runs inherit prior clones and raw
sources rather than rebuilding from cold.

## 5. Ground-truth mechanism

When two clones disagree on a computable claim, the swarm does not run
a third LLM to adjudicate. It runs the tool:

| Claim shape | Tool |
|-------------|------|
| "X function is pure / has side effects" | taint analyzer, grep + AST |
| "This patch breaks the public API" | public-API diff tool |
| "Tests T catch this regression" | `pytest` on patched tree |
| "This change requires a migration" | schema-diff tool |
| "Dependency Y is compatible" | lockfile resolver dry-run |
| "This path has a history of regressions" | `git log -S` + revert detection |
| "Symbol is dead code" | call-graph query |
| "Race is possible here" | static concurrency linter |

Tool results are first-class evidence in verdicts. A verdict without
evidence is rejected by the critic-loop sanitizer before reaching the
operator.

## 6. Latency model

The plugin is unusable if the hot path doesn't stay under budget.
Design constraints:

- **Prefix-cache dependency is absolute.** Clones consulted on the hot
  path must be warm. A clone that needs a ~30s prefill cannot be
  consulted in a 25s hot-path budget. The routing layer is allowed to
  *decline* to consult a cold clone.
- **Decode-only queries.** Every hot-path query is prefix-cached +
  2–4K query tokens + ~500 output tokens. On DeepSeek 4 Flash via the
  DeepSeek API (or a locally-served vLLM), this lands in the 1–3s
  range per clone per query.
- **Parallelism.** The 1–3 clones consulted on a turn run in parallel;
  the total budget is dominated by the slowest.
- **Failed budget is a log entry, not an error.** Operators proceed.

## 7. What's explicitly out of scope

- Auto-applying patches. Rookery does not edit code. It informs the
  operator that edits.
- Parallel competing implementations. There is one operator. The
  swarm does not race against it.
- Persona role-play. Clones are specializations by *angle*, not by
  persona. "Senior Rust Reviewer" is not a clone; "Concurrency Keeper"
  is.
- Replacing CI. Rookery is pre- and intra-turn advisory; CI remains
  the gate at merge time.

## 8. Open questions

These are deliberately left open; they will be resolved with evidence
from Stage 1 and Stage 2 builds:

- Optimal clone size budget on DeepSeek 4 Flash (the model's effective
  context and prefix-cache behavior has to be measured on a real repo).
- Whether to default to a remote API model (DeepSeek) or a locally
  served vLLM. The repo supports both; the default is DeepSeek for
  out-of-the-box usability.
- Exact scorecard calibration schedule.
- Policy when critic BLOCKs with low confidence vs. the operator wants
  to proceed.
