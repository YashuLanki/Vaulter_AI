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
import threading
import time

import numpy as np
import chromadb
from chromadb import EmbeddingFunction, Documents, Embeddings

from config import CHROMA_DIR, CHROMA_COLLECTION_NAME, EMBEDDING_DIM

log = logging.getLogger("vaulter.embedder")


# ─── Embedding Function ───────────────────────────────────────────────────────

class LocalHashEmbedding(EmbeddingFunction):
    """
    Deterministic pseudo-embedding based on word position hashing.
    No model downloads required — safe for offline environments.

    Production upgrade path:
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
        ef = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
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
                _CLIENT = chromadb.PersistentClient(path=str(CHROMA_DIR))
                _COLLECTION = _CLIENT.get_or_create_collection(
                    name=CHROMA_COLLECTION_NAME,
                    embedding_function=LocalHashEmbedding(),
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
            collection.add(documents=chunks, metadatas=metadatas, ids=ids)
            log.info(f"  Stored {len(chunks)} chunks in ChromaDB")
        except Exception as e:
            log.error(f"  ChromaDB write failed: {e} — resetting client for next attempt")
            _reset_collection()
            raise


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
