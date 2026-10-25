"""`PatternKnowledgeBase` over local markdown docs with a numpy vector index.

The index is built on first search and cached on disk, keyed by a hash of the
embedder identity and the chunk texts, so editing a doc or switching embedders
rebuilds it automatically.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Self

from langchain_core.embeddings import Embeddings

from sentinel.core.schemas import PatternDoc
from sentinel.knowledge.documents import (
    DEFAULT_PATTERNS_DIR,
    PatternSource,
    chunk_document,
    load_pattern_docs,
)
from sentinel.knowledge.embeddings import embedder_id
from sentinel.knowledge.index import LocalVectorIndex
from sentinel.tools.paths import CACHE_DIR

logger = logging.getLogger(__name__)

DEFAULT_INDEX_DIR = CACHE_DIR / "patterns"


class VectorPatternKnowledgeBase:
    """Semantic search over fraud-pattern documents."""

    def __init__(
        self,
        embeddings: Embeddings,
        docs: Sequence[PatternSource],
        *,
        index_dir: Path | None = DEFAULT_INDEX_DIR,
        max_chunk_chars: int = 1500,
    ) -> None:
        if not docs:
            raise ValueError("at least one pattern document is required")
        self._embeddings = embeddings
        self._docs = {doc.doc_id: doc for doc in docs}
        self._chunks = [c for doc in docs for c in chunk_document(doc, max_chunk_chars)]
        self._index_dir = index_dir
        self._index: LocalVectorIndex | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def from_directory(
        cls,
        embeddings: Embeddings,
        directory: Path = DEFAULT_PATTERNS_DIR,
        *,
        index_dir: Path | None = DEFAULT_INDEX_DIR,
    ) -> Self:
        return cls(embeddings, load_pattern_docs(directory), index_dir=index_dir)

    @property
    def doc_ids(self) -> list[str]:
        return list(self._docs)

    def cache_key(self) -> str:
        digest = hashlib.sha256(embedder_id(self._embeddings).encode())
        for chunk in self._chunks:
            digest.update(b"\0" + chunk.chunk_id.encode() + b"\0" + chunk.text.encode())
        return digest.hexdigest()[:24]

    def _cache_path(self) -> Path | None:
        return self._index_dir / f"{self.cache_key()}.npz" if self._index_dir else None

    async def build_index(self) -> LocalVectorIndex:
        """Load the cached index or embed all chunks. Safe to call concurrently."""
        async with self._lock:
            if self._index is not None:
                return self._index
            path = self._cache_path()
            ids = [c.chunk_id for c in self._chunks]
            if path is not None and await asyncio.to_thread(path.exists):
                try:
                    cached = await asyncio.to_thread(LocalVectorIndex.load, path)
                    if cached.ids == ids:
                        self._index = cached
                        return cached
                except (OSError, ValueError, KeyError) as exc:
                    logger.warning("ignoring unreadable pattern index %s: %r", path, exc)
            vectors = await self._embeddings.aembed_documents([c.text for c in self._chunks])
            index = LocalVectorIndex(ids, vectors)
            if path is not None:
                await asyncio.to_thread(index.save, path)
            self._index = index
            return index

    async def search(self, query: str, k: int = 4) -> list[PatternDoc]:
        if k <= 0 or not query.strip():
            return []
        index = await self.build_index()
        vector = await self._embeddings.aembed_query(query)
        chunk_doc = {c.chunk_id: c.doc_id for c in self._chunks}
        best: dict[str, float] = {}
        for chunk_id, score in index.search(vector, len(index)):
            doc_id = chunk_doc[chunk_id]
            best.setdefault(doc_id, score)  # results are best first: keep max per doc
            if len(best) == k:
                break
        return [self._to_pattern_doc(self._docs[d], s) for d, s in best.items()]

    @staticmethod
    def _to_pattern_doc(doc: PatternSource, score: float) -> PatternDoc:
        return PatternDoc(
            doc_id=doc.doc_id,
            title=doc.title,
            content=doc.content,
            indicators=doc.indicators,
            score=round(score, 6),
        )
