"""Named model configurations: which model each role uses in an eval run.

Model ids were checked on 2026-10-25 against the provider model pages
(see `sentinel.observability.pricing` for sources).
"""

from __future__ import annotations

import os
import shutil
from typing import Any

from langchain.chat_models import init_chat_model
from pydantic import BaseModel, ConfigDict, Field

from sentinel.agents import Models
from sentinel.core.config import Role, Settings

ROLES: tuple[Role, ...] = ("supervisor", "specialist", "scorer", "critic")

GPT_MINI = "openai:gpt-5.4-mini"
GPT_NANO = "openai:gpt-5.4-nano"
GEMINI_FLASH = "google_genai:gemini-flash-latest"
GEMINI_FLASH_LITE = "google_genai:gemini-3.5-flash-lite"
OLLAMA_QWEN = "ollama:qwen3:8b"

PROVIDER_KEYS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "google_genai": "GOOGLE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


class ModelConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    supervisor: str
    specialist: str
    scorer: str
    critic: str
    model_kwargs: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Extra init_chat_model arguments per model string, e.g. reasoning=False.",
    )

    def model_for(self, role: Role) -> str:
        return str(getattr(self, role))

    def models(self) -> dict[str, str]:
        return {role: self.model_for(role) for role in ROLES}

    def apply(self, settings: Settings) -> Settings:
        return settings.model_copy(update={f"{r}_model": self.model_for(r) for r in ROLES})

    def missing_requirements(self) -> list[str]:
        """Why this configuration cannot run here (empty when it can)."""
        missing: list[str] = []
        for model in sorted(set(self.models().values())):
            provider = model.split(":", 1)[0]
            key = PROVIDER_KEYS.get(provider)
            if key and not os.environ.get(key):
                missing.append(f"{key} is not set (needed for {model})")
            if provider == "ollama" and shutil.which("ollama") is None:
                missing.append(f"ollama is not installed (needed for {model})")
        return sorted(set(missing))


def build_models(config: ModelConfig, settings: Settings) -> Models:
    """One chat model per role, with the config's per-model arguments."""

    def make(role: Role) -> Any:
        model = config.model_for(role)
        kwargs = {"temperature": settings.temperature, **config.model_kwargs.get(model, {})}
        return init_chat_model(model, **kwargs)

    return Models(
        supervisor=make("supervisor"),
        specialist=make("specialist"),
        scorer=make("scorer"),
        critic=make("critic"),
    )


def _uniform(
    name: str, model: str, description: str, **kwargs: dict[str, dict[str, Any]]
) -> ModelConfig:
    return ModelConfig(
        name=name,
        description=description,
        supervisor=model,
        specialist=model,
        scorer=model,
        critic=model,
        **kwargs,
    )


CONFIGS: dict[str, ModelConfig] = {
    c.name: c
    for c in (
        ModelConfig(
            name="default",
            description="Project default: Gemini Flash specialists, GPT-5.4 mini elsewhere.",
            supervisor=GPT_MINI,
            specialist=GEMINI_FLASH,
            scorer=GPT_MINI,
            critic=GPT_MINI,
        ),
        _uniform("gpt-mini", GPT_MINI, "All roles on GPT-5.4 mini."),
        _uniform("gpt-nano", GPT_NANO, "All roles on GPT-5.4 nano."),
        _uniform("gemini-flash-lite", GEMINI_FLASH_LITE, "All roles on Gemini 3.5 Flash-Lite."),
        _uniform(
            "ollama-qwen",
            OLLAMA_QWEN,
            "All roles on local Qwen3 8B via Ollama, thinking disabled.",
            model_kwargs={OLLAMA_QWEN: {"reasoning": False}},
        ),
    )
}


def get_config(name: str) -> ModelConfig:
    try:
        return CONFIGS[name]
    except KeyError:
        raise KeyError(f"unknown model config {name!r}; choose from {sorted(CONFIGS)}") from None
