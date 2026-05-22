"""The embedder. Turns text into vectors so the brain can search by meaning.

Default: local sentence-transformers. Free, runs on CPU, the text never
leaves the machine. 384 dimensions.

Opt-in via env var SOLOMON_EMBEDDING_PROVIDER=openai: OpenAI
text-embedding-3-small, 1536 dims. Costs money per call, but scales
better for big tenants.

We support both behind the same `embed(text) -> List[float]` interface
so the rest of Solomon never knows or cares which one is running.

If the user switches providers mid-life (e.g. local -> openai later),
all old embeddings become unusable because the dimensions and meaning
differ. We log a warning and require a full re-embed. The schema's
`vector(N)` column has to match the provider too — we ship with N=384
(local default). If a tenant flips to OpenAI, they migrate the column
to vector(1536) and re-embed.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import List, Optional

logger = logging.getLogger("solomon.ingestion.embedder")

# Two providers, same interface.
_LOCAL_MODEL = None
_LOCAL_LOCK = threading.Lock()

DEFAULT_LOCAL_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
LOCAL_DIM = 384
OPENAI_DIM = 1536


def _get_provider() -> str:
    return os.getenv("SOLOMON_EMBEDDING_PROVIDER", "local").lower()


def dimension() -> int:
    """Return the dim of vectors this embedder produces. The schema's
    `vector(N)` column has to match this.
    """
    return OPENAI_DIM if _get_provider() == "openai" else LOCAL_DIM


def embed(text: str) -> Optional[List[float]]:
    """Return an embedding for `text`. None on failure (so callers can
    skip cleanly instead of crashing the pipeline).
    """
    if not text or not text.strip():
        return None
    provider = _get_provider()
    try:
        if provider == "openai":
            return _embed_openai(text)
        return _embed_local(text)
    except Exception as e:  # noqa: BLE001
        logger.warning("Embedder (%s) failed: %s", provider, e)
        return None


def embed_batch(texts: List[str]) -> List[Optional[List[float]]]:
    """Batch interface. Returns a list of vectors (or None) in the same
    order as inputs. Local model batches efficiently; OpenAI does too.
    """
    if not texts:
        return []
    provider = _get_provider()
    try:
        if provider == "openai":
            return _embed_openai_batch(texts)
        return _embed_local_batch(texts)
    except Exception as e:  # noqa: BLE001
        logger.warning("Embedder batch (%s) failed: %s", provider, e)
        return [None] * len(texts)


# ---------------------------------------------------------------------------
# Local: sentence-transformers
# ---------------------------------------------------------------------------

def _local_model():  # type: ignore[no-untyped-def]
    """Lazy-load the model. First call downloads ~90MB; subsequent calls
    are instant.
    """
    global _LOCAL_MODEL
    if _LOCAL_MODEL is not None:
        return _LOCAL_MODEL
    with _LOCAL_LOCK:
        if _LOCAL_MODEL is not None:
            return _LOCAL_MODEL
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError(
                "sentence-transformers is not installed. Run "
                "`pip install sentence-transformers` or switch to "
                "SOLOMON_EMBEDDING_PROVIDER=openai."
            ) from e
        model_name = os.getenv("SOLOMON_LOCAL_EMBEDDING_MODEL", DEFAULT_LOCAL_MODEL)
        logger.info("Loading local embedding model: %s (first run downloads weights)", model_name)
        _LOCAL_MODEL = SentenceTransformer(model_name, device="cpu")
        return _LOCAL_MODEL


def _embed_local(text: str) -> List[float]:
    vec = _local_model().encode([text], normalize_embeddings=True)[0]
    return vec.tolist()


def _embed_local_batch(texts: List[str]) -> List[Optional[List[float]]]:
    # Filter out empty strings; preserve positions with None placeholders.
    indices_and_texts = [(i, t) for i, t in enumerate(texts) if t and t.strip()]
    if not indices_and_texts:
        return [None] * len(texts)
    only_texts = [t for _, t in indices_and_texts]
    vecs = _local_model().encode(only_texts, normalize_embeddings=True, batch_size=32, show_progress_bar=False)
    out: List[Optional[List[float]]] = [None] * len(texts)
    for (i, _), v in zip(indices_and_texts, vecs):
        out[i] = v.tolist()
    return out


# ---------------------------------------------------------------------------
# OpenAI: text-embedding-3-small
# ---------------------------------------------------------------------------

def _embed_openai(text: str) -> List[float]:
    return _embed_openai_batch([text])[0]  # type: ignore[return-value]


def _embed_openai_batch(texts: List[str]) -> List[Optional[List[float]]]:
    import httpx
    key = os.getenv("OPENAI_API_KEY") or os.getenv("SOLOMON_OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set; cannot use openai embeddings provider.")
    model = os.getenv("SOLOMON_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
    # Filter empties.
    indices_and_texts = [(i, t) for i, t in enumerate(texts) if t and t.strip()]
    if not indices_and_texts:
        return [None] * len(texts)
    only_texts = [t for _, t in indices_and_texts]
    with httpx.Client(timeout=60.0) as client:
        r = client.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "input": only_texts},
        )
        r.raise_for_status()
        data = r.json()
    vectors = [item["embedding"] for item in data["data"]]
    out: List[Optional[List[float]]] = [None] * len(texts)
    for (i, _), v in zip(indices_and_texts, vectors):
        out[i] = v
    return out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def store_embedding(tenant_id: str, source_table: str, source_id: int, vector: List[float]) -> Optional[int]:
    """Persist one vector. Returns embedding_id or None on failure."""
    from ..storage.pool import get_pool
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO embeddings (tenant_id, source_table, source_id, vector) "
                    "VALUES (%s, %s, %s, %s) RETURNING embedding_id;",
                    (tenant_id, source_table, source_id, vector),
                )
                row = cur.fetchone()
            conn.commit()
            return int(row[0]) if row else None
    except Exception as e:  # noqa: BLE001
        logger.warning("store_embedding failed: %s", e)
        return None
