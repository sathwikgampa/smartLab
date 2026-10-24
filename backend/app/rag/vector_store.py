"""Vector similarity search over chunk embeddings.

``FaissVectorStore`` (exact inner-product ``IndexFlatIP`` wrapped in an
``IndexIDMap``) is used when ``faiss-cpu`` is installed; ``NumpyVectorStore``
is a drop-in fallback with identical semantics. Both persist to ``index_dir``
and store vectors keyed by the SQLite ``chunks.id``.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


class NumpyVectorStore:
    backend = "numpy"

    def __init__(self, dim: int, index_dir: Path | None = None):
        self.dim = dim
        self.index_dir = Path(index_dir) if index_dir else None
        self._ids = np.zeros((0,), dtype=np.int64)
        self._vectors = np.zeros((0, dim), dtype=np.float32)
        self._lock = threading.RLock()

    def __len__(self) -> int:
        return int(self._ids.shape[0])

    def add(self, ids: list[int], vectors: np.ndarray) -> None:
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim != 2 or vectors.shape[1] != self.dim:
            raise ValueError(f"Expected vectors of shape (n, {self.dim}), got {vectors.shape}.")
        if len(ids) != vectors.shape[0]:
            raise ValueError("ids and vectors must have the same length.")
        with self._lock:
            self.remove(ids)
            self._ids = np.concatenate([self._ids, np.asarray(ids, dtype=np.int64)])
            self._vectors = np.vstack([self._vectors, vectors])

    def remove(self, ids: list[int]) -> None:
        if not len(ids):
            return
        with self._lock:
            keep = ~np.isin(self._ids, np.asarray(ids, dtype=np.int64))
            self._ids, self._vectors = self._ids[keep], self._vectors[keep]

    def search(self, query: np.ndarray, k: int, allowed_ids: set[int] | None = None) -> list[tuple[int, float]]:
        with self._lock:
            if not len(self) or k <= 0:
                return []
            scores = self._vectors @ np.asarray(query, dtype=np.float32).reshape(-1)
            ids = self._ids
            if allowed_ids is not None:
                mask = np.isin(ids, np.fromiter(allowed_ids, dtype=np.int64, count=len(allowed_ids)))
                scores, ids = scores[mask], ids[mask]
                if not len(ids):
                    return []
            k = min(k, len(ids))
            top = np.argpartition(-scores, k - 1)[:k]
            top = top[np.argsort(-scores[top])]
            return [(int(ids[i]), float(scores[i])) for i in top]

    def save(self) -> None:
        if self.index_dir is None:
            return
        self.index_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            np.savez(self.index_dir / "vectors.npz", ids=self._ids, vectors=self._vectors)

    def load(self) -> bool:
        path = self.index_dir / "vectors.npz" if self.index_dir else None
        if not path or not path.exists():
            return False
        data = np.load(path)
        if data["vectors"].shape[1:] != (self.dim,):
            logger.warning("Stored index dimension mismatch; index will be rebuilt.")
            return False
        self._ids, self._vectors = data["ids"].astype(np.int64), data["vectors"].astype(np.float32)
        return True


class FaissVectorStore:
    backend = "faiss"

    def __init__(self, dim: int, index_dir: Path | None = None):
        import faiss

        self._faiss = faiss
        self.dim = dim
        self.index_dir = Path(index_dir) if index_dir else None
        self._index = faiss.IndexIDMap(faiss.IndexFlatIP(dim))
        self._lock = threading.RLock()

    def __len__(self) -> int:
        return int(self._index.ntotal)

    def add(self, ids: list[int], vectors: np.ndarray) -> None:
        vectors = np.ascontiguousarray(vectors, dtype=np.float32)
        if vectors.ndim != 2 or vectors.shape[1] != self.dim:
            raise ValueError(f"Expected vectors of shape (n, {self.dim}), got {vectors.shape}.")
        if len(ids) != vectors.shape[0]:
            raise ValueError("ids and vectors must have the same length.")
        with self._lock:
            self.remove(ids)
            self._index.add_with_ids(vectors, np.asarray(ids, dtype=np.int64))

    def remove(self, ids: list[int]) -> None:
        if len(ids):
            with self._lock:
                self._index.remove_ids(np.asarray(ids, dtype=np.int64))

    def search(self, query: np.ndarray, k: int, allowed_ids: set[int] | None = None) -> list[tuple[int, float]]:
        with self._lock:
            if not len(self) or k <= 0:
                return []
            q = np.ascontiguousarray(np.asarray(query, dtype=np.float32).reshape(1, -1))
            params = None
            if allowed_ids is not None:
                if not allowed_ids:
                    return []
                # IDSelectorBatch is hash-based (O(1) per candidate); IDSelectorArray scans the whole list.
                selector = self._faiss.IDSelectorBatch(np.fromiter(allowed_ids, dtype=np.int64, count=len(allowed_ids)))
                params = self._faiss.SearchParameters(sel=selector)
            scores, ids = self._index.search(q, min(k, len(self)), params=params)
            return [(int(i), float(s)) for i, s in zip(ids[0], scores[0]) if i != -1]

    def save(self) -> None:
        if self.index_dir is None:
            return
        self.index_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._faiss.write_index(self._index, str(self.index_dir / "index.faiss"))

    def load(self) -> bool:
        path = self.index_dir / "index.faiss" if self.index_dir else None
        if not path or not path.exists():
            return False
        index = self._faiss.read_index(str(path))
        if index.d != self.dim:
            logger.warning("Stored FAISS index dimension mismatch; index will be rebuilt.")
            return False
        self._index = index
        return True


def get_vector_store(backend: str, dim: int, index_dir: Path | None):
    if backend in ("auto", "faiss"):
        try:
            return FaissVectorStore(dim, index_dir)
        except ImportError:
            if backend == "faiss":
                raise
            logger.info("faiss-cpu not installed; using NumPy vector store.")
    return NumpyVectorStore(dim, index_dir)
