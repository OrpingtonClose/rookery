# Validation runs

This file records concrete runs of rookery on real repositories,
including weaknesses found and fixes applied. Treat it as a changelog
for the plugin's empirical behavior.

## 2026-04-25: First real run (MiroThinker, DeepSeek v4 Flash)

### Setup

- Model: `deepseek-v4-flash` via DeepSeek public API
- Target repo: `MiroMindAI/MiroThinker` fork with OrpingtonClose's active work
- Repo size: 413 Python files, ~6.7 MB of code
- Worker budget: 120 KB of source per clone (single-pass baseline)

### Tear-down results

```
8 angles detected, 8 clones materialized
elapsed:        158.6 s (2.6 min)
prompt tokens:  272,566
output tokens:  33,790 (+7,641 reasoning)
cache hits:     0  (first run — no prior prefix to match)
datalake size:  2.5 MB
```

Clone ids: `contract_keeper`, `invariant_keeper`, `side_effect_keeper`,
`concurrency_keeper`, `test_keeper`, `build_keeper`, `history_keeper`,
`convention_keeper`.

All eight materialized successfully; zero worker failures. The
`build_keeper` saw only 1 file (its path-globs are strict); everything
else saw the same 120 KB slice of the repo.

### Ground-truth validation questions

Four questions were put to the clones. Each had a known correct
answer from a manual analysis of the repo done earlier in the same
session. The point was to see whether the clones would produce
answers that matched the known ground truth, and — more importantly —
whether they would signal ignorance when their scope didn't cover the
relevant file.

#### Q1 — history_keeper

> "What fraction of recent commits are agent-authored vs
> human-authored, and what implications for code review?"

**Ground truth**: ~85% Devin AI authored (566/669 commits in 30 days).

**Clone answer**: honest refusal on commit-history fraction ("no
commit messages visible; can't answer as asked"), but pivoted to a
file-structure-based authorship estimate (~20% agent-generated,
heavy in MCP server stubs) with reasonable rationale. Flagged the
tooling gap ("enable git log in future passes").

**Grade**: A for honesty, limited by the fact that tear-down doesn't
yet feed git log into clone prefixes. Noted as follow-up work.

#### Q2 — concurrency_keeper

> "What parallelism primitive is used across the swarm
> orchestrator? Cite file:line refs."

**Ground truth**: `asyncio.gather` + `asyncio.Semaphore`.

**Clone answer**: Self-reported scope limitation ("188 of 501 files
read, all swarm/ files were deferred"). Then inferred the likely
primitive from evidence in other files (`max_concurrent: 5` config,
`with_timeout` decorator) — correctly concluded
`asyncio.Semaphore + asyncio.gather`. Also flagged six real
concurrency footguns (sandbox cancellation leak, missing
`asyncio.shield`, blocking I/O in async contexts).

**Grade**: A. The clone reasoned correctly about what it *could* and
*could not* know, and the footgun list is actionable.

#### Q3 — contract_keeper

> "Does `FlockQueryManager` actually execute DuckDB Flock SQL
> functions, or bypass them?"

**Ground truth**: It bypasses them — the manager fires HTTP requests
directly at vLLM; no `llm_complete` / `llm_filter` SQL is executed.

**Clone answer**: *Refused to answer.* Explicitly stated that the
relevant file (`swarm/flock_query_manager.py`) was deferred,
enumerated the indirect clues available, and offered to re-read.

**Grade**: A+. This is the behavior that makes a specialist swarm
useful — **refusing to bluff** when scope is insufficient. Exactly
the contract we want.

#### Q4 — invariant_keeper

> "What invariants does the ConditionStore DAG rely on?
> Specifically: can rows be deleted, is `parent_id` a real FK, what
> prevents orphaning, schema-change policy?"

**Ground truth**: soft-delete via `consider_for_use = FALSE` (the
column exists and is filtered in queries); `parent_id` is NOT an
SQL-level FK; no orphan-prevention mechanism; v1 schema migration
logic exists (in `corpus_store.py`).

**Clone answer (first attempt)**: Produced a confident, structured
verdict that was **~50% wrong**. Claimed rows could be deleted
freely (missed the `consider_for_use` soft-delete pattern). Claimed
no migration mechanism existed (missed the v1 migration logic in
`corpus_store.py`). Got the FK and orphaning claims right.

**Grade (first attempt)**: C. The clone extrapolated from absence —
"if I didn't see a soft-delete, there isn't one" — when the relevant
file was simply outside its read budget. This is the canonical code-
comprehension failure mode: confident wrongness.

### Fix applied

A scope-awareness guardrail was added to the `ask` flow
(`rookery/operator/ask.py`). Every question now has appended:

> "Before answering, check the corpus manifest above: which files
> did you read, which were deferred? If the most relevant file to
> this question is in the DEFERRED list, say so explicitly at the
> start of your answer and hedge your conclusions. Do not
> extrapolate from absence. Cite file:line refs only for files you
> actually read."

Re-running Q4 with the guardrail produced a dramatically better
answer:

- Opens with `"Deferred file note: apps/adk-agent/models/corpus_store.py
  was not read in this pass."`
- Correctly identifies `consider_for_use` as a soft-delete flag
  (the thing it missed first time).
- Every verdict is explicitly hedged.
- Ends with a caveat that the true invariants remain unknown without
  reading the deferred file.

**Grade after fix**: A-. Still not perfect (can't confirm positive
evidence without the file), but no longer gives false confident
answers. Exactly the right failure mode for an evidence-backed
swarm.

### Observations on prefix caching

On the second ask of the same clone, `cache_hit_tokens` was 99% of
prompt tokens (9984 / 10090). This is DeepSeek's own API-side prefix
caching — no local vLLM required. The "context-as-LoRA" economics
hold: repeated asks of a materialized clone cost essentially only the
decode tokens, not the prefix.

### Validation summary

- Tear-down of a non-trivial repo: **works**, ~3 minutes, ~$0.01
  worth of tokens.
- Clones produce specialist-quality answers when their scope matches.
- Clones correctly refuse/hedge when their scope doesn't match —
  after the guardrail fix.
- Prefix cache hit rate ~99% on repeat queries: the economics
  compound across a session.

### Remaining work

Ordered by priority:

1. **Git log into clone prefixes** — the history_keeper is crippled
   without it. Feed the last N commits + stats as a dedicated
   residue segment during tear-down.
2. **Multi-pass tear-down** — the 120 KB budget per worker means a
   6.7 MB repo gets ~2% coverage per clone. A second pass should
   process the deferred files, prioritized by the open_questions
   field of the first pass's scorecard.
3. **Estimated-coverage surfaced in ask output** — when a clone's
   scorecard reports `estimated_coverage: 0.2`, the user should see
   that before the answer.
4. **Gossip** — Stage 1 workers don't yet read each other's
   residue. This is the next MiroThinker pattern to port.
5. **Critic loop (Stage 3)** — not validated here; requires an
   operator integration session.
