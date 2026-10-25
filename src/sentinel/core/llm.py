"""Model factory: one place that turns a role into a configured chat model.

Provider API keys are read by each provider package from its standard
environment variable (OPENAI_API_KEY, GOOGLE_API_KEY, ANTHROPIC_API_KEY).
"""

from __future__ import annotations

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from sentinel.core.config import Role, Settings


def get_chat_model(role: Role, settings: Settings | None = None) -> BaseChatModel:
    settings = settings or Settings()
    return init_chat_model(settings.model_for(role), temperature=settings.temperature)
