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

from .config import (
    EMBED_BATCH,
    EMBED_CPU_ARENA,
    EMBED_DIM,
    EMBED_MODEL,
    EMBED_TEXT_CAP,
    EMBED_THREADS,
    EMBED_TOKEN_BUDGET,
)

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
            # Cap onnxruntime intra-op threads so a single ingest doesn't
            # saturate every core. `threads` is fastembed's onnxruntime
            # intra_op_num_threads knob; fall back if an API drops the kwarg.
            # `enable_cpu_mem_arena` is a fastembed-exposed session option
            # (fastembed.common.onnx_model.OnnxModel.EXPOSED_SESSION_OPTIONS);
            # off means onnxruntime returns activation memory between batches
            # instead of ratcheting a long-lived server to its worst-ever peak.
            kwargs = {"model_name": EMBED_MODEL, "threads": EMBED_THREADS}
            if not EMBED_CPU_ARENA:
                kwargs["enable_cpu_mem_arena"] = False
            for attempt in (kwargs, {"model_name": EMBED_MODEL, "threads": EMBED_THREADS},
                            {"model_name": EMBED_MODEL}):
                try:
                    _model = TextEmbedding(**attempt)
                    break
                except (TypeError, AssertionError):
                    continue  # older/newer fastembed dropped a kwarg
    return _model


def _token_lengths(model, texts: list[str]) -> list[int]:
    """True (unpadded) token count per text, for batch planning.

    Uses the model's own tokenizer so the planner sees the same lengths
    onnxruntime will. Counts via attention_mask because the tokenizer has
    padding enabled — len(ids) would report the padded batch width, not the
    real length. Falls back to character count if the tokenizer isn't
    reachable: a BPE token always covers >=1 character, so chars are a safe
    upper bound on tokens and the memory budget still holds (batches just
    come out smaller than they strictly need to be).
    """
    tok = getattr(getattr(model, "model", None), "tokenizer", None)
    if tok is not None:
        try:
            return [int(sum(e.attention_mask)) for e in tok.encode_batch(texts)]
        except Exception:
            pass
    return [len(t) for t in texts]


def plan_batches(lengths: list[int], cap: int = EMBED_BATCH,
                 budget: int = EMBED_TOKEN_BUDGET) -> list[list[int]]:
    """Group indices into batches with `len(batch) * max_len^2 <= budget`.

    Transformer activation peak is O(batch * seq_len^2) and a batch is padded
    to its longest member, so that product — not the document count — is what
    has to be capped. Indices are sorted by length first so similar-length docs
    ride together and one long doc doesn't shrink the batch for many short ones.
    A single document always gets its own batch when it busts the budget alone;
    that case is bounded by EMBED_TEXT_CAP instead.

    Returns batches of original indices; callers must reassemble by index.
    """
    batches: list[list[int]] = []
    cur: list[int] = []
    cur_max = 0
    for i in sorted(range(len(lengths)), key=lambda j: lengths[j]):
        length = max(1, lengths[i])
        new_max = max(cur_max, length)
        if cur and (len(cur) + 1 > cap or (len(cur) + 1) * new_max * new_max > budget):
            batches.append(cur)
            cur, cur_max, new_max = [], 0, length
        cur.append(i)
        cur_max = new_max
    if cur:
        batches.append(cur)
    return batches


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
    """Embed corpus texts. Input is truncated to EMBED_TEXT_CAP chars.

    Batches are packed under EMBED_TOKEN_BUDGET rather than at a fixed count,
    so peak activation memory is bounded regardless of how long the documents
    are. Output order matches input order.
    """
    if not texts:
        return []
    model = _get_model()
    prefixed = [DOC_PREFIX + t[:EMBED_TEXT_CAP] for t in texts]
    out: list = [None] * len(prefixed)
    for batch in plan_batches(_token_lengths(model, prefixed)):
        docs = [prefixed[i] for i in batch]
        for i, v in zip(batch, model.embed(docs, batch_size=len(docs))):
            out[i] = _finalize(v)
    return out


def embed_query(text: str):
    """Embed a single search query."""
    model = _get_model()
    return [_finalize(v) for v in model.embed([QUERY_PREFIX + text[:EMBED_TEXT_CAP]])][0]
