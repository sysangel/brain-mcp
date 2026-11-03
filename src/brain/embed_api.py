"""OpenAI embeddings for the postgres store.

``text-embedding-3-small`` with ``dimensions=256``. No local ONNX model.
Vectors are L2-normalized so cosine is stable across partial batches.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request

from .config import EMBED_TEXT_CAP
from .embedconf import embed_dim, embed_model_name


def embed_material(kind: str, title: str | None, description: str | None, body: str | None) -> str:
    """Text actually sent to the embedder. Full body stays in the row."""
    if kind == "episode":
        text = body or ""
    elif kind in ("memory", "summary"):
        text = f"{description or ''}\n{body or ''}"
    else:
        text = f"{title or ''}\n{description or ''}\n{body or ''}"
    text = text.strip()
    return (text or " ")[:EMBED_TEXT_CAP]


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 0:
        return vec
    return [x / norm for x in vec]


def _timeout_s() -> float:
    raw = (os.environ.get("BRAIN_EMBED_TIMEOUT_S") or "60").strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 60.0


def _retries() -> int:
    raw = (os.environ.get("BRAIN_EMBED_RETRIES") or "3").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 3


def _post_embeddings(payload: dict) -> dict:
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    base = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base + "/embeddings",
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    last: Exception | None = None
    for attempt in range(_retries()):
        try:
            with urllib.request.urlopen(req, timeout=_timeout_s()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last = RuntimeError(f"openai embeddings HTTP {exc.code}: {body[:500]}")
            if exc.code < 500 and exc.code != 429:
                raise last
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
        time.sleep(min(8.0, 0.5 * (2**attempt)))
    raise RuntimeError(f"openai embeddings failed: {last}")


def embed_texts(
    texts: list[str],
    *,
    model: str | None = None,
    dim: int | None = None,
) -> list[list[float]]:
    """Embed ``texts`` in input order. Empty input returns []."""
    if not texts:
        return []
    model = model or embed_model_name()
    if dim is None:
        dim = embed_dim()
    out: list[list[float] | None] = [None] * len(texts)
    batch = 64
    for start in range(0, len(texts), batch):
        chunk = texts[start : start + batch]
        payload = {"model": model, "input": chunk, "dimensions": dim}
        body = _post_embeddings(payload)
        data = body.get("data") or []
        if len(data) != len(chunk):
            raise RuntimeError(
                f"openai returned {len(data)} vectors for {len(chunk)} inputs"
            )
        ordered = sorted(data, key=lambda row: row.get("index", 0))
        for offset, row in enumerate(ordered):
            vec = row.get("embedding")
            if not isinstance(vec, list) or len(vec) != dim:
                raise RuntimeError(
                    f"openai vector length {len(vec) if isinstance(vec, list) else 'missing'}"
                    f" != {dim}"
                )
            out[start + offset] = _normalize([float(x) for x in vec])
    return [v for v in out if v is not None]
