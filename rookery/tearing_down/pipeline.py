"""Comprehension (tear-down) pipeline — Stage 1.

This is the *cold* path. It runs once per repo per major change and
produces the datalake + clone roster the hot path depends on.

The pipeline itself should be lean: its job is to **orchestrate
sub-agents**, not to do the analysis inline. Each angle's worker is a
separate agent. Disagreements between workers are resolved by tool
invocations, not by a meta-LLM.

Agents working on this file: delegate the individual tear-down workers
to sub-agents (one sub-agent per angle). This file should remain a
thin orchestration layer per AGENTS.md.

STATUS: Stage 1 scaffolding. The run() function below returns a typed
result shape but does not yet execute workers. Filling this in is the
first real implementation task.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from rookery.clones.roster import CloneSpec, default_roster
from rookery.config import Config
from rookery.datalake.store import BlobStore, IndexDb

logger = logging.getLogger(__name__)


@dataclass
class TearDownResult:
    """Summary of one tear-down run."""

    run_id: str
    repo_id: str
    started_at: datetime
    finished_at: datetime | None = None
    angles_detected: list[str] = field(default_factory=list)
    clones_materialized: list[str] = field(default_factory=list)
    rows_written: int = 0
    notes: list[str] = field(default_factory=list)


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

    async def run(self) -> TearDownResult:
        """Run the full tear-down pipeline.

        STAGE 1 — SCAFFOLD. The current implementation wires the
        datalake and returns a typed result with detected angles
        populated from the roster. It does NOT yet run the per-angle
        workers or materialize clone prefixes; that is the next
        implementation milestone.
        """
        run_id = datetime.now(tz=UTC).strftime("tearing_%Y%m%d_%H%M%S")
        repo_id = self.repo_id or self.repo_path.name
        started = datetime.now(tz=UTC)

        logger.info("starting tear-down run_id=%s repo_id=%s", run_id, repo_id)

        dl_dir = self.config.datalake_dir
        dl_dir.mkdir(parents=True, exist_ok=True)
        _ = BlobStore(dl_dir)
        index = IndexDb.open(dl_dir / "index.duckdb")

        # Record the run as a single row so the datalake knows it happened.
        index.insert_row(
            repo_id=repo_id,
            run_id=run_id,
            row_type="run_marker",
            inline_text=f"tear-down pipeline started at {started.isoformat()}",
        )

        # Angle detection is a separate sub-agent job (not done here yet).
        # For now, assume one angle per roster entry.
        angles = [spec.id for spec in self.roster]

        result = TearDownResult(
            run_id=run_id,
            repo_id=repo_id,
            started_at=started,
            angles_detected=angles,
            clones_materialized=[],  # filled by sub-agents in the real impl
            rows_written=1,
            notes=[
                "Stage 1 scaffold — worker execution is not yet implemented.",
                "Next step: spawn one sub-agent per angle (see AGENTS.md).",
            ],
        )
        result.finished_at = datetime.now(tz=UTC)
        index.close()
        return result
