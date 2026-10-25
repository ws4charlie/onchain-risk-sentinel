"""Composition root: turn settings into real adapters, a checkpointer and a compiled graph.

`open_runtime` is the one entry point the CLI and the API use. It owns every resource
(HTTP clients, the Postgres pool, the tracing client) and closes them on exit. Tests
inject fake ports, scripted models and an in-memory checkpointer through its arguments.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.embeddings import Embeddings
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from sentinel.agents import Models, build_graph
from sentinel.agents.graph import TriageGraph, checkpoint_serde
from sentinel.app.settings import AppSettings
from sentinel.core.ports import PatternKnowledgeBase, Ports
from sentinel.knowledge import HashingEmbeddings, VectorPatternKnowledgeBase, default_embeddings
from sentinel.knowledge.pgvector import PgVectorPatternKnowledgeBase
from sentinel.tools import EtherscanChainData, FeedScamIntel, OfacSanctions

logger = logging.getLogger(__name__)


async def open_pool(settings: AppSettings, stack: AsyncExitStack) -> AsyncConnectionPool[Any]:
    """Open a Postgres pool configured the way `AsyncPostgresSaver` requires."""
    if settings.database_url is None:
        raise ValueError("SENTINEL_DATABASE_URL is not set")
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        settings.database_url.get_secret_value(),
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open(wait=True)
    stack.push_async_callback(pool.close)
    return pool


def build_embeddings(settings: AppSettings) -> Embeddings:
    if settings.embedding_provider == "hashing":
        return HashingEmbeddings()
    return default_embeddings(settings.embedding_model)


async def build_ports(
    settings: AppSettings,
    *,
    stack: AsyncExitStack | None = None,
    pool: AsyncConnectionPool[Any] | None = None,
) -> Ports:
    """Real adapters. Uses the pgvector knowledge base when a database is configured.

    Resources are registered on `stack`; without one the caller must close them
    (`ports.chain_data.aclose()`, and the pool if this function opened it).
    """
    stack = stack if stack is not None else AsyncExitStack()
    chain_data = EtherscanChainData.from_settings(settings)
    stack.push_async_callback(chain_data.aclose)
    sanctions = await asyncio.to_thread(OfacSanctions.from_snapshot)
    scam_intel = await FeedScamIntel.load()

    embeddings = build_embeddings(settings)
    patterns: PatternKnowledgeBase
    if settings.database_url is not None:
        pool = pool or await open_pool(settings, stack)
        patterns = PgVectorPatternKnowledgeBase.from_directory(pool, embeddings)
    else:
        patterns = VectorPatternKnowledgeBase.from_directory(embeddings)
    return Ports(
        chain_data=chain_data, sanctions=sanctions, scam_intel=scam_intel, patterns=patterns
    )


async def build_checkpointer(
    settings: AppSettings,
    *,
    stack: AsyncExitStack | None = None,
    pool: AsyncConnectionPool[Any] | None = None,
) -> BaseCheckpointSaver[Any]:
    """Postgres checkpointer when configured (tables created on first use), else in-memory."""
    if settings.database_url is None:
        return InMemorySaver(serde=checkpoint_serde())
    pool = pool or await open_pool(settings, stack if stack is not None else AsyncExitStack())
    saver = AsyncPostgresSaver(pool, serde=checkpoint_serde())
    await saver.setup()
    return saver


def default_callbacks(settings: AppSettings) -> list[BaseCallbackHandler]:
    """Callbacks attached to every graph run: Langfuse tracing when configured, else none."""
    if not settings.langfuse_enabled:
        return []
    try:
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler
    except ImportError as exc:
        raise RuntimeError(
            "Langfuse keys are set but the package is missing: uv sync --extra tracing"
        ) from exc
    assert settings.langfuse_secret_key is not None
    Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key.get_secret_value(),
        host=settings.langfuse_host,
    )
    return [CallbackHandler(public_key=settings.langfuse_public_key)]


def _flush_langfuse(settings: AppSettings) -> None:
    from langfuse import get_client

    get_client(public_key=settings.langfuse_public_key).flush()


@dataclass
class Runtime:
    """Everything an entry point needs to run cases."""

    graph: TriageGraph
    settings: AppSettings
    callbacks: list[BaseCallbackHandler] = field(default_factory=list)
    persistent: bool = False  # True when checkpoints survive the process


RuntimeFactory = Callable[[], AbstractAsyncContextManager[Runtime]]


@asynccontextmanager
async def open_runtime(
    settings: AppSettings | None = None,
    *,
    ports: Ports | None = None,
    models: Models | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    callbacks: list[BaseCallbackHandler] | None = None,
) -> AsyncIterator[Runtime]:
    """Build the graph from settings; any injected part replaces its real counterpart."""
    settings = settings or AppSettings()
    async with AsyncExitStack() as stack:
        pool = None
        if settings.database_url is not None and (ports is None or checkpointer is None):
            pool = await open_pool(settings, stack)
        if checkpointer is None:
            checkpointer = await build_checkpointer(settings, stack=stack, pool=pool)
        if ports is None:
            ports = await build_ports(settings, stack=stack, pool=pool)
        if callbacks is None:
            callbacks = default_callbacks(settings)
            if settings.langfuse_enabled:
                stack.callback(_flush_langfuse, settings)
        graph = build_graph(ports, models or Models.from_settings(settings), settings, checkpointer)
        yield Runtime(
            graph=graph,
            settings=settings,
            callbacks=callbacks,
            persistent=not isinstance(checkpointer, InMemorySaver),
        )
