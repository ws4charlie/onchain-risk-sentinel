"""Embedding models for the pattern knowledge base."""

from __future__ import annotations

import hashlib
import itertools
import math
import re

from langchain_core.embeddings import Embeddings

DEFAULT_GEMINI_EMBEDDING_MODEL = "gemini-embedding-001"

_TOKEN = re.compile(r"[a-z0-9]+")


def default_embeddings(model: str = DEFAULT_GEMINI_EMBEDDING_MODEL) -> Embeddings:
    """Gemini embeddings via langchain-google-genai; reads GOOGLE_API_KEY from the env."""
    from langchain_google_genai import GoogleGenerativeAIEmbeddings

    return GoogleGenerativeAIEmbeddings(model=model)


def embedder_id(embeddings: Embeddings) -> str:
    """A stable identifier for an embedder, used to key the on-disk index cache."""
    cls = type(embeddings)
    model = getattr(embeddings, "model", None) or getattr(embeddings, "model_name", None)
    dim = getattr(embeddings, "dim", None)
    return f"{cls.__module__}.{cls.__qualname__}:{model or ''}:{dim or ''}"


class HashingEmbeddings(Embeddings):
    """Deterministic, offline bag-of-words embeddings (feature hashing of words and bigrams).

    No semantic understanding, but stable across runs and machines. Used by tests and as
    an offline fallback when no embedding API key is available.
    """

    def __init__(self, dim: int = 512) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self.dim = dim

    def _embed(self, text: str) -> list[float]:
        words = _TOKEN.findall(text.lower())
        features = words + [f"{a} {b}" for a, b in itertools.pairwise(words)]
        vector = [0.0] * self.dim
        for feature in features:
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            sign = 1.0 if value & 1 else -1.0
            vector[(value >> 1) % self.dim] += sign
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)
