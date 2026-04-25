"""Real-path tests for git history extraction.

These tests operate on actual git repos created in tmp_path via real
``git`` subprocess calls. No mocks.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rookery.tools.git_history import extract_git_summary


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
    )


def _make_git_repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")


def test_extract_git_summary_non_repo(tmp_path: Path) -> None:
    summary = extract_git_summary(tmp_path)
    assert not summary.is_git_repo
    assert summary.error  # some reason reported
    assert "UNAVAILABLE" in summary.render()


def test_extract_git_summary_real_repo(tmp_path: Path) -> None:
    pytest.importorskip("pytest")  # trivially always available

    repo = tmp_path / "proj"
    repo.mkdir()
    _make_git_repo(repo)

    # Create two authors with different commit counts so the
    # authorship histogram has something meaningful to report.
    (repo / "a.py").write_text("print('a')\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "feat: add a")

    _git(repo, "config", "user.email", "alice@example.com")
    _git(repo, "config", "user.name", "Alice")
    for i in range(3):
        (repo / f"b{i}.py").write_text(f"x = {i}\n")
        _git(repo, "add", f"b{i}.py")
        _git(repo, "commit", "-q", "-m", f"feat: b{i}")

    _git(repo, "config", "user.email", "bob@example.com")
    _git(repo, "config", "user.name", "Bob")
    (repo / "c.py").write_text("y = 0\n")
    _git(repo, "add", "c.py")
    _git(repo, "commit", "-q", "-m", "Revert feat: b2")

    summary = extract_git_summary(repo, hotspot_since_days=None)

    assert summary.is_git_repo
    assert summary.head_sha  # non-empty
    assert summary.total_commits_scanned >= 5
    authors = dict(summary.authors_by_count)
    assert authors.get("Alice", 0) >= 3
    assert authors.get("Bob", 0) >= 1
    # The revert commit must be picked up
    assert any("Revert" in r["subject"] for r in summary.revert_commits)

    rendered = summary.render()
    assert "Git history summary" in rendered
    assert "Alice" in rendered
    assert "Revert" in rendered


def test_extract_git_summary_hotspots(tmp_path: Path) -> None:
    repo = tmp_path / "h"
    repo.mkdir()
    _make_git_repo(repo)

    # Touch the same file many times → must appear at top of hotspots
    target = repo / "hot.py"
    for i in range(8):
        target.write_text(f"x = {i}\n")
        _git(repo, "add", "hot.py")
        _git(repo, "commit", "-q", "-m", f"update {i}")

    (repo / "cold.py").write_text("c = 1\n")
    _git(repo, "add", "cold.py")
    _git(repo, "commit", "-q", "-m", "add cold")

    summary = extract_git_summary(repo, hotspot_since_days=None)
    assert summary.hotspot_files
    top_file, top_count = summary.hotspot_files[0]
    assert top_file == "hot.py"
    assert top_count >= 8
