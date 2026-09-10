"""Local semantic candidate generation for chatbot memory.

This module never calls an upstream model and never decides whether a memory
may be spoken aloud.  It only supplies dense similarity scores to the existing
authoritative memory gate, which retains provenance, expiry, supersession and
privacy policy checks.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import threading
from typing import Sequence

from agent.skill_retrieval import (
    AssetValidationError,
    Embedder,
    cosine_similarity,
    load_shared_bge_embedder,
)


class SemanticMemoryRuntime:
    """Lazy local encoder with a bounded per-generation document cache."""

    def __init__(self, asset_root: Path, *, cache_entries: int = 4) -> None:
        self._asset_root = Path(asset_root)
        self._cache_entries = max(1, int(cache_entries))
        self._lock = threading.RLock()
        self._embedder: Embedder | None = None
        self._load_failure = ""
        self._document_vectors: OrderedDict[
            tuple[str, ...], tuple[tuple[float, ...], ...]
        ] = OrderedDict()

    @property
    def unavailable_reason(self) -> str:
        with self._lock:
            return self._load_failure

    def clear_document_cache(self) -> None:
        """Forget cached memory vectors after write, clear or supersession."""

        with self._lock:
            self._document_vectors.clear()

    def _encoder(self) -> Embedder:
        if self._embedder is not None:
            return self._embedder
        if self._load_failure:
            raise AssetValidationError(self._load_failure)
        try:
            self._embedder = load_shared_bge_embedder(self._asset_root)
        except Exception as error:
            self._load_failure = str(error) or type(error).__name__
            raise
        return self._embedder

    def similarities(
        self,
        query: str,
        documents: Sequence[str],
    ) -> tuple[float, ...] | None:
        normalized_query = str(query or "").strip()
        document_key = tuple(str(item or "").strip() for item in documents)
        if not normalized_query or not document_key:
            return ()
        try:
            with self._lock:
                encoder = self._encoder()
                vectors = self._document_vectors.get(document_key)
                if vectors is None:
                    raw = encoder.embed(document_key)
                    vectors = tuple(tuple(float(value) for value in row) for row in raw)
                    if len(vectors) != len(document_key):
                        raise AssetValidationError("embedding_batch_shape_invalid")
                    self._document_vectors[document_key] = vectors
                    self._document_vectors.move_to_end(document_key)
                    while len(self._document_vectors) > self._cache_entries:
                        self._document_vectors.popitem(last=False)
                else:
                    self._document_vectors.move_to_end(document_key)
                query_rows = encoder.embed((normalized_query,))
                if len(query_rows) != 1:
                    raise AssetValidationError("embedding_query_shape_invalid")
                query_vector = query_rows[0]
                return tuple(cosine_similarity(query_vector, row) for row in vectors)
        except Exception as error:
            # Dense recall is an enhancement.  Missing optional dependencies or
            # inference errors must leave the established lexical path usable.
            with self._lock:
                if not self._load_failure:
                    self._load_failure = str(error) or type(error).__name__
            return None
