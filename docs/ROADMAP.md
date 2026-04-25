# Roadmap

Staged, in the order that de-risks the project. Each stage is
independently useful; we do not commit to Stage 4 from Stage 1.

## Stage 1 — Comprehension only

**Outcome:** a tear-down pipeline that produces a repo graph and a
persisted clone set, plus a layered comprehension report.

No operator integration yet. The deliverable is a standalone tool that
ingests a repo and emits the datalake.

Scope:
- Angle detection (structural)
- Tear-down workers with verification-capable tools
- Clone materialization with append-only discipline
- Blob store (local FS), SQL index (DuckDB)
- Comprehension report at three depths

Exit criterion: a human reads the report and agrees it captures the
repo meaningfully better than a single-pass summary; the clones can
answer follow-up questions about the repo correctly more often than
not.

## Stage 2 — Consultable tools

**Outcome:** OpenHands operators can voluntarily invoke the swarm
mid-session via MCP-style tools.

Scope:
- `swarm.ask`, `swarm.explain`, `swarm.history` exposed as tools
- Clone rehydration from the datalake
- Routing heuristics (path/symbol/intent → clones)
- No auto-invocation yet

Exit criterion: operators voluntarily use the tools; tool calls have
measurably better outcomes (terminal success rate, fewer turns) vs a
baseline without the tools.

## Stage 3 — Critic loop

**Outcome:** auto-invocation of the swarm after the operator proposes
an action; verdicts surfaced.

Scope:
- Delegation-target interface (`rookery.consult(...)`)
- Critic phase with tool-grounded evidence
- Verdict sanitizer
- BLOCK is advisory only

Exit criterion: verdict precision > 70% (BLOCKs that correlate with
actual downstream problems); operator compliance > 50% (operators act
on a majority of surfaced BLOCKs).

## Stage 4 — Pre-hook + compounding datalake

**Outcome:** the plugin gets better with use.

Scope:
- Pre-hook phase before operator acts
- Calibration job consuming verdict log
- Staleness detection for clones
- Clone retirement + successor spawning
- Publishable datalake

Exit criterion: measurable improvement in Stage-3 metrics (precision,
compliance) over a defined window after calibration is wired in.

## Non-goals for any stage

- Auto-applying patches
- Replacing CI
- Parallel competing implementations
- Persona role-play clones
