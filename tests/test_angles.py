"""Real-path tests for angle detection."""

from __future__ import annotations

from pathlib import Path

from rookery.clones.roster import CloneSpec, default_roster
from rookery.tearing_down.angles import detect_angles, walk_repo


def _make_repo(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "app.py").write_text("def main():\n    return 42\n" * 30, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text(
        "def test_smoke():\n    assert True\n" * 30, encoding="utf-8"
    )
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("should be ignored", encoding="utf-8")
    # Pad past the 500-byte min-scope floor — any real repo will be
    # well above this; the tiny fixture needs filler.
    pyproject_body = (
        '[project]\nname = "x"\nversion = "0"\n'
        + "# filler comment to push past the min-scope floor\n" * 20
    )
    (root / "pyproject.toml").write_text(pyproject_body, encoding="utf-8")


def test_walk_skips_junk_dirs(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    files = walk_repo(tmp_path)
    names = {p.name for p in files}
    assert "app.py" in names
    assert "test_app.py" in names
    assert "pyproject.toml" in names
    assert "junk.js" not in names  # node_modules excluded


def test_detect_angles_picks_expected(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    angles = detect_angles(tmp_path, default_roster())
    ids = {a.clone_spec.id for a in angles}

    # test_keeper is always-relevant (no path_globs) and has enough scope
    assert "test_keeper" in ids
    # build_keeper has path_globs that should match pyproject.toml
    assert "build_keeper" in ids


def test_detect_angles_skips_tiny_scope(tmp_path: Path) -> None:
    # Repo with only one trivial file — everything is below the floor
    (tmp_path / "pyproject.toml").write_text("x\n", encoding="utf-8")

    # A single clone spec with no globs, so it would match if scope were big enough
    spec = CloneSpec(
        id="tiny",
        role_short="x",
        role_prompt="x",
    )
    # No code files at all → no angles
    angles = detect_angles(tmp_path, [spec])
    assert angles == []
