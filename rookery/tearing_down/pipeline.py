"""Comprehension (tear-down) pipeline — Stage 1.

Now a real, end-to-end pipeline:

    repo path
      → walk + detect angles (structural, no LLM)
      → for each angle, spawn a worker (parallel, one LLM call per worker)
      → each worker produces a narrative + scorecard
      → clone version is materialized and persisted to the datalake
      → run summary returned to caller

Parallelism is bounded. DeepSeek's concurrent-request limits mean
dumping 8 workers at once is fine; this is set via config.

Agents touching this module: keep orchestration honest. If a worker
fails, log it, attach the failure to the result, and keep going.
A partial datalake is better than no datalake.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from rookery.clones.model import Clone
from rookery.clones.persist import PersistedCloneRef, persist_clone_version
from rookery.clones.roster import CloneSpec, default_roster
from rookery.config import Config
from rookery.datalake.store import BlobStore, IndexDb
from rookery.llm import LLMClient
from rookery.tearing_down.angles import AngleAssignment, detect_angles
from rookery.tearing_down.worker import WorkerResult, run_worker
from rookery.tools.git_history import extract_git_summary

logger = logging.getLogger(__name__)


@dataclass
class TearDownResult:
    """Summary of one tear-down run."""

    run_id: str
    repo_id: str
    started_at: datetime
    finished_at: datetime | None = None
    angles_detected: list[str] = field(default_factory=list)
    clones_materialized: list[PersistedCloneRef] = field(default_factory=list)
    worker_failures: list[tuple[str, str]] = field(default_factory=list)
    rows_written: int = 0
    notes: list[str] = field(default_factory=list)
    total_llm_usage: dict[str, int] = field(default_factory=dict)


@dataclass
class TearDownPipeline:
    """Orchestrates comprehension.

    Minimal API:
        pipe = TearDownPipeline(config=Config.from_env(), repo_path=...)
        result = await pipe.run()
    """

    config: Config
    repo_path: Path
    repo_id: str | None = None
    roster: list[CloneSpec] = field(default_factory=default_roster)
    max_concurrent_workers: int = 4
    # When True, run a second comprehension pass for each clone whose
    # first pass had deferred files AND reported at least one open
    # question. The second pass consumes pass 1's open_questions as
    # targeted guidance and reads from the deferred-file pool.
    do_second_pass: bool = True
    # When True, inject a plain-text git summary (authors, hotspots,
    # recent commits, revert markers) into history_keeper's pass 1
    # prompt. Safe to leave on — falls back silently if not a git repo.
    feed_git_history: bool = True

    async def run(self) -> TearDownResult:
        run_id = datetime.now(tz=UTC).strftime("tearing_%Y%m%d_%H%M%S")
        repo_id = self.repo_id or self.repo_path.name
        started = datetime.now(tz=UTC)

        logger.info("tear-down start run_id=%s repo=%s", run_id, repo_id)

        dl_dir = self.config.datalake_dir
        dl_dir.mkdir(parents=True, exist_ok=True)
        blobs = BlobStore(dl_dir)
        index = IndexDb.open(dl_dir / "index.duckdb")

        try:
            # Mark the run.
            index.insert_row(
                repo_id=repo_id,
                run_id=run_id,
                row_type="run_marker",
                inline_text=f"tear-down started at {started.isoformat()}",
            )

            # 1. Angle detection (deterministic, no LLM)
            assignments = detect_angles(self.repo_path, self.roster)
            if not assignments:
                logger.warning(
                    "no angles detected under %s; nothing to materialize",
                    self.repo_path,
                )
                return TearDownResult(
                    run_id=run_id,
                    repo_id=repo_id,
                    started_at=started,
                    finished_at=datetime.now(tz=UTC),
                    notes=["no code files found under repo root"],
                )

            logger.info(
                "%d angles selected: %s",
                len(assignments),
                ", ".join(a.clone_spec.id for a in assignments),
            )

            # 2. Run workers with bounded concurrency
            result = TearDownResult(
                run_id=run_id,
                repo_id=repo_id,
                started_at=started,
                angles_detected=[a.clone_spec.id for a in assignments],
            )

            # Optional: extract git summary once so it can be injected
            # into history_keeper's pass 1 prompt. Other clones get a
            # shorter repo-level header but don't need the full history.
            git_summary_text = ""
            if self.feed_git_history:
                gs = extract_git_summary(self.repo_path)
                git_summary_text = gs.render()
                logger.info(
                    "git history: %s, %d commits scanned",
                    "ok" if gs.is_git_repo else "unavailable",
                    gs.total_commits_scanned,
                )

            totals: dict[str, int] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "cache_hit_tokens": 0,
                "cache_miss_tokens": 0,
            }

            async with LLMClient(
                base_url=self.config.base_url,
                api_key=self.config.api_key,
                default_model=self.config.model,
            ) as llm:
                sem = asyncio.Semaphore(self.max_concurrent_workers)

                # -- Pass 1 ----------------------------------------------
                async def _pass1(
                    assn: AngleAssignment,
                ) -> tuple[AngleAssignment, Clone, WorkerResult] | BaseException:
                    async with sem:
                        clone = Clone(
                            id=assn.clone_spec.id,
                            repo_id=repo_id,
                            role=assn.clone_spec.role_short,
                        )
                        clone.new_version(role_prompt=assn.clone_spec.role_prompt)
                        extra = git_summary_text if assn.clone_spec.id == "history_keeper" else ""
                        try:
                            wres = await run_worker(
                                assignment=assn,
                                clone=clone,
                                repo_root=self.repo_path,
                                repo_id=repo_id,
                                llm=llm,
                                model=self.config.model_for(assn.clone_spec.id),
                                pass_number=1,
                                extra_context=extra,
                            )
                            return assn, clone, wres
                        except Exception as exc:  # noqa: BLE001
                            logger.exception(
                                "worker %s pass 1 failed",
                                assn.clone_spec.id,
                            )
                            return exc

                pass1_outcomes = await asyncio.gather(
                    *(_pass1(a) for a in assignments),
                    return_exceptions=False,
                )

                pass1_good: list[tuple[AngleAssignment, Clone, WorkerResult]] = []
                for assn, outcome in zip(assignments, pass1_outcomes, strict=True):
                    if isinstance(outcome, BaseException):
                        result.worker_failures.append(
                            (f"{assn.clone_spec.id}:pass1", repr(outcome))
                        )
                        continue
                    _, _, wres = outcome  # type: ignore[assignment]
                    pass1_good.append(outcome)  # type: ignore[arg-type]
                    for k in totals:
                        totals[k] += wres.llm_usage.get(k, 0)

                # -- Pass 2 ----------------------------------------------
                # Eligible: pass 1 had deferred files AND produced at
                # least one open_question. Read the deferred files,
                # chasing the prior pass's open questions.
                pass2_ran: list[str] = []
                if self.do_second_pass:

                    async def _pass2(
                        bundle: tuple[AngleAssignment, Clone, WorkerResult],
                    ) -> tuple[str, WorkerResult | None, BaseException | None]:
                        assn, clone, pass1 = bundle
                        open_qs = pass1.scorecard.get("open_questions") or []
                        if not pass1.files_skipped_for_budget or not open_qs:
                            return assn.clone_spec.id, None, None
                        async with sem:
                            try:
                                wres = await run_worker(
                                    assignment=assn,
                                    clone=clone,
                                    repo_root=self.repo_path,
                                    repo_id=repo_id,
                                    llm=llm,
                                    model=self.config.model_for(assn.clone_spec.id),
                                    pass_number=2,
                                    only_paths=pass1.files_skipped_for_budget,
                                    prior_open_questions=[str(q) for q in open_qs],
                                )
                                return assn.clone_spec.id, wres, None
                            except Exception as exc:  # noqa: BLE001
                                logger.exception(
                                    "worker %s pass 2 failed",
                                    assn.clone_spec.id,
                                )
                                return assn.clone_spec.id, None, exc

                    pass2_outcomes = await asyncio.gather(
                        *(_pass2(b) for b in pass1_good),
                        return_exceptions=False,
                    )
                    for clone_id, wres2, exc in pass2_outcomes:
                        if exc is not None:
                            result.worker_failures.append((f"{clone_id}:pass2", repr(exc)))
                        elif wres2 is not None:
                            pass2_ran.append(clone_id)
                            for k in totals:
                                totals[k] += wres2.llm_usage.get(k, 0)

                if pass2_ran:
                    result.notes.append(f"pass 2 ran for {len(pass2_ran)}: " + ", ".join(pass2_ran))

            # 3. Persist clones (both passes accumulated on each clone)
            for _, clone, _ in pass1_good:
                persisted = persist_clone_version(
                    clone=clone,
                    version=clone.current,
                    run_id=run_id,
                    blobs=blobs,
                    index=index,
                    size_tokens=clone.current.size_chars // 3,
                )
                result.clones_materialized.append(persisted)
                result.rows_written += 1

            result.total_llm_usage = totals
            result.finished_at = datetime.now(tz=UTC)
            result.notes.insert(
                0,
                f"materialized {len(result.clones_materialized)} clones, "
                f"{len(result.worker_failures)} failures",
            )
            return result
        finally:
            index.close()
