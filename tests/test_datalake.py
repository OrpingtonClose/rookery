"""Real-path tests for the datalake store.

No mocks. We write a real DuckDB and a real blob, close it, reopen,
and verify persistence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rookery.datalake.store import BlobStore, IndexDb


def test_blob_store_put_get_roundtrip(tmp_path: Path) -> None:
    bs = BlobStore(tmp_path)
    sha = bs.put(b"hello rookery")
    assert bs.exists(sha)
    assert bs.get(sha) == b"hello rookery"


def test_blob_store_put_is_idempotent(tmp_path: Path) -> None:
    bs = BlobStore(tmp_path)
    sha_a = bs.put(b"same bytes")
    sha_b = bs.put(b"same bytes")
    assert sha_a == sha_b  # content-addressed: writing twice is a no-op


def test_blob_store_rejects_wrong_length_sha(tmp_path: Path) -> None:
    bs = BlobStore(tmp_path)
    with pytest.raises(ValueError):
        bs._path_for("short")  # noqa: SLF001  — verifying defensive check


def test_index_db_persists_across_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "idx.duckdb"
    idx = IndexDb.open(db_path)
    row_id = idx.insert_row(
        repo_id="r1",
        run_id="run_1",
        row_type="finding",
        inline_text="factor X depends on Y",
    )
    idx.close()

    # Reopen and confirm the row is still there
    idx2 = IndexDb.open(db_path)
    row = idx2.conn.execute("SELECT inline_text FROM rows WHERE id = ?", [row_id]).fetchone()
    idx2.close()

    assert row is not None
    assert row[0] == "factor X depends on Y"


def test_clone_registration(tmp_path: Path) -> None:
    idx = IndexDb.open(tmp_path / "idx.duckdb")
    idx.register_clone(
        clone_id="invariant_keeper",
        repo_id="rookery",
        version=1,
        prefix_sha256="0" * 64,
        size_tokens=1234,
        scorecard={"domains_strong": ["db/models"]},
    )
    row = idx.conn.execute(
        "SELECT version, size_tokens FROM clones "
        "WHERE id = 'invariant_keeper' AND repo_id = 'rookery'"
    ).fetchone()
    idx.close()
    assert row == (1, 1234)


def test_schema_version_row_inserted(tmp_path: Path) -> None:
    idx = IndexDb.open(tmp_path / "idx.duckdb")
    row = idx.conn.execute(
        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
    ).fetchone()
    idx.close()
    assert row is not None
    assert row[0] >= 1
