"""Tear-down worker — one angle, one pass.

A worker reads its angle's files, calls the LLM to produce a
structured comprehension pass, and writes the result into the clone's
append-only prefix. No gossip yet (Stage 1 baseline); gossip lands in
a later PR once the single-pass worker is proven.

The worker's output has two parts:
  1. ``residue`` — an append-only text segment containing the
     worker's structured analysis (what the module does, invariants,
     hotspots, risks). This goes into the clone prefix.
  2. ``scorecard`` — a small dict summarizing what this clone claims
     to know and where it thinks its blind spots are.

Files are read and bounded: we budget ~120KB of source per worker on
the first pass to keep cold runs cheap. A follow-up pass (Stage 1.1)
will process the remainder in chunks.

Agents touching this file: the worker is the heart of the tear-down.
Keep it honest — no silent truncation without logging, no pretending
to have read files that didn't fit in the budget.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from rookery.clones.model import Clone
from rookery.clones.roster import CloneSpec
from rookery.llm import LLMClient, LLMResponse
from rookery.tearing_down.angles import AngleAssignment

logger = logging.getLogger(__name__)


# How much source code we feed the worker per pass. Bounded so the
# first pass always fits under ~30k input tokens.
_SOURCE_BUDGET_CHARS = 120_000


@dataclass
class WorkerResult:
    """What one worker produced for one clone version."""

    clone_spec: CloneSpec
    files_read: list[Path]
    files_skipped_for_budget: list[Path]
    residue_text: str
    scorecard: dict[str, object]
    llm_usage: dict[str, int] = field(default_factory=dict)


def _pack_sources(
    paths: list[Path], repo_root: Path, budget: int
) -> tuple[str, list[Path], list[Path]]:
    """Concatenate file contents up to ``budget`` chars.

    Returns (packed_text, included, excluded). Prioritizes smaller
    files first on the theory that they're more likely to be the
    navigational spine (``__init__.py``, small interfaces) and the
    bigger ones can wait for a follow-up pass.
    """
    by_size = sorted(paths, key=lambda p: _safe_size(p))
    chunks: list[str] = []
    used = 0
    included: list[Path] = []
    excluded: list[Path] = []

    for p in by_size:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("cannot read %s: %s", p, exc)
            excluded.append(p)
            continue

        rel = p.relative_to(repo_root).as_posix()
        header = f"\n\n===== FILE: {rel} =====\n"
        need = len(header) + len(text)

        if used + need > budget:
            excluded.append(p)
            continue

        chunks.append(header)
        chunks.append(text)
        used += need
        included.append(p)

    # Stable output order: by path, not by size.
    return ("".join(chunks), sorted(included), sorted(excluded))


def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _build_prompt(
    *,
    spec: CloneSpec,
    repo_id: str,
    packed_sources: str,
    files_included: list[Path],
    files_excluded: list[Path],
    repo_root: Path,
) -> str:
    """Prompt for the worker's comprehension pass.

    Produces structured output we can both append to the clone prefix
    and parse into a scorecard. Output contract is strict: a narrative
    section followed by a machine-readable JSON block.
    """
    included_list = "\n".join(f"  - {p.relative_to(repo_root).as_posix()}" for p in files_included)
    excluded_list = (
        "\n".join(f"  - {p.relative_to(repo_root).as_posix()}" for p in files_excluded)
        or "  (none)"
    )

    return f"""You are the {spec.role_short} for repository ``{repo_id}``.

Role:
{spec.role_prompt}

You have just completed a first reading pass of the code below. Produce:

1) A NARRATIVE of what you now understand. Not a summary of the code —
   your *internal model*: the mechanisms you identified, the ontology
   you built, the landmarks you'll use to navigate this angle on
   future questions. Be dense and specific. Name symbols, files, and
   lines where helpful. 800–1500 words.

