"""Rookery config — one place to read environment into typed settings.

Fail-fast on misconfiguration. Do not silently default API keys or
model names; the operator needs to know when something is wrong before
a hot-path budget is burned on a bad request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    return val if val else None


def _env_required(name: str) -> str:
    val = _env(name)
    if not val:
        raise ConfigError(f"Environment variable {name} is required. See .env.example.")
    return val


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a float, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    """All runtime configuration, read once at startup."""

    # Model — default for every agent in the system
    model: str
    base_url: str
    api_key: str

    # Per-role overrides (empty = use `model`)
    model_invariant_keeper: str | None
    model_concurrency_keeper: str | None
    model_critic: str | None

    # Datalake
    datalake_dir: Path

    # Hot-path budgets
    prehook_budget_s: float
    critic_budget_s: float
    router_budget_ms: int

    # Clone sizing
    clone_target_tokens: int
    clone_cap_tokens: int

    # Optional local vLLM
    vllm_base: str | None
    vllm_model: str | None

    # Observability
    log_level: str

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            model=_env_required("ROOKERY_MODEL"),
            base_url=_env("ROOKERY_BASE_URL", "https://api.deepseek.com/v1") or "",
            api_key=_env_required("ROOKERY_API_KEY"),
            model_invariant_keeper=_env("ROOKERY_MODEL_INVARIANT_KEEPER"),
            model_concurrency_keeper=_env("ROOKERY_MODEL_CONCURRENCY_KEEPER"),
            model_critic=_env("ROOKERY_MODEL_CRITIC"),
            datalake_dir=Path(_env("ROOKERY_DATALAKE_DIR", ".rookery") or ".rookery"),
            prehook_budget_s=_env_float("ROOKERY_PREHOOK_BUDGET", 10.0),
            critic_budget_s=_env_float("ROOKERY_CRITIC_BUDGET", 15.0),
            router_budget_ms=_env_int("ROOKERY_ROUTER_BUDGET_MS", 200),
            clone_target_tokens=_env_int("ROOKERY_CLONE_TARGET_TOKENS", 500_000),
            clone_cap_tokens=_env_int("ROOKERY_CLONE_CAP_TOKENS", 950_000),
            vllm_base=_env("ROOKERY_VLLM_BASE"),
            vllm_model=_env("ROOKERY_VLLM_MODEL"),
            log_level=_env("ROOKERY_LOG_LEVEL", "INFO") or "INFO",
        )

    def model_for(self, role: str) -> str:
        """Resolve the model name for a given clone role.

        Role-specific overrides take precedence; fall back to the global
        default. Roles with no override return ``self.model``.
        """
        override_map = {
            "invariant_keeper": self.model_invariant_keeper,
            "concurrency_keeper": self.model_concurrency_keeper,
            "critic": self.model_critic,
        }
        return override_map.get(role) or self.model
