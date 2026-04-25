# AGENTS.md — Rookery contribution conventions

This file is the persistent contract between this repository and any
agent (or human) making changes. Every OpenHands session on this repo
loads this automatically.

## Prime directive

**Delegate liberally.** When asked to do anything non-trivial, prefer
spawning a focused sub-agent (via the OpenHands SDK's delegation or
`task` tool) over writing a monolithic patch yourself.

Rule of thumb: if a task touches more than one of
(design, code, tests, docs, CI, data), split it across sub-agents.

This repo's architecture is *about* swarms of specialists cooperating;
building it any other way would be incoherent.

### When to spawn a sub-agent

- Reading/understanding an unfamiliar module: spawn a "comprehension"
  sub-agent with a tight prompt asking for a structured summary.
- Writing a non-trivial function: spawn an "implementation" sub-agent
  with the interface, the invariants, and the test expectations.
- Writing tests: spawn a "test-author" sub-agent; never mix test writing
  into the same turn as implementation.
- Reviewing your own diff: spawn a "critic" sub-agent with the diff and
  a review checklist before committing.
- Investigating a failure: spawn a "bisect" sub-agent with the symptom
  and a hypothesis budget.

A good sub-agent prompt specifies: scope, acceptance criteria, the
concrete artifact to return, and a hard token/time budget.

### When NOT to delegate

- Single-file edits under ~30 lines
- Reading a file you already have in context
- Running a shell command you already know the form of
- Producing a final user-facing response

## Model

All agents in this repo default to **DeepSeek 4 Flash** via the
OpenAI-compatible endpoint. The exact model string is controlled by one
env var so it can be adjusted without code changes:

```bash
ROOKERY_MODEL=deepseek-4-flash          # override if the exact name
                                        # differs on your DeepSeek account
ROOKERY_BASE_URL=https://api.deepseek.com/v1
ROOKERY_API_KEY=...                      # from DeepSeek console
```

Sub-agents inherit these unless overridden. If you need a deep-thinker
model for a specific sub-agent (e.g., architectural critique), pass
`model=` explicitly when delegating.

## Code conventions

- Python ≥ 3.11
- `ruff` for lint + format (config in `pyproject.toml`)
- Type hints everywhere; `from __future__ import annotations` at the top
- Tests under `tests/` via `pytest`; no mocks unless strictly necessary
  (follow the same no-mocks ethos as MiroThinker — test real code paths)
- Module docstrings explain *why*, not *what*
- Functions > ~60 lines or files > ~400 lines: split or spawn a
  sub-agent to refactor

## Commit hygiene

- One logical change per commit
- Commit messages: imperative mood, reference the design doc section
  when applicable (e.g., `operator-loop: wire pre-hook (docs §3.2)`)
- Include `Co-authored-by: openhands <openhands@all-hands.dev>` on
  agent-authored commits

## Testing expectations

- Every new module lands with at least one real-path test
- Architectural invariants (clone append-only discipline, scorecard
  monotonicity, etc.) must have explicit tests; these are stronger than
  unit tests
- No CI gate is currently enforced on this repo; act as if there is one

## Architectural invariants (do not violate)

1. **Clone prefixes are append-only.** Once bytes are committed to a
   clone's prefix file, they are not rewritten. This is the discipline
   that makes vLLM prefix caching survive across rounds.
2. **Verdicts carry evidence.** A clone's verdict without a pointer to
   concrete evidence (file:line, test name, git SHA, AST path) is a
   warning-sign and should be rejected by the critic loop.
3. **Operator-turn budget is a first-class feature.** Pre-hook ≤ 10s,
   critic ≤ 15s. Exceeding the budget discards the verdict; it does not
   block the operator.
4. **The datalake is the product.** Reports are views over it. Never
   design something that makes report generation easier at the cost of
   datalake integrity.
5. **Ground truth beats gossip.** When two clones disagree and the
   disagreement is computable, run the tool; don't LLM-adjudicate.

## What the rookery skill provides to OpenHands

- A delegation-target agent: `rookery.consult(intent, targets, ...)`
- A set of MCP-style tools: `swarm.ask`, `swarm.explain`,
  `swarm.verify`, `swarm.history`
- A persistent datalake on disk (default `./.rookery/`) that compounds
  across sessions for the same repo.

## When you're stuck

Don't widen the scope — narrow it. Spawn a sub-agent with a smaller
question than the one you had. This is also how humans should work on
the repo.
