"""Rookery — a swarm of specialist clones that understand a codebase.

Public API:

    from rookery import consult, ask, explain, verify, history
    from rookery import SwarmAdvisory, Verdict, PreHookAdvisory

See docs/ARCHITECTURE.md for the shape of things, and AGENTS.md for
the development contract (delegate to sub-agents liberally).
"""

from __future__ import annotations

from rookery.operator.api import (
    PreHookAdvisory,
    SwarmAdvisory,
    Verdict,
    ask,
    consult,
    explain,
    history,
    verify,
)

__all__ = [
    "PreHookAdvisory",
    "SwarmAdvisory",
    "Verdict",
    "ask",
    "consult",
    "explain",
    "history",
    "verify",
]

__version__ = "0.0.1"
