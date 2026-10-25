"""Fraud-pattern knowledge base: markdown docs, embeddings and a local vector index."""

from sentinel.knowledge.base import VectorPatternKnowledgeBase
from sentinel.knowledge.documents import PatternSource, load_pattern_docs
from sentinel.knowledge.embeddings import HashingEmbeddings, default_embeddings
from sentinel.knowledge.index import LocalVectorIndex

__all__ = [
    "HashingEmbeddings",
    "LocalVectorIndex",
    "PatternSource",
    "VectorPatternKnowledgeBase",
    "default_embeddings",
    "load_pattern_docs",
]
