# Operator Loop

The hot-path contract between OpenHands's operator agent and the
Rookery swarm. Everything in this doc is subject to the budgets in
§3 — if an element misses its budget, it is discarded, not waited on.

## 1. Integration surfaces

Rookery integrates with OpenHands in two complementary ways.

### 1.1 As a delegation target (primary)

The operator agent delegates to Rookery via the SDK's sub-agent
mechanism at specific moments:

```python
advisory = await rookery.consult(
    intent="edit",                          # edit | read | run | plan
    target_paths=["auth/session.py"],
    target_symbols=["UserSession.refresh"],
    summary="allow token refresh past 24h",
    diff_preview=<optional patch|plan>,
    budget_seconds=20,
)
```

Returns a `SwarmAdvisory`:

```python
SwarmAdvisory(
    clones_consulted=[...],
    pre_hook=PreHookAdvisory(
        prerequisites=[...],
        pitfalls=[...],
        invariants=[...],
        tests_to_run=[...],
    ),
    verdicts=[
        Verdict(clone="invariant_keeper", kind="BLOCK",
                message="schema change requires migration",
                evidence_refs=["db/models.py:42", "tests/db/test_schema.py::test_schema_match"],
                confidence=0.92),
        ...
    ],
    blocking=True,
    budget_exhausted=False,
    late_verdicts_filed=[],   # verdicts that will arrive async, filed to datalake
)
```

### 1.2 As on-demand tools (secondary)

The operator can also query the swarm directly without a full consult:

- `swarm.ask(clone_id, question)` — put a question to a named
  specialist. Returns the clone's answer with evidence.
- `swarm.explain(symbol_or_path)` — multi-angle comprehension.
- `swarm.verify(action)` — run just the critic leg on a proposed
  action.
- `swarm.history(path)` — History Keeper's summary of a path.

These are thin wrappers on the same clone infrastructure; they're just
more granular than `consult`.

## 2. The full turn

```
┌──────────────────────────────────────────────────────────────┐
│ OPERATOR receives user intent                                 │
│   e.g. "Allow user tokens to be refreshed past 24h"           │
└─────────────────────────────┬────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│ OPERATOR produces an intent summary + target paths/symbols    │
│   (this is a built-in operator skill, not Rookery's job)      │
└─────────────────────────────┬────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│ ROOKERY.consult(intent=..., targets=..., phase="pre_hook")    │
│                                                                │
│   Router picks clones:                                         │
│     contract_keeper, invariant_keeper, test_keeper             │
│                                                                │
│   Each clone (parallel, budget 10s):                           │
│     - reads its cached prefix (warm in vLLM)                   │
│     - queries itself: "before this edit, what matters?"        │
│     - returns structured advisory                              │
│                                                                │
│   Router assembles PreHookAdvisory, returns.                   │
└─────────────────────────────┬────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│ OPERATOR incorporates advisory, produces an action            │
│   (patch proposal, plan step, tool call)                       │
└─────────────────────────────┬────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│ ROOKERY.consult(intent=..., action=..., phase="critic")       │
│                                                                │
│   Same clones, now with the proposed action in view.           │
│   Each (parallel, budget 15s):                                 │
│     - evaluates the action from its angle                      │
│     - emits APPROVE / WARN / BLOCK with evidence               │
│                                                                │
│   Disagreement resolution:                                     │
│     If two clones disagree on a *computable* claim, the        │
│     tool is invoked as tie-breaker (not a third LLM).          │
│                                                                │
│   Router returns verdicts.                                     │
└─────────────────────────────┬────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│ OPERATOR: sees verdicts.                                      │
│   - No BLOCKs → proceeds with action                           │
│   - BLOCK present → surfaces to user, possibly revises         │
│   - WARNs → surfaces briefly, proceeds unless user intervenes  │
└─────────────────────────────┬────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│ DATALAKE: all verdicts logged, regardless of operator action  │
│   These feed scorecard calibration later.                      │
└──────────────────────────────────────────────────────────────┘
```

## 3. Budgets

Non-negotiable. Violations are logged, but do not hang the operator.

| Stage | Budget | On overrun |
|-------|--------|------------|
| Router | 200ms | Pick one clone deterministically; log |
| Pre-hook total | 10s | Discard late responses; operator proceeds without |
| Critic total | 15s | Discard late responses; filed to datalake for calibration |
| Cold prefix | n/a (hot path) | Clone is simply *not consulted* this turn |

## 4. Verdict shape

Every verdict must have evidence. A verdict without evidence is
sanitizer-rejected before reaching the operator.

```python
@dataclass
class Verdict:
    clone_id: str
    kind: Literal["APPROVE", "WARN", "BLOCK"]
    message: str                    # terse, operator-facing
    evidence_refs: list[EvidenceRef]  # REQUIRED; non-empty
    confidence: float               # 0.0-1.0
    tool_invocations: list[ToolCall]  # tools the clone ran to ground this

@dataclass
class EvidenceRef:
    kind: Literal["file_line", "test", "git_sha", "symbol", "tool_output"]
    ref: str
    excerpt: str = ""
```

## 5. Sanitizer

Before verdicts reach the operator, a sanitizer pass enforces:

1. Every BLOCK has ≥1 evidence ref of kind `test`, `tool_output`, or
   `file_line` (not just a symbol).
2. Confidence is in `[0, 1]`.
3. Message length ≤ 280 chars (operator-facing terseness).
4. No duplicated clone-id in the verdict list.
5. Late verdicts (past budget) are re-routed to the datalake, not the
   operator.

## 6. Calibration (Stage 4 and beyond)

Scorecards are not hand-maintained. A background job consumes the
verdict log and:

- Tracks precision per clone per verdict kind
- Decays priority of clones whose WARNs are routinely dismissed
  without user consequence
- Promotes clones whose BLOCKs prevent issues that would have
  surfaced downstream (CI failure, revert commit, bug-marker commit)
- Retires clones with sustained calibration below threshold and
  surfaces their angle for re-specialization

This makes the plugin self-correcting: loud-but-wrong clones fade;
quiet-but-right clones rise.
