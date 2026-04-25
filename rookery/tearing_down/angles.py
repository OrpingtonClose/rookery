"""Angle detection — structural, deterministic.

Scans a repo and decides which clones from the roster are worth
materializing. Uses fast filesystem walks + path-glob matching; no
LLM calls here. The LLM work happens *inside* each selected angle's
worker.

Design: a clone is selected when either
  (a) its ``path_globs`` match ≥1 file in the repo, or
  (b) the roster entry declares no path_globs (meaning "always
      relevant") AND the repo has ≥1 code file.

Plus a minimum-signal floor: we refuse to select a clone that would
only see trivial amounts of content (< 500 bytes across its scope).
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from pathlib import Path

from rookery.clones.roster import CloneSpec

logger = logging.getLogger(__name__)


# File extensions we treat as "code content" for scope matching.
_CODE_EXTS = {
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".kts",
    ".scala",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".rb",
    ".php",
    ".swift",
    ".m",
    ".mm",
    ".sql",
    ".proto",
    ".graphql",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
}

# Directories we never descend into.
_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".env",
    "dist",
    "build",
    "target",
    ".next",
    ".nuxt",
    ".ruff_cache",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    ".idea",
    ".vscode",
    "coverage",
    "htmlcov",
}

# Minimum bytes a clone's scope must see to be worth materializing.
_MIN_SCOPE_BYTES = 500


@dataclass
class AngleAssignment:
    """A clone that will be materialized, plus the paths it owns."""

    clone_spec: CloneSpec
    paths: list[Path] = field(default_factory=list)
    scope_bytes: int = 0


def walk_repo(root: Path) -> list[Path]:
    """Return all code-content files under ``root``, skipping junk dirs.

    This is the canonical repo scan. Everything else reads from this
    list. Sorted for stability across runs.
    """
    out: list[Path] = []
    for path in _iter_code_files(root):
        out.append(path)
    out.sort()
    return out


def _iter_code_files(root: Path):
    for entry in sorted(root.iterdir()):
        name = entry.name
        if name in _SKIP_DIRS or name.startswith("."):
            # Skip dotdirs except common config-bearing ones would be nice
            # but path-scoped clones can still match via path_globs.
            continue
        if entry.is_dir():
            yield from _iter_code_files(entry)
        elif entry.is_file() and entry.suffix in _CODE_EXTS:
            yield entry


def _matches_any_glob(path: Path, root: Path, globs: tuple[str, ...]) -> bool:
    """True if ``path`` (relative to ``root``) matches any of ``globs``.

    Globs use shell semantics; ``**`` matches any number of directories.
    """
    rel = path.relative_to(root).as_posix()
    for g in globs:
        # Manual ``**`` handling: fnmatch treats ``**`` the same as ``*``.
        # For our purposes, convert ``a/**/b`` and ``a/**`` to a regex-like
        # fnmatch pattern.
        pattern = g.replace("**/", "*").replace("**", "*")
        if fnmatch.fnmatch(rel, pattern):
            return True
        # Also try matching the basename for patterns like "pyproject.toml"
        if "/" not in g and fnmatch.fnmatch(path.name, g):
            return True
    return False


def detect_angles(
    repo_root: Path,
    roster: list[CloneSpec],
) -> list[AngleAssignment]:
    """Pick which clones to materialize and what files each one owns.

    Returns one ``AngleAssignment`` per selected clone. Clones that
    don't meet the minimum-scope floor are dropped (and logged).
    """
    all_files = walk_repo(repo_root)
    if not all_files:
        logger.warning("no code files found under %s", repo_root)
        return []

    assignments: list[AngleAssignment] = []
    for spec in roster:
        if spec.path_globs:
            matched = [p for p in all_files if _matches_any_glob(p, repo_root, spec.path_globs)]
        else:
            # No globs = always-relevant clone; owns the full repo but
            # will prioritize by its own heuristics inside the worker.
            matched = list(all_files)

        scope_bytes = sum(_safe_size(p) for p in matched)
        if scope_bytes < _MIN_SCOPE_BYTES:
            logger.info(
                "skipping clone %s: scope=%d bytes (< %d floor)",
                spec.id,
                scope_bytes,
                _MIN_SCOPE_BYTES,
            )
            continue

        assignments.append(
            AngleAssignment(
                clone_spec=spec,
                paths=matched,
                scope_bytes=scope_bytes,
            )
        )

    return assignments


def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0
