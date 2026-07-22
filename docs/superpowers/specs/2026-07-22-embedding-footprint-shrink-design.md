# Design: Shrink the search-engine footprint (drop torch/sentence-transformers)

**Date:** 2026-07-22
**Status:** Proposed — awaiting review
**Part of:** Multi-user onboarding improvements (see `docs/MULTI_USER_TRANSITION.md`). This is
step 1 of a two-step plan agreed with the project owner: shrink the install footprint first
(this doc), then package the result as a one-click Claude Desktop Extension (`.mcpb`) in a
follow-up design.

## Problem

Every staff member's install currently weighs **~1.5 GB**, and roughly **540 MB of that is a
single library ("torch")** used for exactly one purpose: powering the semantic search engine
(`ingestion/embedder.py`'s `SentenceTransformerEmbeddingFunction`, model `all-MiniLM-L6-v2`).

Torch is also the single most failure-prone dependency in `requirements.txt` — it ships
different large binary wheels per OS/CPU-architecture/Python-version combination, and is the
most likely reason a `pip install` fails, is unusually slow, or behaves differently across two
staff members' machines (exactly the "why doesn't it work for me" support-ticket risk flagged in
`docs/MULTI_USER_TRANSITION.md`'s Theme 1).

None of that size or fragility buys anything unique: **ChromaDB (already a hard dependency of
this project) ships its own built-in embedding function, `ONNXMiniLM_L6_V2`, using the exact
same underlying model** (`all-MiniLM-L6-v2`), at a fraction of the footprint, with no separate
ML framework required.

## What's being proposed

Swap `ingestion/embedder.py`'s real (non-fallback) embedding function from
`SentenceTransformerEmbeddingFunction` to ChromaDB's own `ONNXMiniLM_L6_V2`, and remove
`sentence-transformers` from `requirements.txt` (torch is a transitive dependency of
`sentence-transformers`, not a direct one, so removing that one line drops both).

This is a **narrow, single-file change** — confirmed by searching the whole codebase that
nothing outside `ingestion/embedder.py` imports `torch` or `sentence_transformers` directly.
Every other module already goes through `embedder.py`'s `get_embedding_function()` /
`embed_texts()` / `get_embedding_model_name()` — none of that public interface changes shape.

## Facts verified before writing this spec (not assumed)

- `ONNXMiniLM_L6_V2` produces **384-dimensional vectors** — identical to `EMBEDDING_DIM` in
  `config.py` and to what `SentenceTransformerEmbeddingFunction` produces today. No dimension
  migration, no `EMBEDDING_DIM` change.
- Ran it for real: init took ~0.05s (vs. multiple seconds to load a full sentence-transformers
  model), embedding three short strings took ~0.09s.
- Ran a genuine semantic-similarity check (not just "it returns numbers"): cosine similarity
  between "flood zone risk assessment" and the zero-word-overlap paraphrase "inundation area
  hazard study" was **0.40** (meaningfully positive — a real synonym match), versus **-0.05**
  against an unrelated sentence about cats. This is direct evidence the swap preserves the
  "inundation area" vs "flood zone" synonym-matching quality `embedder.py`'s own docstring
  calls out as the reason semantic embeddings matter over the hash fallback.
- `onnxruntime` (what `ONNXMiniLM_L6_V2` runs on) is **already a mandatory dependency of
  chromadb itself** (`onnxruntime>=1.14.1` in chromadb's own declared dependencies) — confirmed
  via `importlib.metadata.requires('chromadb')`. Nothing new is being added; only
  `sentence-transformers` (and therefore torch) is being removed.
- The ONNX model file downloads once to `~/.cache/chroma/onnx_models/` (~90–165 MB on disk,
  comparable to or smaller than sentence-transformers' own cached PyTorch weights) — the real
  savings is in the **installed package tree** (`venv/`), not a wash at the cache layer.
- `venv/lib/.../site-packages/torch` measured at **535 MB**, `sentence_transformers` at 4.9 MB,
  on this machine's current install.

## Migration path for databases that already have data

This project **already has the exact machinery needed** for this migration, built for a prior
embedding-model change — nothing new to invent:

- Every stored chunk is tagged with which embedding function produced it
  (`get_embedding_model_name()`, stored as chunk metadata).
- `check_embedding_freshness()` samples existing chunks and flags a mismatch against whatever
  function is active now.
- `get_stats()` (surfaced through `get_database_stats` and `check_system_health`) already
  reports `needs_reindex` in plain English when a mismatch is found, with the exact fix
  (`python main.py reindex`).
- `reindex_all()` and the `python main.py reindex` CLI command already exist and already
  re-embed every stored chunk with whichever function is currently active.

**Fresh installs (new staff) are unaffected** — they start with an empty database, so there's
nothing to migrate. **Existing installs** (anyone already running this, including the project
owner's own machine with 1,331 chunks) will see `needs_reindex: true` the next time they check
stats or start a conversation, with a plain-English fix already surfaced by tools that exist
today.

**Decision: this is included, not optional.** Since `apply_pending_update` (Priority 4's
auto-update tool) already exists, and this change ships as a normal code update,
`apply_pending_update()` in `apply_update.py` will run `python main.py reindex` automatically,
right after syncing files and refreshing dependencies, whenever the applied update changes which
embedding model is active. This spares a non-technical user from ever needing to notice
`needs_reindex` or run a second command by hand — the whole point of Priority 4 was removing
manual steps, and leaving reindex as a manual follow-up would undercut that. This is a small,
additive step inside the existing `apply_pending_update()` flow, not a new mechanism — and it
only ever runs the ONE TIME the model actually changes, not on every update.

## What does NOT change

- `EMBEDDING_DIM`, ChromaDB collection schema, chunk metadata shape (aside from the model-name
  tag value itself).
- The hash-based fallback (`LocalHashEmbedding`) and its use as a last-resort when no real
  embedding function can be loaded — untouched, still the safety net for "offline on first run,
  can't download any model."
- Every caller of `embedder.py` (`ingestion/watcher.py`, `pipeline/*.py`, `analysis/rag_engine.py`,
  `mcp_server.py`) — none of them reference torch/sentence-transformers directly, and the public
  functions they call keep the same names and signatures.
- Search *quality* — same model, same vectors, same dimension.

## Testing plan

1. Confirm `ONNXMiniLM_L6_V2` produces 384-dim vectors and passes a real semantic-similarity
   sanity check (synonym pair scores meaningfully higher than an unrelated pair) — already done
   above; will be re-run as an automated check, not just an ad hoc script.
2. Confirm the hash fallback path still activates correctly if the ONNX function can't load
   (e.g. simulate an import failure) and that `get_embedding_model_name()` correctly reports
   `hash-fallback-v1` in that case, matching current behavior.
3. Confirm `check_embedding_freshness()` / `get_stats()` correctly flags `needs_reindex: true`
   against a database seeded with the OLD model-name tag, and correctly reports healthy against
   one seeded with the new tag.
4. Confirm `python main.py reindex` actually re-embeds a small seeded database end-to-end and
   clears `needs_reindex`, and confirm the auto-reindex step inside `apply_pending_update` triggers
   it correctly when (and only when) the embedding model actually changed.
5. Confirm a fresh `pip install -r requirements.txt` no longer pulls `torch` at all, and measure
   the resulting `venv/` size drop.
6. Run the existing `search_database` / `get_property_info` MCP tool paths against real (or
   representative sample) ingested content before and after, to confirm search results remain
   sensible — not a formal quality benchmark, but a real smoke test, not just a unit test on
   embedding vectors in isolation.

## Out of scope for this design

- Packaging as a one-click Claude Desktop Extension (`.mcpb`) — separate, follow-up design, as
  agreed with the project owner.
- Any change to OCR tooling, the scheduler, auto-update mechanics, or anything in
  `analysis/screening/` — untouched by this change.
- Re-evaluating embedding model *choice* (e.g. a different/better model) — out of scope; this is
  a footprint swap of the same model, not a search-quality project.
