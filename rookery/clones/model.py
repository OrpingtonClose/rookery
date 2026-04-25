"""Clone data model — the stable shape of a specialist clone.

A clone is an append-only artifact. This module enforces that:
`CloneVersion.append_segment(...)` is the ONLY write path. Replacing or
reordering a segment is impossible from this API.

See AGENTS.md invariant 1 and docs/ARCHITECTURE.md §3.

Agents modifying this module: the append-only discipline is load-bearing
for prefix-cache validity. Do not add any API that rewrites committed
segments. If you think you need one, spawn a sub-agent to challenge
the requirement first.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

SegmentKind = Literal[
    "role_prompt",  # immutable, set once at clone birth
    "corpus",  # raw code, tests, blame slices
    "residue",  # clone's reasoning trail
    "exemplar",  # worked-example decision
    "evidence",  # external source appended via reflex research
]


@dataclass(frozen=True)
class Segment:
    """A single immutable chunk of a clone's prefix.

    Once created, a Segment is never mutated. A clone's prefix is
    simply the ordered concatenation of its segments.
    """

    kind: SegmentKind
    text: str
    created_at: datetime
    origin: str = ""  # free-text: which phase/worker/query produced this

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def size_chars(self) -> int:
        return len(self.text)


@dataclass
class Scorecard:
    """What the clone is trustworthy about, and what it isn't."""

    domains_strong: list[str] = field(default_factory=list)
    domains_weak: list[str] = field(default_factory=list)
    calibration: dict[str, float] = field(default_factory=dict)
    recent_verdicts: list[str] = field(default_factory=list)


@dataclass
class CloneVersion:
    """A single version of a clone — an append-only list of segments.

    Public API is deliberately narrow:
        - ``append_segment()`` — the only write path
        - ``segments`` — read-only access via property
        - ``prefix_text()`` / ``prefix_sha256()`` — derived
    """

    clone_id: str
    repo_id: str
    version: int
    created_at: datetime
    scorecard: Scorecard = field(default_factory=Scorecard)
    predecessor_version: int | None = None
    _segments: list[Segment] = field(default_factory=list, repr=False)

    @property
    def segments(self) -> tuple[Segment, ...]:
        """Read-only view of segments."""
        return tuple(self._segments)

    def append_segment(
        self,
        *,
        kind: SegmentKind,
        text: str,
        origin: str = "",
    ) -> Segment:
        """Append a segment at the tail. Cannot rewrite existing content.

        This is the only mutation this class permits.
        """
        if not text:
            raise ValueError("Segment text must be non-empty")

        seg = Segment(
            kind=kind,
            text=text,
            created_at=datetime.now(tz=UTC),
            origin=origin,
        )
        self._segments.append(seg)
        return seg

    def prefix_text(self) -> str:
        """The full prefix = ordered concatenation of all segments."""
        return "".join(s.text for s in self._segments)

    def prefix_sha256(self) -> str:
        """Stable hash of the full prefix. Used for cache affinity."""
        h = hashlib.sha256()
        for s in self._segments:
            h.update(s.text.encode("utf-8"))
        return h.hexdigest()

    @property
    def size_chars(self) -> int:
        return sum(s.size_chars for s in self._segments)


@dataclass
class Clone:
    """A logical clone. Has versions; the current version is the active prefix."""

    id: str  # e.g. "invariant_keeper"
    repo_id: str
    role: str  # short human description
    versions: list[CloneVersion] = field(default_factory=list)

    @property
    def current(self) -> CloneVersion:
        if not self.versions:
            raise RuntimeError(f"Clone {self.id!r} has no versions; call materialize() first")
        return self.versions[-1]

    def new_version(
        self,
        *,
        role_prompt: str,
        predecessor: CloneVersion | None = None,
    ) -> CloneVersion:
        """Spawn a new version of this clone.

        A new version starts with exactly one immutable ``role_prompt``
        segment. All further context arrives via ``append_segment``.
        """
        v = CloneVersion(
            clone_id=self.id,
            repo_id=self.repo_id,
            version=(self.versions[-1].version + 1) if self.versions else 1,
            created_at=datetime.now(tz=UTC),
            predecessor_version=predecessor.version if predecessor else None,
        )
        v.append_segment(
            kind="role_prompt",
            text=role_prompt,
            origin="birth",
        )
        self.versions.append(v)
        return v
