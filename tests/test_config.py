"""Config loads from env and fails fast on missing required keys."""

from __future__ import annotations

import pytest

from rookery.config import Config, ConfigError


def test_config_from_env_reads_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROOKERY_MODEL", "deepseek-4-flash")
    monkeypatch.setenv("ROOKERY_API_KEY", "sk-xxx")
    cfg = Config.from_env()
    assert cfg.model == "deepseek-4-flash"
    assert cfg.api_key == "sk-xxx"
    assert cfg.base_url.startswith("https://")


def test_config_fails_fast_on_missing_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ROOKERY_MODEL", "deepseek-4-flash")
    monkeypatch.delenv("ROOKERY_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        Config.from_env()


def test_model_for_resolves_role_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROOKERY_MODEL", "deepseek-4-flash")
    monkeypatch.setenv("ROOKERY_API_KEY", "sk-xxx")
    monkeypatch.setenv("ROOKERY_MODEL_INVARIANT_KEEPER", "deepseek-reasoner")
    cfg = Config.from_env()
    assert cfg.model_for("invariant_keeper") == "deepseek-reasoner"
    assert cfg.model_for("test_keeper") == "deepseek-4-flash"  # no override
