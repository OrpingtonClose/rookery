"""Worker pass-2 behavior: guided by prior open_questions, reads
deferred files, merges (does not overwrite) scorecard fields.

No external LLM calls — we use a stub ``LLMClient`` that returns a
canned narrative + JSON for each call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from rookery.clones.model import Clone
from rookery.clones.roster import CloneSpec
from rookery.llm import LLMResponse, LLMUsage
from rookery.tearing_down.angles import AngleAssignment
from rookery.tearing_down.worker import run_worker


@dataclass
class _StubClient:
    """Minimal stub matching LLMClient's .complete(...) surface."""

    scripted_responses: list[str]
    calls: list[dict] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    async def __aenter__(self) -> _StubClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    async def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.3,
    ) -> LLMResponse:
        self.calls.append({"prompt": prompt, "model": model})
        content = self.scripted_responses.pop(0)
        return LLMResponse(
            content=content,
            reasoning="",
            finish_reason="stop",
            model=model or "stub",
            usage=LLMUsage(prompt_tokens=100, completion_tokens=50),
            elapsed_s=0.01,
            raw={},
        )


PASS1_RESPONSE = (
    "Pass 1 narrative: I read the small files and understood X.\n"
    "\n"
    "```json\n"
    "{\n"
    '  "domains_strong": ["x handling"],\n'
    '  "domains_weak": ["y handling"],\n'
    '  "landmarks": [],\n'
    '  "open_questions": ["Does y handler exist?"],\n'
    '  "estimated_coverage": 0.3\n'
    "}\n"
    "```\n"
)

PASS2_RESPONSE = (
    "Pass 2 narrative: Having read the deferred files, I now see that Y "
    "handler does exist at y.py:7.\n"
    "\n"
    "```json\n"
    "{\n"
    '  "domains_strong": ["y handling"],\n'
    '  "domains_weak": [],\n'
    '  "landmarks": [],\n'
    '  "open_questions": [],\n'
    '  "estimated_coverage": 0.7\n'
    "}\n"
    "```\n"
)


async def test_worker_pass2_merges_scorecard_and_uses_prior_questions(
    tmp_path: Path,
) -> None:
    # Two tiny files, one "in-budget", one "deferred"
    small = tmp_path / "x.py"
    small.write_text("def x():\n    pass\n" * 3, encoding="utf-8")
    large = tmp_path / "y.py"
    large.write_text("def y():\n    pass\n" * 40, encoding="utf-8")

    spec = CloneSpec(
        id="test_keeper",
        role_short="Test Keeper",
        role_prompt="You are the Test Keeper.",
    )
    assn = AngleAssignment(
        clone_spec=spec,
        paths=[small, large],
        scope_bytes=small.stat().st_size + large.stat().st_size,
    )

    clone = Clone(id="test_keeper", repo_id="repo", role="Test Keeper")
    clone.new_version(role_prompt=spec.role_prompt)

    stub = _StubClient([PASS1_RESPONSE, PASS2_RESPONSE])

    # Pass 1 — single call, no prior questions.
    wres1 = await run_worker(
        assignment=assn,
        clone=clone,
        repo_root=tmp_path,
        repo_id="repo",
        llm=stub,  # type: ignore[arg-type]
        model="stub",
        pass_number=1,
    )
    # The worker should have carried pass1 open_questions out for us
    assert wres1.scorecard["open_questions"] == ["Does y handler exist?"]

    # Pass 2 — feed the deferred files + prior questions.
    await run_worker(
        assignment=assn,
        clone=clone,
        repo_root=tmp_path,
        repo_id="repo",
        llm=stub,  # type: ignore[arg-type]
        model="stub",
        pass_number=2,
        only_paths=wres1.files_skipped_for_budget,
        prior_open_questions=[str(q) for q in wres1.scorecard["open_questions"]],
    )
    # Pass 2's prompt MUST mention the prior question verbatim
    pass2_prompt = stub.calls[1]["prompt"]
    assert "Does y handler exist?" in pass2_prompt
    assert "comprehension pass 2" in pass2_prompt
    # Pass 2's scorecard should have merged, not overwritten, strengths
    sc = clone.current.scorecard
    assert "x handling" in sc.domains_strong
    assert "y handling" in sc.domains_strong  # added by pass 2
    # Coverage tracks both passes and the cumulative best
    assert sc.calibration["estimated_coverage_pass_1"] == 0.3
    assert sc.calibration["estimated_coverage_pass_2"] == 0.7
    assert sc.calibration["estimated_coverage"] == 0.7
    # The clone's prefix has TWO manifests + TWO residues by now
    kinds = [s.kind for s in clone.current.segments]
    assert kinds.count("corpus") == 2
    assert kinds.count("residue") == 2


async def test_worker_extra_context_lands_in_prompt(tmp_path: Path) -> None:
    """history_keeper should see the git summary in its prompt."""
    f = tmp_path / "a.py"
    f.write_text("x = 1\n" * 30, encoding="utf-8")

    spec = CloneSpec(
        id="history_keeper",
        role_short="History Keeper",
        role_prompt="You are the History Keeper.",
    )
    assn = AngleAssignment(clone_spec=spec, paths=[f], scope_bytes=f.stat().st_size)

    clone = Clone(id="history_keeper", repo_id="repo", role="History Keeper")
    clone.new_version(role_prompt=spec.role_prompt)
    stub = _StubClient([PASS1_RESPONSE])

    git_summary = "# Git history summary\n## Authors\n  12 (80.0%)  Alice\n   3 (20.0%)  Bob\n"

    await run_worker(
        assignment=assn,
        clone=clone,
        repo_root=tmp_path,
        repo_id="repo",
        llm=stub,  # type: ignore[arg-type]
        model="stub",
        pass_number=1,
        extra_context=git_summary,
    )

    sent_prompt = stub.calls[0]["prompt"]
    assert "Git history summary" in sent_prompt
    assert "Alice" in sent_prompt


@pytest.mark.parametrize("missing_questions", [True, False])
async def test_pass2_prompt_shape(tmp_path: Path, missing_questions: bool) -> None:
    f = tmp_path / "z.py"
    f.write_text("y = 2\n" * 30, encoding="utf-8")
    spec = CloneSpec(id="x", role_short="X", role_prompt="role")
    assn = AngleAssignment(clone_spec=spec, paths=[f], scope_bytes=f.stat().st_size)
    clone = Clone(id="x", repo_id="r", role="X")
    clone.new_version(role_prompt=spec.role_prompt)

    stub = _StubClient([PASS2_RESPONSE])
    await run_worker(
        assignment=assn,
        clone=clone,
        repo_root=tmp_path,
        repo_id="r",
        llm=stub,  # type: ignore[arg-type]
        model="stub",
        pass_number=2,
        only_paths=[f],
        prior_open_questions=None if missing_questions else ["Q1?", "Q2?"],
    )
    prompt = stub.calls[0]["prompt"]
    if missing_questions:
        # With no prior questions, pass 2 prompt should NOT ask about them
        assert "OPEN QUESTIONS" not in prompt
    else:
        assert "OPEN QUESTIONS" in prompt
        assert "Q1?" in prompt
        assert "Q2?" in prompt
