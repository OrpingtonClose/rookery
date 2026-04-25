"""`rookery ask` / swarm.ask implementation.

Rehydrates a persisted clone from the datalake and runs a single
question against it with the full clone prefix as the system message.

This is the minimum viable Stage-2 tool: a user (or the operator) can
ask a specialist clone a direct question and get an evidence-backed
answer. No critic loop yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from rookery.clones.persist import load_clone
from rookery.config import Config
from rookery.datalake.store import BlobStore, IndexDb
from rookery.llm import LLMClient

logger = logging.getLogger(__name__)


@dataclass
class AskResult:
    clone_id: str
    clone_version: int
    answer: str
    reasoning: str
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int
    elapsed_s: float
    # Surfaced from the clone's scorecard so users can calibrate trust
    # before reading the answer. Derived from the worker's own JSON
    # output during tear-down; None if the clone never reported it.
    estimated_coverage: float | None = None
    domains_strong: list[str] = field(default_factory=list)
    domains_weak: list[str] = field(default_factory=list)


async def ask_clone(
    *,
    config: Config,
    repo_id: str,
    clone_id: str,
    question: str,
    max_tokens: int = 6000,
) -> AskResult:
    """Put a question to a named clone. Returns its answer + telemetry.

    Opens the datalake, rehydrates the clone's latest version, and
    sends a single chat-completion with the clone's full prefix as the
    system message. The caller is responsible for printing/logging.
    """
    dl = config.datalake_dir
    blobs = BlobStore(dl)
    index = IndexDb.open(dl / "index.duckdb")
    try:
        clone = load_clone(
            clone_id=clone_id,
            repo_id=repo_id,
            blobs=blobs,
            index=index,
        )
    finally:
        index.close()

    system_prompt = clone.current.prefix_text()

    # Scope-awareness guardrail: tell the clone to check its corpus
    # manifest BEFORE answering, and to refuse/hedge when the relevant
    # file is in the deferred list. This is the fix for the Q4-style
    # failure mode (confident extrapolation from absence).
    guardrail = (
        "\n\n"
        "Before answering, check the corpus manifest above: which files "
        "did you read, which were deferred? If the most relevant file "
        "to this question is in the DEFERRED list, say so explicitly "
        "at the start of your answer and hedge your conclusions. Do not "
        "extrapolate from absence. Cite file:line refs only for files "
        "you actually read."
    )
    augmented_question = question + guardrail

    logger.info(
        "ask: clone=%s v%d prefix=%d chars, question=%d chars",
        clone_id,
        clone.current.version,
        len(system_prompt),
        len(augmented_question),
    )

    async with LLMClient(
        base_url=config.base_url,
        api_key=config.api_key,
        default_model=config.model,
    ) as llm:
        resp = await llm.complete(
            prompt=augmented_question,
            system=system_prompt,
            model=config.model_for(clone_id),
            max_tokens=max_tokens,
            temperature=0.2,
        )

    sc = clone.current.scorecard
    cov = sc.calibration.get("estimated_coverage")
    return AskResult(
        clone_id=clone_id,
        clone_version=clone.current.version,
        answer=resp.content,
        reasoning=resp.reasoning,
        prompt_tokens=resp.usage.prompt_tokens,
        completion_tokens=resp.usage.completion_tokens,
        cache_hit_tokens=resp.usage.cache_hit_tokens,
        elapsed_s=resp.elapsed_s,
        estimated_coverage=float(cov) if isinstance(cov, int | float) else None,
        domains_strong=list(sc.domains_strong),
        domains_weak=list(sc.domains_weak),
    )
