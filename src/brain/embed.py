"""fastembed wrapper. Lazy: importing this module must NOT load the model.

Model: nomic-ai/nomic-embed-text-v1.5. Nomic requires task prefixes:
corpus text gets "search_document: ", queries get "search_query: ".
Vectors are Matryoshka-truncated to the first EMBED_DIM dims, then
L2-normalized, stored as float32.

The model downloads on first use to FASTEMBED_CACHE_PATH, which config.py
defaults to <BRAIN_DIR>/cache/fastembed (env-overridable).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from .config import EMBED_DIM, EMBED_MODEL, EMBED_TEXT_CAP

_model = None
_model_lock = threading.Lock()

DOC_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "


def _get_model():
    global _model
    with _model_lock:  # background ingest + recall may race on first load
        if _model is None:
            from fastembed import TextEmbedding

            cache = os.environ.get("FASTEMBED_CACHE_PATH")
            if cache:
                Path(cache).mkdir(parents=True, exist_ok=True)
            _model = TextEmbedding(model_name=EMBED_MODEL)
    return _model


def available() -> bool:
    """Cheap check that the embedding stack is importable (does not load the model)."""
    try:
        import fastembed  # noqa: F401

        return True
    except Exception:
        return False


def _finalize(vec):
    import numpy as np

    v = np.asarray(vec, dtype=np.float32)[:EMBED_DIM]
    norm = float(np.linalg.norm(v))
    if norm > 0:
        v = v / norm
    return v.astype(np.float32)


def embed_documents(texts: list[str]) -> list:
    """Embed corpus texts (batched). Input is truncated to EMBED_TEXT_CAP chars."""
    model = _get_model()
    prefixed = [DOC_PREFIX + t[:EMBED_TEXT_CAP] for t in texts]
    return [_finalize(v) for v in model.embed(prefixed, batch_size=32)]


def embed_query(text: str):
    """Embed a single search query."""
    model = _get_model()
    return [_finalize(v) for v in model.embed([QUERY_PREFIX + text[:EMBED_TEXT_CAP]])][0]
