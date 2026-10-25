import os
from pathlib import Path

import pytest

from sentinel.core.ports import PatternKnowledgeBase
from sentinel.knowledge import (
    HashingEmbeddings,
    LocalVectorIndex,
    VectorPatternKnowledgeBase,
    default_embeddings,
    load_pattern_docs,
)
from sentinel.knowledge.documents import (
    REQUIRED_SECTIONS,
    PatternSource,
    chunk_document,
    parse_pattern_markdown,
)

DOC = """# Example pattern

## Description
Something bad happens on chain.

## On-chain indicators
- first indicator
- second indicator

## Example signals
- check a thing

## References
- https://example.org/a
"""


class CountingEmbeddings(HashingEmbeddings):
    def __init__(self) -> None:
        super().__init__(dim=256)
        self.document_calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += 1
        return super().embed_documents(texts)


def test_parse_markdown_sections() -> None:
    doc = parse_pattern_markdown("example", DOC)
    assert doc.title == "Example pattern"
    assert doc.indicators == ["first indicator", "second indicator"]
    assert doc.signals == ["check a thing"]
    assert doc.references == ["https://example.org/a"]


def test_parse_requires_title() -> None:
    with pytest.raises(ValueError):
        parse_pattern_markdown("x", "no title here")


def test_chunking_excludes_references_and_splits_long_docs() -> None:
    doc = parse_pattern_markdown("example", DOC)
    [chunk] = chunk_document(doc)
    assert "https://" not in chunk.text and "first indicator" in chunk.text
    chunks = chunk_document(doc, max_chars=60)
    assert len(chunks) == 3
    assert all(c.text.startswith("Example pattern") for c in chunks)


def test_committed_pattern_docs_are_complete() -> None:
    docs = load_pattern_docs()
    assert 20 <= len(docs) <= 30
    for doc in docs:
        assert all(doc.sections.get(name) for name in REQUIRED_SECTIONS), doc.doc_id
        assert doc.indicators, doc.doc_id
        assert all(ref.startswith("https://") for ref in doc.references), doc.doc_id


def test_vector_index_ranks_by_cosine(tmp_path: Path) -> None:
    index = LocalVectorIndex(["a", "b", "c"], [[1, 0], [0, 1], [1, 1]])
    assert [i for i, _ in index.search([1, 0.1], 3)] == ["a", "c", "b"]
    path = tmp_path / "index.npz"
    index.save(path)
    loaded = LocalVectorIndex.load(path)
    assert loaded.ids == index.ids
    assert loaded.search([0, 1], 1)[0][0] == "b"
    with pytest.raises(ValueError):
        index.search([1, 0, 0], 1)


def test_hashing_embeddings_are_deterministic_and_normalized() -> None:
    emb = HashingEmbeddings(dim=64)
    a, b = (
        emb.embed_query("zero value transfer"),
        HashingEmbeddings(dim=64).embed_query("zero value transfer"),
    )
    assert a == b
    assert sum(v * v for v in a) == pytest.approx(1.0)


async def test_search_finds_relevant_pattern(tmp_path: Path) -> None:
    kb = VectorPatternKnowledgeBase.from_directory(HashingEmbeddings(), index_dir=tmp_path)
    assert isinstance(kb, PatternKnowledgeBase)
    results = await kb.search(
        "look-alike address prefix suffix zero-value transfers poisoning victim history", k=3
    )
    assert len(results) == 3
    assert results[0].doc_id == "address-poisoning"
    assert results[0].indicators
    assert [r.score for r in results] == sorted((r.score for r in results), reverse=True)
    assert len({r.doc_id for r in results}) == 3


async def test_search_edge_cases(tmp_path: Path) -> None:
    kb = VectorPatternKnowledgeBase.from_directory(HashingEmbeddings(), index_dir=tmp_path)
    assert await kb.search("anything", k=0) == []
    assert await kb.search("   ") == []
    assert len(await kb.search("mixer", k=100)) == len(kb.doc_ids)


async def test_index_is_cached_on_disk(tmp_path: Path) -> None:
    docs = [parse_pattern_markdown("example", DOC), parse_pattern_markdown("other", DOC)]
    first = CountingEmbeddings()
    await VectorPatternKnowledgeBase(first, docs, index_dir=tmp_path).search("indicator")
    assert first.document_calls == 1
    assert len(list(tmp_path.glob("*.npz"))) == 1  # noqa: ASYNC240

    second = CountingEmbeddings()
    await VectorPatternKnowledgeBase(second, docs, index_dir=tmp_path).search("indicator")
    assert second.document_calls == 0  # loaded from cache

    changed = [*docs, PatternSource("new", "New", "x", {"Description": "new text"})]
    third = CountingEmbeddings()
    await VectorPatternKnowledgeBase(third, changed, index_dir=tmp_path).search("indicator")
    assert third.document_calls == 1  # docs changed: cache key changed


def test_requires_docs() -> None:
    with pytest.raises(ValueError):
        VectorPatternKnowledgeBase(HashingEmbeddings(), [])


@pytest.mark.live
async def test_live_gemini_embeddings(tmp_path: Path) -> None:
    if not os.environ.get("GOOGLE_API_KEY"):
        pytest.skip("GOOGLE_API_KEY not set")
    kb = VectorPatternKnowledgeBase.from_directory(default_embeddings(), index_dir=tmp_path)
    results = await kb.search("attacker sends zero-value tokens from a look-alike address", k=3)
    assert "address-poisoning" in {r.doc_id for r in results}
