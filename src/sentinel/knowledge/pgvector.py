"""`PatternKnowledgeBase` backed by Postgres + pgvector.

Chunks come from the same markdown loader and chunker as the local index. Rows are
keyed by (embedder, chunk_id) and carry a hash of the chunk text, so indexing is
idempotent: unchanged chunks are not re-embedded, changed ones are replaced and
chunks of deleted docs are removed. Different embedders share the table safely.

Vectors are sent as pgvector text literals (`'[0.1,0.2]'::vector`), so no client-side
type registration is needed. The `vector` extension must be available in the database.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Self

from langchain_core.embeddings import Embeddings
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from sentinel.core.schemas import PatternDoc
from sentinel.knowledge.documents import (
    DEFAULT_PATTERNS_DIR,
    PatternSource,
    chunk_document,
    load_pattern_docs,
)
from sentinel.knowledge.embeddings import embedder_id

DEFAULT_TABLE = "pattern_chunks"

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS {table} (
    embedder     text NOT NULL,
    chunk_id     text NOT NULL,
    doc_id       text NOT NULL,
    content_hash text NOT NULL,
    content      text NOT NULL,
    embedding    vector NOT NULL,
    PRIMARY KEY (embedder, chunk_id)
);
"""

# Serializes concurrent indexers (several API workers starting at once).
_LOCK_KEY = 0x5E7_1DE


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class PgVectorPatternKnowledgeBase:
    """Semantic search over fraud-pattern documents stored in pgvector."""

    def __init__(
        self,
        pool: AsyncConnectionPool[Any],
        embeddings: Embeddings,
        docs: Sequence[PatternSource],
        *,
        max_chunk_chars: int = 1500,
        table: str = DEFAULT_TABLE,
    ) -> None:
        if not docs:
            raise ValueError("at least one pattern document is required")
        if not _IDENTIFIER.match(table):
            raise ValueError(f"invalid table name: {table!r}")
        self._table = table
        self._pool = pool
        self._embeddings = embeddings
        self._embedder = embedder_id(embeddings)
        self._docs = {doc.doc_id: doc for doc in docs}
        self._chunks = [c for doc in docs for c in chunk_document(doc, max_chunk_chars)]
        self._ready = False
        self._lock = asyncio.Lock()

    @classmethod
    def from_directory(
        cls,
        pool: AsyncConnectionPool[Any],
        embeddings: Embeddings,
        directory: Path = DEFAULT_PATTERNS_DIR,
        *,
        table: str = DEFAULT_TABLE,
    ) -> Self:
        return cls(pool, embeddings, load_pattern_docs(directory), table=table)

    @property
    def doc_ids(self) -> list[str]:
        return list(self._docs)

    async def setup(self) -> None:
        """Create the extension and table if missing."""
        async with self._pool.connection() as conn:
            await conn.execute(_SCHEMA.format(table=self._table))

    async def build_index(self) -> int:
        """Embed and store new or changed chunks; return how many were embedded."""
        async with self._lock:
            if self._ready:
                return 0
            await self.setup()
            async with self._pool.connection() as conn, conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
                cur = conn.cursor(row_factory=dict_row)
                await cur.execute(
                    f"SELECT chunk_id, content_hash FROM {self._table} WHERE embedder = %s",
                    (self._embedder,),
                )
                stored = {row["chunk_id"]: row["content_hash"] for row in await cur.fetchall()}
                stale = [c for c in self._chunks if stored.get(c.chunk_id) != _content_hash(c.text)]
                if stale:
                    vectors = await self._embeddings.aembed_documents([c.text for c in stale])
                    await cur.executemany(
                        f"""
                        INSERT INTO {self._table}
                            (embedder, chunk_id, doc_id, content_hash, content, embedding)
                        VALUES (%s, %s, %s, %s, %s, %s::vector)
                        ON CONFLICT (embedder, chunk_id) DO UPDATE SET
                            doc_id = EXCLUDED.doc_id,
                            content_hash = EXCLUDED.content_hash,
                            content = EXCLUDED.content,
                            embedding = EXCLUDED.embedding
                        """,
                        [
                            (
                                self._embedder,
                                c.chunk_id,
                                c.doc_id,
                                _content_hash(c.text),
                                c.text,
                                _vector_literal(v),
                            )
                            for c, v in zip(stale, vectors, strict=True)
                        ],
                    )
                await cur.execute(
                    f"DELETE FROM {self._table} WHERE embedder = %s AND NOT (chunk_id = ANY(%s))",
                    (self._embedder, [c.chunk_id for c in self._chunks]),
                )
            self._ready = True
            return len(stale)

    async def search(self, query: str, k: int = 4) -> list[PatternDoc]:
        if k <= 0 or not query.strip():
            return []
        if not self._ready:
            await self.build_index()
        vector = _vector_literal(await self._embeddings.aembed_query(query))
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"""
                SELECT doc_id, max(1 - (embedding <=> %s::vector)) AS score
                FROM {self._table}
                WHERE embedder = %s
                GROUP BY doc_id
                ORDER BY score DESC, doc_id
                LIMIT %s
                """,
                (vector, self._embedder, k),
            )
            rows = await cur.fetchall()
        return [
            self._to_pattern_doc(self._docs[row["doc_id"]], float(row["score"]))
            for row in rows
            if row["doc_id"] in self._docs
        ]

    @staticmethod
    def _to_pattern_doc(doc: PatternSource, score: float) -> PatternDoc:
        return PatternDoc(
            doc_id=doc.doc_id,
            title=doc.title,
            content=doc.content,
            indicators=doc.indicators,
            score=round(score, 6),
        )
