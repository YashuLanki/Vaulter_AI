"""
ingestion/embedder.py
---------------------
Handles all ChromaDB interactions for the Vaulterup ingestion pipeline.

Every chunk is stored with full metadata tags:
  - filename, ingested_at, page_count, ocr_used
  - property, state, category  ← new property-aware tags

This allows searching across all documents OR filtering by property/state.

Examples:
  query_documents("flood zone")                          # search everything
  query_documents("flood zone", state="arizona")         # Arizona only
  query_documents("easements", property="Magic Ranch 50") # one property
"""

import hashlib
import logging
import os
import threading
import time

# Must be set before chromadb/sentence-transformers/transformers are
# imported below -- these libraries print download progress bars and
# telemetry banners straight to stdout on first use, which would corrupt
# the MCP stdio connection to Claude Desktop (see mcp_server.py header).
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import numpy as np
import chromadb
from chromadb import EmbeddingFunction, Documents, Embeddings

from config import CHROMA_DIR, CHROMA_COLLECTION_NAME, EMBEDDING_DIM

log = logging.getLogger("vaulter.embedder")


# ─── Embedding Functions ──────────────────────────────────────────────────────

class LocalHashEmbedding(EmbeddingFunction):
    """
    Deterministic pseudo-embedding based on word position hashing. This is
    NOT semantic search -- cosine similarity here reduces to weighted
    literal word overlap, so synonyms/paraphrases ("inundation area" vs
    "flood zone") won't match. Kept only as an offline fallback for when
    the real model (below) can't be loaded -- no model download required.
    """

    def __init__(self):
        pass

    def __call__(self, input: Documents) -> Embeddings:
        result = []
        for text in input:
            words = text.lower().split()
            vec = np.zeros(EMBEDDING_DIM)
            for i, word in enumerate(words[:500]):
                h = int(hashlib.md5(word.encode()).hexdigest(), 16)
                idx = h % EMBEDDING_DIM
                vec[idx] += 1.0 / (i + 1)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            result.append(vec.tolist())
        return result


_EMBEDDING_FUNCTION = None
_EMBEDDING_INIT_LOCK = threading.Lock()


