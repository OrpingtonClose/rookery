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

            async with LLMClient(
                base_url=self.config.base_url,
                api_key=self.config.api_key,
                default_model=self.config.model,
            ) as llm:
                sem = asyncio.Semaphore(self.max_concurrent_workers)

                async def _one(assn: AngleAssignment) -> WorkerResult | BaseException:
                    async with sem:
                        clone = Clone(
                            id=assn.clone_spec.id,
                            repo_id=repo_id,
                            role=assn.clone_spec.role_short,
                        )
                        clone.new_version(role_prompt=assn.clone_spec.role_prompt)
                        try:
                            return await run_worker(
                                assignment=assn,
                                clone=clone,
                                repo_root=self.repo_path,
                                repo_id=repo_id,
                                llm=llm,
                                model=self.config.model_for(assn.clone_spec.id),
                            ), clone  # type: ignore[return-value]
                        except Exception as exc:  # noqa: BLE001 — top-level isolation
                            logger.exception(
                                "worker %s failed",
                                assn.clone_spec.id,
                            )
                            return exc

                outcomes = await asyncio.gather(
                    *(_one(a) for a in assignments),
                    return_exceptions=False,
                )

            # 3. Persist successful clones
            totals = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "cache_hit_tokens": 0,
                "cache_miss_tokens": 0,
            }
            for assn, outcome in zip(assignments, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    result.worker_failures.append((assn.clone_spec.id, repr(outcome)))
                    continue

                # The happy-path return is (WorkerResult, Clone) — see _one.
                worker_result, clone = outcome  # type: ignore[assignment]
                persisted = persist_clone_version(
                    clone=clone,
                    version=clone.current,
                    run_id=run_id,
                    blobs=blobs,
                    index=index,
                    # Rough token estimate: chars / 3 (empirical for code).
                    size_tokens=clone.current.size_chars // 3,
                )
                result.clones_materialized.append(persisted)
                result.rows_written += 1
                for k in totals:
                    totals[k] += worker_result.llm_usage.get(k, 0)

            result.total_llm_usage = totals
            result.finished_at = datetime.now(tz=UTC)
            result.notes.append(
                f"materialized {len(result.clones_materialized)} clones, "
                f"{len(result.worker_failures)} failures"
            )
            return result
        finally:
            index.close()