2) A JSON block (and ONLY JSON) at the end, fenced as ```json ... ```,
   with this exact shape:

   {{
     "domains_strong": ["short phrases describing what you are now
                        trustworthy about on this repo"],
     "domains_weak":   ["short phrases describing what you still don't
                        know or are guessing at"],
     "landmarks": [
       {{"kind": "symbol|file|pattern", "ref": "...", "why": "..."}},
       ...
     ],
     "open_questions": ["concrete questions about this repo you could
                        not answer from this pass"],
     "estimated_coverage": 0.0
   }}

   ``estimated_coverage`` is your honest estimate (0.0–1.0) of what
   fraction of this angle's scope you actually read and understood on
   this pass.

Files included in this pass ({len(files_included)} of {len(files_included) + len(files_excluded)}):
{included_list}

Files deferred to later passes (did not fit in budget):
{excluded_list}

────────────────────────────────────────────────────────────────────
SOURCE CODE:
{packed_sources}
────────────────────────────────────────────────────────────────────

Produce the narrative, then the JSON block. Nothing after the JSON.
"""


def _parse_scorecard(text: str) -> dict[str, object]:
    """Extract the trailing ```json ... ``` block from the worker's output.

    Tolerant: if parsing fails, return an empty dict and log. This
    keeps a malformed worker response from taking down the whole run.
    """
    # Find the LAST ```json fence — tolerant of prose that may also
    # contain stray backticks.
    marker = "```json"
    idx = text.rfind(marker)
    if idx < 0:
        logger.warning("scorecard JSON block not found")
        return {}

    start = idx + len(marker)
    end = text.find("```", start)
    if end < 0:
        logger.warning("scorecard JSON block not closed")
        return {}

    payload = text[start:end].strip()
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        logger.warning("scorecard JSON parse failed: %s", exc)
        return {}

    if not isinstance(data, dict):
        logger.warning("scorecard JSON was not an object: %r", type(data))
        return {}
    return data


def _narrative_without_json(text: str) -> str:
    """Strip the trailing ```json ... ``` block from the narrative.

    Returns the prose we'll append as the clone's residue segment.
    """
    marker = "```json"
    idx = text.rfind(marker)
    if idx < 0:
        return text.rstrip()
    return text[:idx].rstrip()


async def run_worker(
    *,
    assignment: AngleAssignment,
    clone: Clone,
    repo_root: Path,
    repo_id: str,
    llm: LLMClient,
    model: str,
    max_tokens: int = 8000,
) -> WorkerResult:
    """Run one angle's first-pass comprehension.

    Side effects:
      - Appends a ``corpus`` segment to ``clone.current`` listing the
        files read (source of truth for what the worker saw).
      - Appends a ``residue`` segment with the worker's narrative.
      - Writes the LLM's scorecard to the clone's version-level
        scorecard (does not persist to disk here; caller does that).
    """
    spec = assignment.clone_spec
    logger.info(
        "worker %s: %d files, %d bytes in scope",
        spec.id,
        len(assignment.paths),
        assignment.scope_bytes,
    )

    packed, included, excluded = _pack_sources(
        assignment.paths,
        repo_root,
        _SOURCE_BUDGET_CHARS,
    )

    prompt = _build_prompt(
        spec=spec,
        repo_id=repo_id,
        packed_sources=packed,
        files_included=included,
        files_excluded=excluded,
        repo_root=repo_root,
    )

    response: LLMResponse = await llm.complete(
        prompt,
        model=model,
        max_tokens=max_tokens,
        temperature=0.2,
    )

    narrative = _narrative_without_json(response.content)
    scorecard_data = _parse_scorecard(response.content)

    if not narrative:
        logger.warning(
            "worker %s returned empty narrative (finish=%s, reasoning_tokens=%d)",
            spec.id,
            response.finish_reason,
            response.usage.reasoning_tokens,
        )
        narrative = f"[empty narrative from worker; finish={response.finish_reason}]"

    # Write the corpus-manifest segment: what this clone actually saw.
    # Keeping this as a segment (not just metadata) means the clone's
    # prefix is self-describing when rehydrated.
    manifest_lines = [
        f"# Corpus manifest for clone {clone.id} (worker pass 1)",
        f"# Repo: {repo_id}",
        f"# Files read: {len(included)}",
        f"# Files deferred: {len(excluded)}",
        "",
        "## Files read",
        *(f"  - {p.relative_to(repo_root).as_posix()}" for p in included),
    ]
    if excluded:
        manifest_lines += [
            "",
            "## Files deferred (budget)",
            *(f"  - {p.relative_to(repo_root).as_posix()}" for p in excluded),
        ]
    manifest = "\n".join(manifest_lines) + "\n"

    version = clone.current
    version.append_segment(kind="corpus", text=manifest, origin="worker_pass_1")
    version.append_segment(kind="residue", text=narrative + "\n", origin="worker_pass_1")

    # Update the clone's version-level scorecard from the worker JSON.
    # Only fields we recognize; ignore unknown keys rather than crash.
    sc = version.scorecard
    if isinstance(scorecard_data.get("domains_strong"), list):
        sc.domains_strong = [str(x) for x in scorecard_data["domains_strong"]]
    if isinstance(scorecard_data.get("domains_weak"), list):
        sc.domains_weak = [str(x) for x in scorecard_data["domains_weak"]]
    coverage = scorecard_data.get("estimated_coverage")
    if isinstance(coverage, (int, float)):
        sc.calibration["estimated_coverage"] = float(coverage)

    return WorkerResult(
        clone_spec=spec,
        files_read=included,
        files_skipped_for_budget=excluded,
        residue_text=narrative,
        scorecard=scorecard_data,
        llm_usage={
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "reasoning_tokens": response.usage.reasoning_tokens,
            "cache_hit_tokens": response.usage.cache_hit_tokens,
            "cache_miss_tokens": response.usage.cache_miss_tokens,
        },
    )
