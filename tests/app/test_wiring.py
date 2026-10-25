"""Wiring tests: checkpointer choice, callbacks seam and run metadata."""

from __future__ import annotations

import sys
import types
from typing import Any
from uuid import UUID

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.memory import InMemorySaver

from sentinel.app.runner import read_case, stream_case
from sentinel.app.wiring import build_checkpointer, default_callbacks
from sentinel.core.schemas import Case
from tests.app.conftest import app_settings, runtime_factory
from tests.fakes import CLEAN


class RecordingHandler(BaseCallbackHandler):
    def __init__(self) -> None:
        self.metadata: list[dict[str, Any]] = []

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.metadata.append(metadata or {})


async def test_in_memory_checkpointer_without_database() -> None:
    saver = await build_checkpointer(app_settings())
    assert isinstance(saver, InMemorySaver)


def test_no_callbacks_without_langfuse_keys() -> None:
    assert default_callbacks(app_settings()) == []


def test_langfuse_handler_when_keys_are_set(monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict[str, Any] = {}

    class FakeLangfuse:
        def __init__(self, **kwargs: Any) -> None:
            created["client"] = kwargs

    class FakeHandler(BaseCallbackHandler):
        def __init__(self, public_key: str | None = None) -> None:
            created["handler"] = public_key

    root = types.ModuleType("langfuse")
    root.Langfuse = FakeLangfuse  # type: ignore[attr-defined]
    sub = types.ModuleType("langfuse.langchain")
    sub.CallbackHandler = FakeHandler  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langfuse", root)
    monkeypatch.setitem(sys.modules, "langfuse.langchain", sub)

    settings = app_settings(
        langfuse_public_key="pk-test", langfuse_secret_key="sk-test", langfuse_host="http://lf"
    )
    handlers = default_callbacks(settings)

    assert len(handlers) == 1 and isinstance(handlers[0], FakeHandler)
    assert created["client"] == {
        "public_key": "pk-test",
        "secret_key": "sk-test",
        "host": "http://lf",
    }
    assert created["handler"] == "pk-test"


def test_langfuse_env_names_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    settings = app_settings()
    assert settings.langfuse_enabled
    assert settings.langfuse_public_key == "pk"


async def test_run_callbacks_receive_case_metadata() -> None:
    handler = RecordingHandler()
    factory = runtime_factory(callbacks=[handler])
    case = Case(case_id="traced-1", address=CLEAN)

    async with factory() as runtime:
        events = [e async for e in stream_case(runtime, case, "traced-1-t")]
        state = await read_case(runtime, "traced-1-t")

    assert events[-1].kind == "completed"
    assert state is not None and state.verdict is not None
    assert handler.metadata, "callbacks were not propagated to model calls"
    assert all(m.get("langfuse_session_id") == "traced-1" for m in handler.metadata)
    assert all(m.get("case_id") == "traced-1" for m in handler.metadata)
