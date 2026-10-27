"""Integration: the pgvector knowledge base against a real Postgres with the vector extension."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from sentinel.core.ports import PatternKnowledgeBase
from sentinel.knowledge import HashingEmbeddings, VectorPatternKnowledgeBase, load_pattern_docs
from sentinel.knowledge.documents import PatternSource
from sentinel.knowledge.pgvector import PgVectorPatternKnowledgeBase

DATABASE_URL = os.environ.get("SENTINEL_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="SENTINEL_DATABASE_URL is not set"),
]

QUERY = "zero-value transfers from a look-alike address copied from history"


class CountingEmbeddings(HashingEmbeddings):
    def __init__(self) -> None:
        super().__init__(dim=256)
        self.embedded = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.embedded += len(texts)
        return super().embed_documents(texts)


@pytest.fixture
async def pool() -> AsyncIterator[AsyncConnectionPool[Any]]:
    assert DATABASE_URL
    async with AsyncConnectionPool(DATABASE_URL, kwargs={"autocommit": True}) as pool:
        yield pool


@pytest.fixture
async def table(pool: AsyncConnectionPool[Any]) -> AsyncIterator[str]:
    name = f"test_patterns_{uuid.uuid4().hex[:8]}"
    yield name
    async with pool.connection() as conn:
        await conn.execute(f"DROP TABLE IF EXISTS {name}")


async def test_search_matches_the_local_index(pool: AsyncConnectionPool[Any], table: str) -> None:
    docs = load_pattern_docs()
    embeddings = HashingEmbeddings(dim=256)
    kb = PgVectorPatternKnowledgeBase(pool, embeddings, docs, table=table)
    local = VectorPatternKnowledgeBase(embeddings, docs, index_dir=None)

    assert isinstance(kb, PatternKnowledgeBase)
    results = await kb.search(QUERY, k=4)
    expected = await local.search(QUERY, k=4)

    assert [r.doc_id for r in results] == [e.doc_id for e in expected]
    for got, want in zip(results, expected, strict=True):
        assert got.score == pytest.approx(want.score, abs=1e-4)
    assert results[0].indicators
    assert await kb.search("   ") == []


async def test_indexing_is_idempotent(pool: AsyncConnectionPool[Any], table: str) -> None:
    docs = load_pattern_docs()[:3]
    first = CountingEmbeddings()
    assert await PgVectorPatternKnowledgeBase(pool, first, docs, table=table).build_index() > 0
    chunks = first.embedded

    second = CountingEmbeddings()
    again = PgVectorPatternKnowledgeBase(pool, second, docs, table=table)
    assert await again.build_index() == 0
    assert second.embedded == 0

    edited = PatternSource(
        doc_id=docs[0].doc_id,
        title=docs[0].title,
        content=docs[0].content,
        sections={**docs[0].sections, "Description": "Rewritten description."},
    )
    third = CountingEmbeddings()
    changed = PgVectorPatternKnowledgeBase(pool, third, [edited, docs[1]], table=table)
    assert await changed.build_index() >= 1
    assert third.embedded < chunks

    async with pool.connection() as conn:
        cur = await conn.execute(f"SELECT DISTINCT doc_id FROM {table}")
        stored = {row[0] for row in await cur.fetchall()}
    assert stored == {docs[0].doc_id, docs[1].doc_id}  # the removed doc's chunks are gone


async def test_embedders_do_not_collide(pool: AsyncConnectionPool[Any], table: str) -> None:
    docs = load_pattern_docs()[:2]
    small = PgVectorPatternKnowledgeBase(pool, HashingEmbeddings(dim=64), docs, table=table)
    large = PgVectorPatternKnowledgeBase(pool, HashingEmbeddings(dim=128), docs, table=table)

    assert len(await small.search(QUERY, k=2)) == 2
    assert len(await large.search(QUERY, k=2)) == 2
    assert len(await small.search(QUERY, k=2)) == 2


def test_rejects_unsafe_table_names() -> None:
    docs = load_pattern_docs()[:1]
    pool: Any = None
    with pytest.raises(ValueError):
        PgVectorPatternKnowledgeBase(pool, HashingEmbeddings(), docs, table="x; drop table y")