def get_embedding_function():
    """
    Returns the real semantic embedding function (all-MiniLM-L6-v2, 384
    dimensions -- matches EMBEDDING_DIM) if it can be loaded, falling back
    to LocalHashEmbedding if sentence-transformers isn't installed or the
    model can't be downloaded (e.g. no internet on first run). Loaded once
    and cached -- loading the model is slow, calling it per-chunk is not.
    """
    global _EMBEDDING_FUNCTION
    if _EMBEDDING_FUNCTION is not None:
        return _EMBEDDING_FUNCTION

    with _EMBEDDING_INIT_LOCK:
        if _EMBEDDING_FUNCTION is not None:
            return _EMBEDDING_FUNCTION

        try:
            from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
            _EMBEDDING_FUNCTION = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
            log.info("Using semantic embeddings (all-MiniLM-L6-v2)")
        except Exception as e:
            log.warning(
                f"Could not load sentence-transformers model, falling back to "
                f"non-semantic hash embeddings (search quality will be degraded "
                f"until this is resolved): {e}"
            )
            _EMBEDDING_FUNCTION = LocalHashEmbedding()

        return _EMBEDDING_FUNCTION


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Shared embedding entry point for anything that needs to precompute
    embeddings itself (email/web/property-intel pipelines currently pass
    embeddings explicitly at upsert time instead of letting the collection
    auto-embed) -- keeps every ingestion path using the same function."""
    ef = get_embedding_function()
    result = ef(texts)
    return [r.tolist() if hasattr(r, "tolist") else list(r) for r in result]


# ─── ChromaDB Singleton ───────────────────────────────────────────────────────
#
# Problem: creating a new PersistentClient on every call causes
# "Could not connect to tenant default_tenant" errors when the scheduler,
# watcher, and MCP server all call get_collection() from different threads
# simultaneously — each tries to open the SQLite database at the same time.
#
# Fix: one client, one collection, shared across all threads.
# _INIT_LOCK ensures only one thread initializes at a time.
# _WRITE_LOCK serializes all writes so concurrent upserts don't conflict.

_CLIENT:     chromadb.PersistentClient | None = None
_COLLECTION: object | None                    = None
_INIT_LOCK  = threading.Lock()
_WRITE_LOCK = threading.Lock()


def get_collection():
    """
    Return the shared ChromaDB collection.
    Initializes once on first call (thread-safe).
    Retries up to 3 times on connection errors before raising.
    """
    global _CLIENT, _COLLECTION

    # Fast path — already initialized
    if _COLLECTION is not None:
        return _COLLECTION

    # Slow path — initialize with lock (double-checked locking pattern)
    with _INIT_LOCK:
        if _COLLECTION is not None:
            return _COLLECTION

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                _CLIENT = chromadb.PersistentClient(
                    path=str(CHROMA_DIR),
                    settings=chromadb.config.Settings(anonymized_telemetry=False),
                )
                _COLLECTION = _CLIENT.get_or_create_collection(
                    name=CHROMA_COLLECTION_NAME,
                    embedding_function=get_embedding_function(),
                    metadata={"hnsw:space": "cosine"},
                )
                log.debug("ChromaDB collection initialized")
                return _COLLECTION
            except Exception as e:
                last_error = e
                log.warning(f"ChromaDB init attempt {attempt + 1}/3 failed: {e}")
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))

        raise RuntimeError(
            f"ChromaDB could not be initialized after 3 attempts: {last_error}\n"
            f"Path: {CHROMA_DIR}\n"
            f"Tip: make sure no other process has the database locked, "
            f"and try 'pip install --upgrade chromadb' if the error mentions RustBindingsAPI."
        )


def _reset_collection():
    """
    Force re-initialization of the ChromaDB client on next call.
    Called automatically after write errors so the singleton recovers.
    """
    global _CLIENT, _COLLECTION
    with _INIT_LOCK:
        _CLIENT     = None
        _COLLECTION = None


# ─── Storage ──────────────────────────────────────────────────────────────────

def store_chunks(chunks: list[str], metadata: dict, doc_hash: str):
    """
    Store text chunks in ChromaDB with full metadata tags including
    property, state, and category for property-aware search.

    Write operations are serialized with _WRITE_LOCK to prevent
    concurrent-write conflicts across the watcher, scheduler, and
    MCP server threads.
    """
    if not chunks:
        log.warning(f"No chunks to store for {metadata['filename']}")
        return

    ids = [f"{doc_hash}_{i}" for i in range(len(chunks))]
    metadatas = [
        {
            "filename":     metadata["filename"],
            "ingested_at":  metadata["ingested_at"],
            "page_count":   str(metadata["page_count"]),
            "has_tables":   str(metadata["has_tables"]),
            "ocr_used":     str(metadata["ocr_used"]),
            "property":     metadata.get("property", "unknown"),
            "state":        metadata.get("state", "unknown"),
            "category":     metadata.get("category", "unknown"),
            "chunk_index":  str(i),
            "total_chunks": str(len(chunks)),
            "doc_hash":     doc_hash,
        }
        for i in range(len(chunks))
    ]

    with _WRITE_LOCK:
        try:
            collection = get_collection()
            collection.upsert(documents=chunks, metadatas=metadatas, ids=ids)
            log.info(f"  Stored {len(chunks)} chunks in ChromaDB")
        except Exception as e:
            log.error(f"  ChromaDB write failed: {e} — resetting client for next attempt")
            _reset_collection()
            raise


def reindex_all(batch_size: int = 200) -> dict:
    """
    Re-embeds every chunk already in the collection using whatever
    embedding function is active right now.

    Needed after switching embedding functions (e.g. the old hash-based
    embedding -> real semantic embeddings): ChromaDB does NOT retroactively
    re-embed existing data just because the collection's configured
    embedding_function changed -- old chunks keep their old vectors
    forever unless something re-upserts them. Without running this, only
    newly-ingested documents would benefit from better search; everything
    ingested before the switch would still return poor/irrelevant results.

    Safe to run more than once (unconditionally re-embeds everything each
    time) and safe to run while the watcher/scheduler are active -- writes
    go through the same _WRITE_LOCK as normal ingestion.
    """
    collection = get_collection()
    total = collection.count()
    if total == 0:
        return {"total": 0, "reembedded": 0}

    reembedded = 0
    offset = 0
    while offset < total:
        batch = collection.get(limit=batch_size, offset=offset, include=["documents", "metadatas"])
        ids, docs, metas = batch["ids"], batch["documents"], batch["metadatas"]
        if not ids:
            break

        embeddings = embed_texts(docs)
        with _WRITE_LOCK:
            collection.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=embeddings)

        reembedded += len(ids)
        offset += batch_size
        log.info(f"Reindexed {reembedded}/{total} chunks...")

    return {"total": total, "reembedded": reembedded}


# ─── Retrieval ────────────────────────────────────────────────────────────────

def query_documents(
    question: str,
    n_results: int = 5,
    state: str = None,
    property_name: str = None,
) -> list[dict]:
    """
    Search ChromaDB for chunks relevant to a question.

    Optional filters:
      state         — only search documents from this state
      property_name — only search documents from this property

    Examples:
      query_documents("flood zone")
      query_documents("easements", state="arizona")
      query_documents("legal description", property_name="Magic Ranch 50")
    """
    collection = get_collection()
    count = collection.count()

    if count == 0:
        return []

    # Build optional where filter
    where = None
    if state and property_name:
        where = {"$and": [{"state": state}, {"property": property_name}]}
    elif state:
        where = {"state": state}
    elif property_name:
        where = {"property": property_name}

    query_params = {
        "query_texts": [question],
        "n_results": min(n_results, count),
    }
    if where:
        query_params["where"] = where

    results = collection.query(**query_params)

    output = []
    if results and results["documents"]:
        for i, doc in enumerate(results["documents"][0]):
            meta = results["metadatas"][0][i]
            dist = results["distances"][0][i] if results.get("distances") else None
            output.append({
                "text":      doc,
                "filename":  meta.get("filename"),
                "property":  meta.get("property"),
                "state":     meta.get("state"),
                "category":  meta.get("category"),
                "chunk":     meta.get("chunk_index"),
                "ocr":       meta.get("ocr_used"),
                "score":     round(1 - dist, 4) if dist is not None else None,
            })
    return output


def get_stats() -> dict:
    """Return a summary of what is currently stored in ChromaDB."""
    collection = get_collection()
    return {"total_chunks": collection.count()}
