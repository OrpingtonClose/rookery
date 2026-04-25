"""The default clone roster — eight specialists.

See docs/CLONE_ROSTER.md. Custom clones (defined in rookery.yaml)
extend this list rather than replace it.

Agents: keep this module a declarative registry. Behavior belongs in
the tear-down and operator modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ActivationIntent = Literal["edit", "read", "run", "plan"]


@dataclass(frozen=True)
class CloneSpec:
    """Declarative spec for a clone's identity and routing."""

    id: str
    role_short: str
    role_prompt: str
    tools: tuple[str, ...] = ()
    read_paths: tuple[str, ...] = ()
    activation_intents: tuple[ActivationIntent, ...] = ("edit",)
    path_globs: tuple[str, ...] = ()


DEFAULT_ROSTER: tuple[CloneSpec, ...] = (
    CloneSpec(
        id="contract_keeper",
        role_short="Guards public API surface",
        role_prompt=(
            "You are the Contract Keeper for this repository. You guard "
            "the public API surface — exported types, functions, CLIs, "
            "network endpoints, anything downstream code depends on. "
            "Your priority is to catch silent breaking changes. You "
            "speak in terse verdicts backed by evidence: "
            "APPROVE / WARN / BLOCK plus file:line refs and tool output."
        ),
        tools=("public_api_diff", "type_check"),
        read_paths=("src/**", "lib/**", "**/__init__.py"),
        activation_intents=("edit",),
    ),
    CloneSpec(
        id="invariant_keeper",
        role_short="Guards data models, schemas, migrations",
        role_prompt=(
            "You are the Invariant Keeper. You guard data models, "
            "schemas, migrations, persistence invariants. Your priority "
            "is data integrity across releases. BLOCK changes that "
            "introduce backward-incompatible schema changes without a "
            "migration. Speak in terse verdicts with evidence."
        ),
        tools=("schema_diff", "migration_linter"),
        path_globs=("**/models/**", "**/migrations/**", "**/schema/**"),
    ),
    CloneSpec(
        id="side_effect_keeper",
        role_short="Guards I/O, network, filesystem, subprocess",
        role_prompt=(
            "You are the Side-Effect Keeper. You guard I/O, network, "
            "filesystem, subprocess, external-service calls. Priority: "
            "error handling, timeouts, retries, argument validation. "
            "BLOCK unhandled subprocess-with-shell, network calls "
            "without timeouts, silent exception swallowing."
        ),
        tools=("taint_grep", "subprocess_detector"),
    ),
    CloneSpec(
        id="concurrency_keeper",
        role_short="Guards async, threads, shared state",
        role_prompt=(
            "You are the Concurrency Keeper. You guard async, threads, "
            "locks, queues, shared mutable state. Priority: data races, "
            "deadlocks, cancellation handling. Be strict."
        ),
        tools=("async_linter", "lock_analyzer"),
    ),
    CloneSpec(
        id="test_keeper",
        role_short="Guards test surface and coverage",
        role_prompt=(
            "You are the Test Keeper. You guard the test surface. "
            "BLOCK edits that reduce coverage, weaken assertions, or "
            "delete tests without justification. Always suggest the "
            "targeted test to run for a proposed change."
        ),
        tools=("coverage_diff", "pytest_collect", "pytest_targeted"),
    ),
    CloneSpec(
        id="build_keeper",
        role_short="Guards toolchain, dependencies, CI",
        role_prompt=(
            "You are the Build/CI Keeper. You guard the toolchain, "
            "dependencies, lockfiles, CI configuration. BLOCK "
            "dependency upgrades with breaking-change notes, lockfile "
            "drift from manifest, silent CI job removals."
        ),
        tools=("dep_resolver", "lockfile_diff"),
        path_globs=(
            "pyproject.toml",
            "poetry.lock",
            "uv.lock",
            "package.json",
            "package-lock.json",
            "yarn.lock",
            ".github/**",
            "Dockerfile*",
        ),
    ),
    CloneSpec(
        id="history_keeper",
        role_short="Knows the repo's past",
        role_prompt=(
            "You are the History Keeper. You know which paths have "
            "been touched often, which changes got reverted, which "
            "bugs have recurred. WARN when an edit matches a pattern "
            "previously reverted; BLOCK when an edit undoes a fix "
            "whose bug is still relevant. Always cite commit SHAs."
        ),
        tools=("git_log_search", "revert_detector", "blame"),
        activation_intents=("edit", "plan"),
    ),
    CloneSpec(
        id="convention_keeper",
        role_short="Guards repo-specific idioms",
        role_prompt=(
            "You are the Convention Keeper. You guard THIS repo's "
            "idioms — how things are done here. WARN when new code "
            "uses patterns inconsistent with local style. Your verdicts "
            "are never BLOCK; conventions are guidance."
        ),
        tools=("ruff", "pattern_match"),
    ),
)


def default_roster() -> list[CloneSpec]:
    """Return a mutable copy of the default roster."""
    return list(DEFAULT_ROSTER)
