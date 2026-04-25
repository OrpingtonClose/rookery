"""Datalake store — blob + SQL index.

Two backends in one:
    - ``BlobStore``:  content-addressed immutable blobs on local FS.
    - ``IndexDb``:    DuckDB SQL index described in docs/DATALAKE.md §4.

This file intentionally does NOT implement the Neo4j graph layer
(Stage 4+). When the graph lands, it goes in a sibling module.

Agents: treat the schema in this file as the authoritative one.
Migrations get a version row; ad-hoc ALTER TABLEs in other modules
are forbidden — spawn a sub-agent to write a migration instead.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

_SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL
);

CREATE SEQUENCE IF NOT EXISTS rows_id_seq;

CREATE TABLE IF NOT EXISTS rows (
    id           BIGINT PRIMARY KEY DEFAULT nextval('rows_id_seq'),
    repo_id      TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    row_type     TEXT NOT NULL,
    parent_id    BIGINT,
    related_id   BIGINT,

    content_sha  TEXT,
    inline_text  TEXT,

    file_path    TEXT,
    line_start   INTEGER,
    line_end     INTEGER,
    symbol       TEXT,
    git_sha      TEXT,

    confidence   FLOAT DEFAULT 0.5,
    novelty      FLOAT DEFAULT 0.5,
    specificity  FLOAT DEFAULT 0.5,
    relevance    FLOAT DEFAULT 0.5,
    scored_at    TIMESTAMP,

    verdict_kind TEXT,
    clone_id     TEXT,
    intent       TEXT,
    target_refs  TEXT[],

    consider_for_use BOOLEAN DEFAULT TRUE,
    obsolete_reason  TEXT,
    created_at   TIMESTAMP NOT NULL,
    metadata     JSON
);

CREATE INDEX IF NOT EXISTS idx_rows_repo_run ON rows(repo_id, run_id);
CREATE INDEX IF NOT EXISTS idx_rows_type     ON rows(row_type);
CREATE INDEX IF NOT EXISTS idx_rows_clone    ON rows(clone_id);
CREATE INDEX IF NOT EXISTS idx_rows_path     ON rows(file_path);

CREATE TABLE IF NOT EXISTS clones (
    id              TEXT NOT NULL,
    repo_id         TEXT NOT NULL,
    version         INTEGER NOT NULL,
    prefix_sha256   TEXT NOT NULL,
    size_tokens     BIGINT,
    scorecard       JSON,
    created_at      TIMESTAMP NOT NULL,
    retired_at      TIMESTAMP,
    predecessor     INTEGER,
    PRIMARY KEY (id, repo_id, version)
);

CREATE SEQUENCE IF NOT EXISTS consult_id_seq;

CREATE TABLE IF NOT EXISTS consultations (
    id               BIGINT PRIMARY KEY DEFAULT nextval('consult_id_seq'),
    repo_id          TEXT NOT NULL,
    run_id           TEXT NOT NULL,
    intent           TEXT NOT NULL,
    clone_ids        TEXT[],
    duration_ms      INTEGER,
    budget_exhausted BOOLEAN,
    created_at       TIMESTAMP NOT NULL
);
"""


class BlobStore:
    """Content-addressed immutable blob storage on local FS.

    Layout: ``{root}/blobs/sha256/{first-2-chars}/{full-sha256}.blob``
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        (self.root / "blobs" / "sha256").mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes) -> str:
        """Write ``data`` if absent. Return its sha256 hex."""
        sha = hashlib.sha256(data).hexdigest()
        path = self._path_for(sha)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)  # atomic
        return sha

    def get(self, sha: str) -> bytes:
        return self._path_for(sha).read_bytes()

    def exists(self, sha: str) -> bool:
        return self._path_for(sha).exists()

    def _path_for(self, sha: str) -> Path:
        if len(sha) != 64:
            raise ValueError(f"Expected 64-char sha256, got {len(sha)}")
        return self.root / "blobs" / "sha256" / sha[:2] / f"{sha}.blob"


@dataclass
class IndexDb:
    """DuckDB-backed SQL index. See docs/DATALAKE.md §4."""

    db_path: Path
    conn: duckdb.DuckDBPyConnection

    @classmethod
    def open(cls, db_path: Path) -> IndexDb:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(db_path))
        conn.execute(_SCHEMA_SQL)
        cls._apply_version(conn)
        return cls(db_path=db_path, conn=conn)

    @staticmethod
    def _apply_version(conn: duckdb.DuckDBPyConnection) -> None:
        row = conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO schema_version VALUES (?, ?)",
                [_SCHEMA_VERSION, datetime.now(tz=UTC)],
            )
            return
        if row[0] > _SCHEMA_VERSION:
            raise RuntimeError(
                f"Datalake schema version {row[0]} is newer than this "
                f"code ({_SCHEMA_VERSION}). Upgrade rookery."
            )
        # row[0] < _SCHEMA_VERSION: migrations go here when they exist.

    # -- Writes ---------------------------------------------------------

    def insert_row(self, **fields: object) -> int:
        """Insert a row. Required: repo_id, run_id, row_type.

        ``created_at`` defaults to now if absent. Returns the new row id.
        """
        fields.setdefault("created_at", datetime.now(tz=UTC))
        if "metadata" in fields and not isinstance(fields["metadata"], str):
            fields["metadata"] = json.dumps(fields["metadata"])

        cols = list(fields.keys())
        placeholders = ", ".join("?" for _ in cols)
        sql = f"INSERT INTO rows ({', '.join(cols)}) VALUES ({placeholders}) RETURNING id"
        row = self.conn.execute(sql, list(fields.values())).fetchone()
        assert row is not None
        return int(row[0])

    def register_clone(
        self,
        *,
        clone_id: str,
        repo_id: str,
        version: int,
        prefix_sha256: str,
        size_tokens: int | None,
        scorecard: dict[str, object] | None = None,
        predecessor: int | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO clones
              (id, repo_id, version, prefix_sha256, size_tokens,
               scorecard, created_at, predecessor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                clone_id,
                repo_id,
                version,
                prefix_sha256,
                size_tokens,
                json.dumps(scorecard or {}),
                datetime.now(tz=UTC),
                predecessor,
            ],
        )

    def close(self) -> None:
        self.conn.close()
