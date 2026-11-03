"""Embedding-space identity for the postgres store.

The local ONNX model remains the sqlite default. The serverless path uses
OpenAI ``text-embedding-3-small`` at 256 dimensions. Vectors are only searched
when ``documents.embed_space`` equals the space this process would write.
"""

from __future__ import annotations

import os

from .config import EMBED_DIM, EMBED_MODEL, VEC_FLOOR_COS

LEGACY_EMBED_SPACE = f"local:{EMBED_MODEL}:{EMBED_DIM}"

# Priors, not measurements on this corpus. Override with BRAIN_VEC_FLOOR_COS.
_SPACE_FLOORS = {
    LEGACY_EMBED_SPACE: VEC_FLOOR_COS,
    "openai:text-embedding-3-small:256": 0.40,
}

OPENAI_REEMBED_MODEL = "text-embedding-3-small"
OPENAI_REEMBED_DIM = 256
OPENAI_REEMBED_SPACE = f"openai:{OPENAI_REEMBED_MODEL}:{OPENAI_REEMBED_DIM}"

# Synchronous OpenAI list prices, USD per 1M input tokens.
OPENAI_USD_PER_MTOK = {
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
    "text-embedding-ada-002": 0.10,
}


def embed_provider() -> str:
    """local | openai. Unset stays local so sqlite ingest is unchanged."""
    return (os.environ.get("BRAIN_EMBED_PROVIDER") or "local").strip().lower()


def embed_model_name() -> str:
    override = (os.environ.get("BRAIN_EMBED_MODEL") or "").strip()
    if override:
        return override
    if embed_provider() == "openai":
        return OPENAI_REEMBED_MODEL
    return EMBED_MODEL


def embed_dim() -> int:
    raw = (os.environ.get("BRAIN_EMBED_DIM") or "").strip()
    if raw:
        try:
            n = int(raw)
            if n > 0:
                return n
        except ValueError:
            pass
    if embed_provider() == "openai":
        return OPENAI_REEMBED_DIM
    return EMBED_DIM


def embed_space() -> str:
    """Identity of the vectors this process would write: provider:model:dim."""
    return f"{embed_provider()}:{embed_model_name()}:{embed_dim()}"


def vec_floor_cos(space: str | None = None) -> float:
    """Cosine floor for the vector leg. ``BRAIN_VEC_FLOOR_COS`` wins."""
    raw = (os.environ.get("BRAIN_VEC_FLOOR_COS") or "").strip()
    if raw:
        try:
            val = float(raw)
        except ValueError:
            val = None
        else:
            if 0.0 <= val <= 1.0:
                return val
    if space is None:
        space = embed_space()
    return _SPACE_FLOORS.get(space, VEC_FLOOR_COS)


def approx_tokens(text: str) -> int:
    """Rough input-token count (4 chars ≈ 1 token) for cost estimates."""
    return max(1, (len(text) + 3) // 4)
