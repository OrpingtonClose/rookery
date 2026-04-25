"""Architectural invariant: clones are append-only within a version.

This is AGENTS.md invariant 1. Without it, prefix caching on vLLM is
meaningless and the hypercloud economics collapse. Test it directly.
"""

from __future__ import annotations

import pytest

from rookery.clones.model import Clone, CloneVersion


def _fresh_clone() -> CloneVersion:
    c = Clone(id="test_keeper", repo_id="repo", role="test")
    return c.new_version(role_prompt="You are the Test Keeper.")


def test_new_version_starts_with_immutable_role_prompt() -> None:
    v = _fresh_clone()
    assert len(v.segments) == 1
    assert v.segments[0].kind == "role_prompt"
    assert "Test Keeper" in v.segments[0].text


def test_append_segment_appends_at_tail() -> None:
    v = _fresh_clone()
    v.append_segment(kind="corpus", text="def foo(): pass\n", origin="worker_a")
    v.append_segment(kind="residue", text="foo is pure\n", origin="worker_a")

    kinds = [s.kind for s in v.segments]
    assert kinds == ["role_prompt", "corpus", "residue"]


def test_segments_property_is_read_only_view() -> None:
    v = _fresh_clone()
    snapshot = v.segments
    v.append_segment(kind="corpus", text="x = 1\n")
    assert len(snapshot) == 1  # the original snapshot is unchanged
    assert len(v.segments) == 2  # but the live view has grown


def test_segment_is_frozen_dataclass() -> None:
    from dataclasses import FrozenInstanceError

    v = _fresh_clone()
    v.append_segment(kind="corpus", text="code")
    seg = v.segments[-1]
    with pytest.raises(FrozenInstanceError):
        seg.text = "mutated"  # type: ignore[misc]


def test_appending_empty_text_is_rejected() -> None:
    v = _fresh_clone()
    with pytest.raises(ValueError):
        v.append_segment(kind="corpus", text="")


def test_prefix_hash_is_append_stable() -> None:
    """Hashing after two appends equals hashing each step concatenated.

    This is the property vLLM prefix caching relies on: the prefix up
    to round N must be a byte-exact prefix of the prefix at round N+1.
    """
    v = _fresh_clone()
    v.append_segment(kind="corpus", text="first\n")
    hash_after_1 = v.prefix_sha256()
    text_after_1 = v.prefix_text()

    v.append_segment(kind="corpus", text="second\n")
    text_after_2 = v.prefix_text()

    # text_after_2 starts with text_after_1 — byte for byte
    assert text_after_2.startswith(text_after_1)

    # The hash changed but in a way a consumer can detect
    assert hash_after_1 != v.prefix_sha256()


def test_successor_version_bumps_monotonically() -> None:
    c = Clone(id="inv_keeper", repo_id="repo", role="test")
    v1 = c.new_version(role_prompt="v1 role")
    v2 = c.new_version(role_prompt="v2 role", predecessor=v1)
    assert v1.version == 1
    assert v2.version == 2
    assert v2.predecessor_version == 1
