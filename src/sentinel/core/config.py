"""Runtime settings, loaded from environment variables (prefix SENTINEL_) and .env."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Role = Literal["supervisor", "specialist", "scorer", "critic"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SENTINEL_", env_file=".env", extra="ignore")

    # Model per role, as "<provider>:<model>" strings for langchain's init_chat_model.
    supervisor_model: str = "openai:gpt-5.4-mini"
    specialist_model: str = "openrouter:google/gemini-3.8-flash"
    scorer_model: str = "openai:gpt-5.4-mini"
    critic_model: str = "openai:gpt-5.4-mini"
    temperature: float = 0.0

    # Graph limits.
    max_steps: int = Field(default=8, ge=1, description="Max supervisor routing decisions.")
    max_revisions: int = Field(default=1, ge=0, description="Max critic send-backs.")
    review_threshold: int = Field(
        default=60, ge=0, le=100, description="Score at or above which non-allow verdicts pause."
    )

    # Data source keys (optional; adapters raise a clear error when a key is required).
    etherscan_api_key: SecretStr | None = None

    def model_for(self, role: Role) -> str:
        return {
            "supervisor": self.supervisor_model,
            "specialist": self.specialist_model,
            "scorer": self.scorer_model,
            "critic": self.critic_model,
        }[role]
