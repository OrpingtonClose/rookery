"""Clone persistence — serialize + rehydrate.

The single most important datalake operation (see docs/DATALAKE.md §6):
a clone must survive the end of the run that produced it. Subsequent
runs rehydrate the clone and pick up where the first left off.

Storage shape:
  blobs/sha256/<aa>/<full>.blob     — one blob per segment (idempotent
                                      via content-addressing)
  clones table row                  — version metadata
  rows table                        — one ``clone_manifest`` row per
                                      version, JSON-metadata holds the
                                      ordered segment sha list
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from rookery.clones.model import Clone, CloneVersion, Scorecard, Segment
from rookery.datalake.store import BlobStore, IndexDb

logger = logging.getLogger(__name__)


@dataclass
class PersistedCloneRef:
    """Pointer to a persisted clone version."""

    clone_id: str
    repo_id: str
    version: int
    prefix_sha256: str
    manifest_row_id: int


def _segment_to_blob(seg: Segment) -> tuple[str, dict[str, Any]]:
    """Return (sha, metadata) for a segment, writing to blob store is
    the caller's job so it can be shared across segments with identical
    text.
    """
    return seg.sha256, {
        "kind": seg.kind,
        "sha": seg.sha256,
        "size_chars": seg.size_chars,
        "created_at": seg.created_at.isoformat(),
        "origin": seg.origin,
    }


def persist_clone_version(
    *,
    clone: Clone,
    version: CloneVersion,
    run_id: str,
    blobs: BlobStore,
    index: IndexDb,
    size_tokens: int | None = None,
) -> PersistedCloneRef:
    """Write every segment's text as a blob and register the version.

    Idempotent: writing the same version twice is safe. Segment blobs
    are content-addressed; the manifest row carries the ordered list.
    """
    segment_metas: list[dict[str, Any]] = []
    for seg in version.segments:
        sha = blobs.put(seg.text.encode("utf-8"))
        _, meta = _segment_to_blob(seg)
        assert meta["sha"] == sha
        segment_metas.append(meta)

    prefix_sha = version.prefix_sha256()

    # Write the manifest row first; its id is the handle we keep.
    manifest_row_id = index.insert_row(
        repo_id=clone.repo_id,
        run_id=run_id,
        row_type="clone_manifest",
        clone_id=clone.id,
        inline_text=f"Clone {clone.id} version {version.version}",
        content_sha=prefix_sha,
        metadata={
            "version": version.version,
            "predecessor_version": version.predecessor_version,
            "segments": segment_metas,
            "scorecard": {
                "domains_strong": version.scorecard.domains_strong,
                "domains_weak": version.scorecard.domains_weak,
                "calibration": version.scorecard.calibration,
            },
            "size_chars": version.size_chars,
        },
    )

    index.register_clone(
        clone_id=clone.id,
        repo_id=clone.repo_id,
        version=version.version,
        prefix_sha256=prefix_sha,
        size_tokens=size_tokens,
        scorecard={
            "domains_strong": version.scorecard.domains_strong,
            "domains_weak": version.scorecard.domains_weak,
            "calibration": version.scorecard.calibration,
            "role": clone.role,
        },
        predecessor=version.predecessor_version,
    )

    logger.info(
        "persisted clone %s v%d: %d segments, %d chars, prefix_sha=%s",
        clone.id,
        version.version,
        len(segment_metas),
        version.size_chars,
        prefix_sha[:16],
    )
    return PersistedCloneRef(
        clone_id=clone.id,
        repo_id=clone.repo_id,
        version=version.version,
        prefix_sha256=prefix_sha,
        manifest_row_id=manifest_row_id,
    )


def load_clone(
    *,
    clone_id: str,
    repo_id: str,
    blobs: BlobStore,
    index: IndexDb,
    version: int | None = None,
) -> Clone:
    """Reconstruct a Clone (with its latest or requested version) from the datalake.

    Raises ``LookupError`` if not found.
    """
    if version is None:
        row = index.conn.execute(
            "SELECT MAX(version) FROM clones WHERE id = ? AND repo_id = ? AND retired_at IS NULL",
            [clone_id, repo_id],
        ).fetchone()
        if not row or row[0] is None:
            raise LookupError(f"No clone {clone_id!r} for repo {repo_id!r} in datalake")
        version = int(row[0])

    # Get the manifest row for this version.
    manifest_row = index.conn.execute(
        """
        SELECT metadata FROM rows
        WHERE row_type = 'clone_manifest'
          AND clone_id = ?
          AND repo_id = ?
          AND json_extract(metadata, '$.version') = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        [clone_id, repo_id, version],
    ).fetchone()
    if not manifest_row:
        raise LookupError(f"Clone {clone_id!r} v{version} registered but manifest row missing")

    meta = json.loads(manifest_row[0]) if isinstance(manifest_row[0], str) else manifest_row[0]

    clone_row = index.conn.execute(
        "SELECT scorecard FROM clones WHERE id = ? AND repo_id = ? AND version = ?",
        [clone_id, repo_id, version],
    ).fetchone()
    scorecard_json = json.loads(clone_row[0]) if clone_row and clone_row[0] else {}
    role = str(scorecard_json.get("role", ""))

    clone = Clone(id=clone_id, repo_id=repo_id, role=role)
    cv = CloneVersion(
        clone_id=clone_id,
        repo_id=repo_id,
        version=version,
        created_at=datetime.now(tz=UTC),
        scorecard=Scorecard(
            domains_strong=list(scorecard_json.get("domains_strong", [])),
            domains_weak=list(scorecard_json.get("domains_weak", [])),
            calibration=dict(scorecard_json.get("calibration", {})),
        ),
        predecessor_version=meta.get("predecessor_version"),
    )

    for seg_meta in meta["segments"]:
        text = blobs.get(seg_meta["sha"]).decode("utf-8")
        cv.append_segment(
            kind=seg_meta["kind"],
            text=text,
            origin=seg_meta.get("origin", ""),
        )

    clone.versions.append(cv)
    return clone
