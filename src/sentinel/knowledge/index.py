"""A tiny local vector index: an in-memory numpy matrix with cosine similarity."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Self

import numpy as np
import numpy.typing as npt

Matrix = npt.NDArray[np.float32]


def _normalize(matrix: Matrix) -> Matrix:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (matrix / norms).astype(np.float32)


class LocalVectorIndex:
    """Rows are unit vectors, so a dot product is the cosine similarity."""

    def __init__(self, ids: Sequence[str], vectors: Sequence[Sequence[float]] | Matrix) -> None:
        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != len(ids):
            raise ValueError("vectors must be a 2-D array with one row per id")
        self.ids = list(ids)
        self._matrix = _normalize(matrix)

    def __len__(self) -> int:
        return len(self.ids)

    def search(self, query: Sequence[float], k: int) -> list[tuple[str, float]]:
        """Return up to k (id, cosine similarity) pairs, best first."""
        if k <= 0 or not self.ids:
            return []
        q = np.asarray(query, dtype=np.float32)
        if q.shape != (self._matrix.shape[1],):
            raise ValueError(f"query has shape {q.shape}, index has dim {self._matrix.shape[1]}")
        norm = float(np.linalg.norm(q))
        scores = self._matrix @ (q / norm if norm else q)
        order = np.argsort(-scores, kind="stable")[:k]
        return [(self.ids[i], float(scores[i])) for i in order]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("wb") as fh:
            np.savez(fh, vectors=self._matrix, ids=np.array(json.dumps(self.ids)))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> Self:
        with np.load(path, allow_pickle=False) as data:
            ids = json.loads(str(data["ids"]))
            return cls(ids, data["vectors"])
