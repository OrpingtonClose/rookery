"""Verification tools — the ground-truth mechanism.

When two clones disagree on a computable claim, the swarm runs a tool
rather than invoking a third LLM to adjudicate. See docs/ARCHITECTURE.md §5.

This package is intentionally a collection of small, single-purpose
tools with typed inputs and typed outputs. Each tool is an independent
unit; nothing here should depend on the clones or the operator loop.

Agents adding tools: one tool per file, one sub-agent per tool.
Prefer subprocess-calling existing Unix tools (rg, git, pytest) over
reimplementing their behavior in Python.
"""

from __future__ import annotations

__all__: list[str] = []
