"""Public operator-facing API.

This is what OpenHands calls. Everything else in Rookery is hidden
behind this surface. The shapes defined here are the stable contract;
internals are free to change.

Implementation status:
    - Data types: STABLE
    - consult():   Stage 3 — stub that returns a typed "unimplemented"
                   advisory, so callers can wire the integration now
                   and the critic loop will light up when Stage 3 lands.
    - ask/explain/verify/history: Stage 2 — same stub shape.

Agents working on this file: delegate implementation to sub-agents per
the AGENTS.md convention. Do NOT write the tear-down or clone runtime
inline here; keep this file thin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# Verdict types (stable contract)
# ---------------------------------------------------------------------------


VerdictKind = Literal["APPROVE", "WARN", "BLOCK"]
IntentKind = Literal["edit", "read", "run", "plan"]


@dataclass
class EvidenceRef:
    """A pointer to concrete evidence backing a verdict.

    A verdict without any evidence refs of kind ``file_line``, ``test``,
    or ``tool_output`` is rejected by the sanitizer before reaching the
    operator (see docs/OPERATOR_LOOP.md §5).
    """

    kind: Literal["file_line", "test", "git_sha", "symbol", "tool_output"]
    ref: str
    excerpt: str = ""


@dataclass
class ToolCall:
    """A tool invocation the clone made to ground its verdict."""

    tool: str
    args: dict[str, object]
    ok: bool
    summary: str = ""


@dataclass
class Verdict:
    """A single clone's judgment on a proposed operator action."""

    clone_id: str
    kind: VerdictKind
    message: str
    evidence_refs: list[EvidenceRef]
    confidence: float
    tool_invocations: list[ToolCall] = field(default_factory=list)


@dataclass
class PreHookAdvisory:
    """What the swarm wants the operator to know before acting.

    All fields are short structured lists, not prose.
    """

    prerequisites: list[str] = field(default_factory=list)
    pitfalls: list[str] = field(default_factory=list)
    invariants: list[str] = field(default_factory=list)
    tests_to_run: list[str] = field(default_factory=list)
    files_to_inspect: list[str] = field(default_factory=list)


@dataclass
class SwarmAdvisory:
    """Return shape of ``consult``."""

    clones_consulted: list[str]
    pre_hook: PreHookAdvisory
    verdicts: list[Verdict]
    blocking: bool
    budget_exhausted: bool
    late_verdicts_filed: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public functions (stubbed — see AGENTS.md, delegate implementation)
# ---------------------------------------------------------------------------


async def consult(
    *,
    intent: IntentKind,
    target_paths: list[str],
    target_symbols: list[str] | None = None,
    summary: str = "",
    diff_preview: str | None = None,
    budget_seconds: float = 20.0,
    phase: Literal["pre_hook", "critic", "both"] = "both",
) -> SwarmAdvisory:
    """Consult the swarm about a proposed or imminent operator action.

    STAGE 3 — STUB. Returns an empty advisory that indicates the
    rookery has been wired but the critic loop is not yet implemented.
    Callers can integrate now; behavior will light up when Stage 3
    lands. See docs/ROADMAP.md.
    """
    return SwarmAdvisory(
        clones_consulted=[],
        pre_hook=PreHookAdvisory(),
        verdicts=[],
        blocking=False,
        budget_exhausted=False,
        late_verdicts_filed=[],
    )


async def ask(clone_id: str, question: str) -> str:
    """Put a direct question to a named clone.

    STAGE 2 — STUB.
    """
    return f"[rookery stub] ask({clone_id!r}, {question!r}) — Stage 2 not implemented"


async def explain(target: str) -> str:
    """Multi-angle comprehension of a symbol or path.

    STAGE 2 — STUB.
    """
    return f"[rookery stub] explain({target!r}) — Stage 2 not implemented"


async def verify(action: str) -> list[Verdict]:
    """Run the critic leg only on a proposed action.

    STAGE 3 — STUB.
    """
    return []


async def history(path: str) -> str:
    """History Keeper's summary of a path.

    STAGE 2 — STUB.
    """
    return f"[rookery stub] history({path!r}) — Stage 2 not implemented"
