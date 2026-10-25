from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from sentinel.agents import Models
from sentinel.agents.graph import checkpoint_serde
from sentinel.app.settings import AppSettings
from sentinel.app.wiring import Runtime, open_runtime
from tests.agents.conftest import make_models
from tests.fakes import fake_ports

DATABASE_URL = os.environ.get("SENTINEL_DATABASE_URL")

requires_database = pytest.mark.skipif(not DATABASE_URL, reason="SENTINEL_DATABASE_URL is not set")


def app_settings(**overrides: Any) -> AppSettings:
    """Settings that ignore .env and the process environment's database URL."""
    values: dict[str, Any] = {"database_url": None, **overrides}
    return AppSettings(_env_file=None, **values)  # type: ignore[call-arg]


def runtime_factory(
    *,
    settings: AppSettings | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    models: Callable[[], Models] = make_models,
    callbacks: list[BaseCallbackHandler] | None = None,
) -> Callable[[], AbstractAsyncContextManager[Runtime]]:
    """A runtime with fake ports and fresh scripted models on every open.

    Pass a shared `checkpointer` to simulate two processes resuming the same thread.
    Without one (and without a database URL) each runtime gets its own InMemorySaver.
    """
    settings = settings or app_settings()

    def factory() -> AbstractAsyncContextManager[Runtime]:
        saver = checkpointer
        if saver is None and settings.database_url is None:
            saver = InMemorySaver(serde=checkpoint_serde())
        return open_runtime(
            settings,
            ports=fake_ports(),
            models=models(),
            checkpointer=saver,
            callbacks=callbacks if callbacks is not None else [],
        )

    return factory


@pytest.fixture
def saver() -> InMemorySaver:
    return InMemorySaver(serde=checkpoint_serde())
