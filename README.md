# Rookery

**A swarm of specialist clones that understand a codebase, then guide every
move the LLM operator makes.**

> A rookery is the nesting ground of a flock. This repository is where
> code-comprehension clones roost, compound across sessions, and go out to
> inform the operator's decisions.

Rookery is an [OpenHands](https://docs.openhands.dev/) plugin/sub-agent
that brings the [MiroThinker](https://github.com/MiroMindAI/MiroThinker)
swarm architecture to source code. The core bet: **angles in a repository
are structural (modules, call graphs, test surface, git history), and
claims made about code can be verified deterministically** (AST, types,
tests). This makes the swarm's gossip mechanism sharper in code than it
ever gets in research text, and makes compounding clones across sessions
a practical engineering asset rather than a speculation.

## Status

Early scaffold. The architecture is designed; the implementation is
staged. See [docs/ROADMAP.md](docs/ROADMAP.md) for stage gates.

## One-paragraph architecture

Rookery operates in two modes. **Comprehension** (cold, minutes, once
per major repo change) tears a repository down along structural angles —
core modules, I/O boundaries, concurrency surface, test harness,
historical hotspots from git — and materializes each angle as a
long-context clone: a 500K–1M token vLLM prefix containing the relevant
code, tests, blame slices, and the clone's own reasoning trail. Clones
persist in a datalake and are rehydratable across sessions.
**Guidance** (hot, seconds, per operator turn) routes each operator
action to the 2–3 most relevant clones as a pre-hook (what matters
before you act?) and a critic (approve/warn/block the proposed action,
with verified evidence). Verdicts flow back into the datalake and
calibrate clone scorecards over time.

## Documents

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — full architecture
- [docs/CLONE_ROSTER.md](docs/CLONE_ROSTER.md) — the specialist roster
- [docs/OPERATOR_LOOP.md](docs/OPERATOR_LOOP.md) — the hot-path loop
- [docs/DATALAKE.md](docs/DATALAKE.md) — how clones compound across runs
- [docs/ROADMAP.md](docs/ROADMAP.md) — staged build order

## Development conventions

Read [`AGENTS.md`](AGENTS.md) first. The repo is built by agents and its
primary convention is: **delegate to sub-agents liberally**. Humans and
agents contributing here should prefer spawning a focused sub-agent for
any non-trivial task over writing large monolithic patches.

## License

Apache-2.0.
