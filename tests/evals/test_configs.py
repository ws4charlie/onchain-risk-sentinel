"""Model configurations: availability checks and model construction (no network)."""

from __future__ import annotations

import pytest
from langchain_ollama import ChatOllama

from sentinel.core.config import Settings
from sentinel.evals.configs import CONFIGS, build_models, get_config


def test_missing_keys_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    missing = CONFIGS["default"].missing_requirements()
    assert any("OPENAI_API_KEY" in m for m in missing)
    assert any("GOOGLE_API_KEY" in m for m in missing)

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert CONFIGS["gpt-nano"].missing_requirements() == []


def test_apply_sets_every_role() -> None:
    settings = CONFIGS["gpt-nano"].apply(Settings(_env_file=None))  # type: ignore[call-arg]
    assert {settings.model_for(r) for r in ("supervisor", "specialist", "scorer", "critic")} == {
        "openai:gpt-5.4-nano"
    }


def test_build_models_passes_per_model_arguments() -> None:
    models = build_models(CONFIGS["ollama-qwen"], Settings(_env_file=None))  # type: ignore[call-arg]
    assert isinstance(models.critic, ChatOllama)
    assert models.critic.model == "qwen3:8b"
    assert models.critic.reasoning is False


def test_unknown_config() -> None:
    with pytest.raises(KeyError, match="unknown model config"):
        get_config("nope")
