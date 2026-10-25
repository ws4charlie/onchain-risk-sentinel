"""Settings used only by the runtime (CLI, API): database, embeddings, tracing.

`AppSettings` extends the core `Settings`, so every `SENTINEL_*` variable still applies.
Langfuse keeps its own standard variable names (no `SENTINEL_` prefix).
"""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field, SecretStr

from sentinel.core.config import Settings


class AppSettings(Settings):
    # Postgres: graph checkpoints and the pgvector pattern index. Unset -> in-memory + local.
    database_url: SecretStr | None = Field(
        default=None, description="e.g. postgresql://localhost/sentinel"
    )

    # Embeddings for the pattern knowledge base. "hashing" is offline and needs no key.
    embedding_provider: Literal["google", "hashing"] = "google"
    embedding_model: str = "gemini-embedding-001"

    # Optional Langfuse tracing; enabled when both keys are set.
    langfuse_public_key: str | None = Field(
        default=None, validation_alias=AliasChoices("LANGFUSE_PUBLIC_KEY")
    )
    langfuse_secret_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("LANGFUSE_SECRET_KEY")
    )
    langfuse_host: str | None = Field(default=None, validation_alias=AliasChoices("LANGFUSE_HOST"))

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)
