"""End-to-end tests for worker + clone persistence, real paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from rookery.clones.model import Clone
from rookery.clones.persist import load_clone, persist_clone_version
from rookery.clones.roster import CloneSpec
from rookery.datalake.store import BlobStore, IndexDb
from rookery.tearing_down.angles import AngleAssignment
from rookery.tearing_down.worker import _narrative_without_json, _pack_sources, _parse_scorecard


def test_narrative_without_json_strips_trailing_block() -> None:
    text = 'prose paragraph\n\nmore prose\n\n```json\n{"a": 1}\n```'
    assert _narrative_without_json(text) == "prose paragraph\n\nmore prose"


def test_narrative_without_json_returns_all_when_no_json() -> None:
    text = "just prose\n"
    assert _narrative_without_json(text) == "just prose"


def test_parse_scorecard_picks_last_fence() -> None:
    text = (
        "prose with an inline ```json``` mention\n"
        "more prose\n"
        "```json\n"
        '{"domains_strong": ["db/models"], "estimated_coverage": 0.7}\n'
        "```\n"
    )
    sc = _parse_scorecard(text)
    assert sc == {"domains_strong": ["db/models"], "estimated_coverage": 0.7}


def test_parse_scorecard_tolerates_malformed() -> None:
    assert _parse_scorecard("no fence here") == {}
    assert _parse_scorecard("```json\n{not valid\n```") == {}


def test_pack_sources_respects_budget(tmp_path: Path) -> None:
    # 3 files, two small (fit), one big (excluded)
    a = tmp_path / "a.py"
    a.write_text("A" * 100, encoding="utf-8")
    b = tmp_path / "b.py"
    b.write_text("B" * 100, encoding="utf-8")
    c = tmp_path / "c.py"
    c.write_text("C" * 5000, encoding="utf-8")

    packed, incl, excl = _pack_sources([a, b, c], tmp_path, budget=300)
    names_incl = {p.name for p in incl}
    names_excl = {p.name for p in excl}
    assert "a.py" in names_incl and "b.py" in names_incl
    assert "c.py" in names_excl
    assert len(packed) <= 400  # with headers, a bit over raw budget


def test_persist_and_load_roundtrip(tmp_path: Path) -> None:
    dl_root = tmp_path / "dl"
    dl_root.mkdir()
    blobs = BlobStore(dl_root)
    index = IndexDb.open(dl_root / "index.duckdb")

    clone = Clone(id="invariant_keeper", repo_id="repo1", role="guards data model")
    v = clone.new_version(role_prompt="You are the Invariant Keeper for repo1.")
    v.append_segment(kind="corpus", text="# files: a.py, b.py\n", origin="test")
    v.append_segment(kind="residue", text="The key invariant is X.\n", origin="test")
    v.scorecard.domains_strong = ["schemas", "migrations"]
    v.scorecard.domains_weak = ["runtime validation"]
    v.scorecard.calibration["estimated_coverage"] = 0.6

    ref = persist_clone_version(
        clone=clone,
        version=v,
        run_id="run_test",
        blobs=blobs,
        index=index,
        size_tokens=v.size_chars // 3,
    )
    assert ref.prefix_sha256 == v.prefix_sha256()
    index.close()

    # Reopen fresh — the load must work from disk state alone
    idx2 = IndexDb.open(dl_root / "index.duckdb")
    loaded = load_clone(
        clone_id="invariant_keeper",
        repo_id="repo1",
        blobs=blobs,
        index=idx2,
    )
    idx2.close()

    assert loaded.id == "invariant_keeper"
    assert loaded.role == "guards data model"
    assert loaded.current.version == 1
    # Prefix bytes are exactly preserved — this is the critical
    # property for prefix caching to survive persistence.
    assert loaded.current.prefix_text() == v.prefix_text()
    assert loaded.current.prefix_sha256() == v.prefix_sha256()
    assert loaded.current.scorecard.domains_strong == ["schemas", "migrations"]
    assert loaded.current.scorecard.calibration["estimated_coverage"] == 0.6


def test_load_clone_missing_raises(tmp_path: Path) -> None:
    dl_root = tmp_path / "dl"
    dl_root.mkdir()
    blobs = BlobStore(dl_root)
    index = IndexDb.open(dl_root / "index.duckdb")
    with pytest.raises(LookupError):
        load_clone(
            clone_id="nope",
            repo_id="nope",
            blobs=blobs,
            index=index,
        )
    index.close()


def test_assignment_dataclass_defaults() -> None:
    spec = CloneSpec(id="x", role_short="r", role_prompt="p")
    a = AngleAssignment(clone_spec=spec)
    assert a.paths == []
    assert a.scope_bytes == 0
