"""The chat models the graph uses, one per role, injected into `build_graph`."""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel

from sentinel.core.config import Settings
from sentinel.core.llm import get_chat_model


@dataclass(frozen=True)
class Models:
    supervisor: BaseChatModel
    specialist: BaseChatModel
    scorer: BaseChatModel
    critic: BaseChatModel

    @classmethod
    def from_settings(cls, settings: Settings) -> Models:
        return cls(
            supervisor=get_chat_model("supervisor", settings),
            specialist=get_chat_model("specialist", settings),
            scorer=get_chat_model("scorer", settings),
            critic=get_chat_model("critic", settings),
        )
