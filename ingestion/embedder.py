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
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time

# Must be set before chromadb is imported below -- it prints a telemetry
# banner straight to stdout on first use otherwise, which would corrupt
# the MCP stdio connection to Claude Desktop (see mcp_server.py header).
# (The three HF_HUB_/TRANSFORMERS_/TOKENIZERS_ env vars this block used
# to set are gone along with sentence-transformers/transformers/tokenizers
# -- those packages are no longer a dependency, so setting env vars only
# they read would be meaningless dead code.)
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

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
_EMBEDDING_MODEL_NAME: str | None = None  # identifies which function produced a chunk's vector
_EMBEDDING_INIT_LOCK = threading.Lock()


def get_embedding_function():
    """
    Returns the real semantic embedding function (ChromaDB's built-in
    ONNXMiniLM_L6_V2 -- the same all-MiniLM-L6-v2 model, packaged as a
    lightweight ONNX model instead of requiring the full sentence-
    transformers/torch stack -- 384 dimensions, matches EMBEDDING_DIM) if
    it can be loaded, falling back to LocalHashEmbedding if the model
    can't be downloaded (e.g. no internet on first run). Loaded once and
    cached -- loading the model is slow, calling it per-chunk is not.
    """
    global _EMBEDDING_FUNCTION, _EMBEDDING_MODEL_NAME
    if _EMBEDDING_FUNCTION is not None:
        return _EMBEDDING_FUNCTION

    with _EMBEDDING_INIT_LOCK:
        if _EMBEDDING_FUNCTION is not None:
            return _EMBEDDING_FUNCTION

        try:
            from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
            _EMBEDDING_FUNCTION = ONNXMiniLM_L6_V2()
            _EMBEDDING_MODEL_NAME = "onnx-all-MiniLM-L6-v2"
            log.info("Using semantic embeddings (onnx-all-MiniLM-L6-v2)")
        except Exception as e:
            log.warning(
                f"Could not load the ONNX embedding model, falling back to "
                f"non-semantic hash embeddings (search quality will be degraded "
                f"until this is resolved): {e}"
            )
            _EMBEDDING_FUNCTION = LocalHashEmbedding()
            _EMBEDDING_MODEL_NAME = "hash-fallback-v1"

        return _EMBEDDING_FUNCTION


def get_embedding_model_name() -> str:
    """The identifier of whichever embedding function is currently active
    (e.g. 'onnx-all-MiniLM-L6-v2' or 'hash-fallback-v1'). Stamped onto
    every stored chunk's metadata so a later mismatch (see
    check_embedding_freshness) can be detected without guessing."""
    get_embedding_function()  # ensures _EMBEDDING_MODEL_NAME is set
    return _EMBEDDING_MODEL_NAME


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


def _is_embedding_function_conflict(e: Exception) -> bool:
    """True if `e` is ChromaDB refusing to open a collection because the
    embedding function requested now differs from whichever one was
    persisted when the collection was first created (e.g. an existing
    user's database, created under the old sentence-transformers
    function, opened by code that now requests the new ONNX one -- see
    _migrate_collection_to_current_embedding_function's docstring for
    why this needs handling here rather than just letting it raise)."""
    return isinstance(e, ValueError) and "embedding function" in str(e).lower()


