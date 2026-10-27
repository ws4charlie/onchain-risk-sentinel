"""Model factory: one place that turns a role into a configured chat model.

Provider API keys are read by each provider package from its standard
environment variable (OPENAI_API_KEY, GOOGLE_API_KEY, ANTHROPIC_API_KEY).
`openrouter:<vendor>/<model>` strings go through OpenRouter's OpenAI-compatible
API with OPENROUTER_API_KEY.
"""

from __future__ import annotations

import os
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from sentinel.core.config import Role, Settings

OPENROUTER_PREFIX = "openrouter:"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def chat_model(model: str, **kwargs: Any) -> BaseChatModel:
    """Build a chat model from a "<provider>:<model>" string."""
    if model.startswith(OPENROUTER_PREFIX):
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError(f"OPENROUTER_API_KEY is not set (needed for {model})")
        return ChatOpenAI(
            model=model.removeprefix(OPENROUTER_PREFIX),
            base_url=OPENROUTER_BASE_URL,
            api_key=key,
            **kwargs,
        )
    built: BaseChatModel = init_chat_model(model, **kwargs)
    return built


def get_chat_model(role: Role, settings: Settings | None = None) -> BaseChatModel:
    settings = settings or Settings()
    return chat_model(settings.model_for(role), temperature=settings.temperature)
