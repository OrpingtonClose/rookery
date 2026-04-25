"""Git history extraction — subprocess-based, no GitPython dependency.

Extracts the signals the History Keeper needs:
  - Authorship distribution over recent commits
  - Hotspots (files touched most frequently)
  - Revert patterns (commits that revert earlier commits)
  - Commit cadence

All output is plain text so it can be appended to a clone prefix as
a segment. The goal is that an LLM reading this text can answer
questions like "who authors most of the code" or "has this file been
reverted recently".

Shells out to ``git``; if the repo is not a git checkout the functions
return empty/"not a git repo" markers rather than raising — the
History Keeper should simply have a shorter prefix when git isn't
available.
"""

from __future__ import annotations

import logging
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class GitSummary:
    """Plain-text-renderable summary of a repo's git state."""

    is_git_repo: bool
    head_sha: str = ""
    head_branch: str = ""
    total_commits_scanned: int = 0
    authors_by_count: list[tuple[str, int]] = field(default_factory=list)
    recent_commits: list[dict[str, str]] = field(default_factory=list)
    hotspot_files: list[tuple[str, int]] = field(default_factory=list)
    revert_commits: list[dict[str, str]] = field(default_factory=list)
    error: str = ""

    def render(self, max_lines: int = 400) -> str:
        """Render as a clone-prefix segment.

        The segment is intentionally structured text (not JSON) so the
        LLM can read it naturally while still being deterministic.
        """
        if not self.is_git_repo:
            reason = self.error or "not a git repository"
            return f"# Git history: UNAVAILABLE ({reason})\n"

        lines: list[str] = [
            "# Git history summary",
            f"# HEAD: {self.head_sha[:12]}  branch: {self.head_branch}",
            f"# Scanned {self.total_commits_scanned} commits",
            "",
        ]

        if self.authors_by_count:
            lines.append("## Authors (by commit count, top 15)")
            for author, count in self.authors_by_count[:15]:
                pct = 100 * count / max(self.total_commits_scanned, 1)
                lines.append(f"  {count:5d}  ({pct:4.1f}%)  {author}")
            lines.append("")

        if self.hotspot_files:
            lines.append("## Hotspot files (most-touched, top 30)")
            for path, count in self.hotspot_files[:30]:
                lines.append(f"  {count:4d}  {path}")
            lines.append("")

        if self.revert_commits:
            lines.append("## Revert commits (last 20)")
            for rc in self.revert_commits[:20]:
                lines.append(f"  {rc['sha'][:10]}  {rc['date']}  {rc['subject'][:100]}")
            lines.append("")

        if self.recent_commits:
            lines.append("## Recent commits (last 30)")
            for c in self.recent_commits[:30]:
                subj = c["subject"][:90]
                lines.append(f"  {c['sha'][:10]}  {c['date']}  {c['author'][:20]:20s}  {subj}")
            lines.append("")

        # Guard against absurd renderings — truncate at max_lines.
        if len(lines) > max_lines:
            lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines truncated)"]
        return "\n".join(lines) + "\n"


def _run_git(repo_root: Path, *args: str, timeout: float = 30.0) -> tuple[int, str, str]:
    """Shell out to git with the given args. Returns (rc, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return 127, "", "git binary not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {args[0] if args else ''} timed out"
    return proc.returncode, proc.stdout, proc.stderr


def extract_git_summary(
    repo_root: Path,
    *,
    commit_limit: int = 500,
    hotspot_since_days: int | None = 90,
) -> GitSummary:
    """Extract a git summary from the repo at ``repo_root``.

    Fails soft: if any subcommand errors, the returned summary has
    ``is_git_repo=True`` if we at least confirmed the repo exists, and
    the missing fields are left empty. Never raises.
    """
    rc, out, err = _run_git(repo_root, "rev-parse", "--git-dir")
    if rc != 0:
        return GitSummary(is_git_repo=False, error=err.strip() or "rev-parse failed")

    summary = GitSummary(is_git_repo=True)

    # HEAD sha + branch
    rc, out, _ = _run_git(repo_root, "rev-parse", "HEAD")
    if rc == 0:
        summary.head_sha = out.strip()
    rc, out, _ = _run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if rc == 0:
        summary.head_branch = out.strip()

    # Recent commits. Format: sha\tiso-date\tauthor\tsubject
    # Using %x09 (tab) as field separator so subjects with pipes don't confuse us.
    rc, out, err = _run_git(
        repo_root,
        "log",
        f"-{commit_limit}",
        "--no-merges",
        "--pretty=format:%H%x09%ai%x09%an%x09%s",
    )
    if rc != 0:
        logger.warning("git log failed: %s", err.strip())
        return summary

    commits: list[dict[str, str]] = []
    authors: Counter[str] = Counter()
    reverts: list[dict[str, str]] = []

    for line in out.splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        sha, date, author, subject = parts
        # Short date: ISO has trailing timezone; keep just YYYY-MM-DD
        short_date = date.split(" ", 1)[0]
        commits.append({"sha": sha, "date": short_date, "author": author, "subject": subject})
        authors[author] += 1
        lower = subject.lower()
        if lower.startswith("revert ") or lower.startswith("revert:"):
            reverts.append({"sha": sha, "date": short_date, "author": author, "subject": subject})

    summary.total_commits_scanned = len(commits)
    summary.recent_commits = commits
    summary.authors_by_count = authors.most_common()
    summary.revert_commits = reverts

    # Hotspot files: `git log --name-only --pretty=format:`, then count.
    log_args = [
        "log",
        "--no-merges",
        "--name-only",
        "--pretty=format:",
    ]
    if hotspot_since_days is not None:
        log_args.extend(["--since", f"{hotspot_since_days}.days.ago"])
    rc, out, err = _run_git(repo_root, *log_args)
    if rc == 0:
        file_counts: Counter[str] = Counter()
        for line in out.splitlines():
            path = line.strip()
            if path:
                file_counts[path] += 1
        summary.hotspot_files = file_counts.most_common(100)
    else:
        logger.info("hotspot scan skipped: %s", err.strip())

    return summary