def _migrate_collection_to_current_embedding_function(client) -> object:
    """
    Recreates CHROMA_COLLECTION_NAME so it can be opened under the
    CURRENT embedding function, preserving every existing chunk's data
    and vectors completely unchanged.

    Why this is needed: ChromaDB persists which embedding function a
    collection was created with, and refuses to open it with a
    DIFFERENT one -- get_or_create_collection raises ValueError
    ("Embedding function conflict") rather than just returning stale
    results. This is fine and invisible for a brand new install (nothing
    exists yet to conflict with), but for anyone with an EXISTING
    database -- created under a since-replaced embedding function (e.g.
    this project's own move from sentence-transformers to ChromaDB's
    built-in ONNX model) -- every single call to get_collection() would
    otherwise raise immediately, breaking every MCP tool that touches
    the database (search, storage, the freshness check, reindexing
    itself) the moment the new code runs against their old database.
    ChromaDB also has no supported way to change a collection's
    embedding-function TYPE in place (collection.modify(configuration=
    {"embedding_function": ...}) explicitly rejects a type change, as
    verified directly against this chromadb version) -- a rebuild is the
    only path.

    Populates a TEMPORARY, separately-named collection first, verifies
    every row copied over correctly, and only THEN deletes the old
    collection and renames the temporary one into place (collection
    rename IS supported by ChromaDB, unlike an embedding-function-type
    change) -- deliberately never deleting the original data until a
    fully-populated replacement already exists and is confirmed intact.
    A naive delete-then-recreate-then-repopulate ordering would leave a
    window where a mid-migration crash (process killed, disk full)
    permanently loses every chunk with no recovery path -- unacceptable
    here since some of this data (email/web/property-intelligence
    content) cannot always be re-ingested from its original source
    afterward. This ordering closes that window: the old collection
    stays fully intact and reachable under its original name for the
    entire copy, and is only ever removed after its replacement is
    verified complete.

    This function does NOT re-embed anything -- it copies every existing
    id/document/metadata/embedding across UNCHANGED (including each
    chunk's existing "embedding_model" tag). This is deliberately cheap
    (no model calls) and leaves the data exactly as
    check_embedding_freshness() already expects it: still tagged with
    the OLD model name, so it's correctly detected as stale and picked
    up by the already-existing reindex_all() flow -- which is what
    actually recomputes each chunk's real vector, exactly as before this
    fix. This function only unblocks OPENING the collection; it doesn't
    change what "stale" means or how staleness gets fixed.
    """
    temp_name = f"{CHROMA_COLLECTION_NAME}__migrating"
    # Clean up any leftover temp collection from a previous run that
    # crashed before completing (e.g. process killed mid-migration) --
    # that attempt's old collection is still intact under its original
    # name (this function never deletes it until the temp copy is
    # verified), so it's always safe to discard a stale, unverified temp
    # collection and simply try the whole migration again from scratch.
    try:
        client.delete_collection(name=temp_name)
    except Exception:
        pass

    old_collection = client.get_collection(name=CHROMA_COLLECTION_NAME)
    existing = old_collection.get(include=["documents", "metadatas", "embeddings"])
    total = len(existing["ids"])
    log.warning(
        f"Collection '{CHROMA_COLLECTION_NAME}' was created under a different embedding "
        f"function than the one now active -- migrating it ({total} existing chunk(s), "
        f"vectors unchanged) so it can be opened going forward. Chunks keep their existing "
        f"embedding_model tag, so the normal check_embedding_freshness()/reindex flow will "
        f"still detect and re-embed them as usual."
    )

    temp_collection = client.create_collection(
        name=temp_name,
        embedding_function=get_embedding_function(),
        metadata={"hnsw:space": "cosine"},
    )
    if total:
        temp_collection.upsert(
            ids=existing["ids"],
            documents=existing["documents"],
            embeddings=existing["embeddings"],
            metadatas=existing["metadatas"],
        )

    copied = temp_collection.count()
    if copied != total:
        # Don't touch the original -- leave the incomplete temp copy
        # for the next attempt to clean up and redo, and surface this
        # clearly rather than silently proceeding with partial data.
        raise RuntimeError(
            f"Migration verification failed: expected {total} chunks in the temporary "
            f"collection, found {copied}. The original collection '{CHROMA_COLLECTION_NAME}' "
            f"was not touched and remains intact; this migration will be retried."
        )

    # Only now, with a verified, fully-populated replacement already in
    # place, remove the original and take its name.
    client.delete_collection(name=CHROMA_COLLECTION_NAME)
    temp_collection.modify(name=CHROMA_COLLECTION_NAME)
    log.warning(f"Migration of '{CHROMA_COLLECTION_NAME}' complete -- {copied} chunk(s) re-inserted.")
    return temp_collection


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
                try:
                    _COLLECTION = _CLIENT.get_or_create_collection(
                        name=CHROMA_COLLECTION_NAME,
                        embedding_function=get_embedding_function(),
                        metadata={"hnsw:space": "cosine"},
                    )
                except Exception as e:
                    if not _is_embedding_function_conflict(e):
                        raise
                    _COLLECTION = _migrate_collection_to_current_embedding_function(_CLIENT)
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
    embedding_model = get_embedding_model_name()
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
            # Which embedding function produced this chunk's vector -- lets
            # check_embedding_freshness() detect chunks left behind by an
            # embedding-model upgrade instead of silently degrading search.
            "embedding_model": embedding_model,
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

    current_model = get_embedding_model_name()
    reembedded = 0
    offset = 0
    while offset < total:
        batch = collection.get(limit=batch_size, offset=offset, include=["documents", "metadatas"])
        ids, docs, metas = batch["ids"], batch["documents"], batch["metadatas"]
        if not ids:
            break

        # Stamp the model that's actually producing these fresh embeddings
        # so a later freshness check sees these chunks as up to date.
        for meta in metas:
            meta["embedding_model"] = current_model

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


def check_embedding_freshness(sample_limit: int = 500) -> dict:
    """
    Samples existing chunks and checks how many were embedded with a
    DIFFERENT model than whatever is active right now (or predate the
    "embedding_model" tag entirely, e.g. chunks stored before this check
    existed -- treated the same as stale, since there's no way to tell
    which model actually produced their vector).

    This is the only way a switch like the old hash-based embedding ->
    real semantic embeddings gets surfaced to a user at all: ChromaDB does
    NOT retroactively re-embed existing data on its own (see
    reindex_all's docstring), so without this check, older documents would
    just silently return worse search results forever with no signal that
    'python main.py reindex' would fix it.
    """
    collection = get_collection()
    total = collection.count()
    if total == 0:
        return {"needs_reindex": False, "stale_chunks_sampled": 0, "chunks_sampled": 0}

    current_model = get_embedding_model_name()
    sample = collection.get(limit=min(sample_limit, total), include=["metadatas"])
    metas = sample.get("metadatas") or []

    stale = sum(1 for m in metas if m.get("embedding_model") != current_model)

    return {
        "needs_reindex":         stale > 0,
        "stale_chunks_sampled":  stale,
        "chunks_sampled":        len(metas),
        "current_embedding_model": current_model,
    }


def get_stats() -> dict:
    """Return a summary of what is currently stored in ChromaDB, including
    whether some chunks were embedded with a different (e.g. pre-upgrade)
    model and would benefit from 'python main.py reindex'."""
    collection = get_collection()
    stats = {"total_chunks": collection.count()}
    stats.update(check_embedding_freshness())
    return stats
