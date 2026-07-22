# Embedding Footprint Shrink Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Swap the project's semantic search engine from `sentence-transformers` (which pulls in
a ~535MB library called torch) to ChromaDB's own built-in `ONNXMiniLM_L6_V2` embedding function
(same model, same 384-dim vectors, no torch), shrinking every install by roughly 540MB and
removing the single most failure-prone dependency in the project — with existing installs
migrated automatically and non-technical-user-safely through the update mechanism that already
exists.

**Architecture:** A narrow, surgical change to one core file (`ingestion/embedder.py`) plus three
small follow-on edits (`requirements.txt`, `apply_update.py`, `mcp_server.py`). No new files, no
new mechanisms — this plan reuses `check_embedding_freshness()` / `reindex_all()` /
`python main.py reindex`, all of which already exist and already handle exactly this kind of
embedding-model swap.

**Tech Stack:** Python 3.11+, ChromaDB (`chromadb.utils.embedding_functions.ONNXMiniLM_L6_V2`,
already a transitive dependency via `onnxruntime`), no new packages added.

## Global Constraints

- **This codebase has no pytest and no test framework** (confirmed in `CLAUDE.md`: "There is no
  lint/test framework configured (no pytest, no linter config)"). Every "test" step in this plan
  is a plain runnable Python one-liner or short script executed via the shell, matching this
  project's existing convention (`test_screening.py` is a manual smoke-test script, not a pytest
  suite). Do NOT introduce pytest, a `tests/` directory, or any new test framework as part of this
  plan — that would be out of scope and inconsistent with the existing codebase.
- **`EMBEDDING_DIM` in `config.py` stays `384`** — verified both the old and new embedding
  functions produce 384-dimensional vectors. No change to `config.py` needed anywhere in this
  plan.
- **Nothing outside `ingestion/embedder.py` imports `torch` or `sentence_transformers` directly**
  (verified via `grep -rn "sentence_transformers\|import torch\|from torch" --include="*.py" .`
  across the whole repo, excluding `venv/`) — every other module goes through
  `get_embedding_function()` / `embed_texts()` / `get_embedding_model_name()`, none of which
  change their name or signature in this plan.
- **`onnxruntime` needs no new installation** — confirmed via
  `importlib.metadata.requires('chromadb')` that it's already a hard dependency of `chromadb`
  itself (`onnxruntime>=1.14.1`).
- **Run every verification step for real** against this actual project (its real `venv`, real
  `ingestion/embedder.py`) — not a fabricated stand-in — except where a step explicitly says to
  use an isolated scratch copy (Tasks 4 and 5 need this, specifically to avoid touching the real,
  currently-live ChromaDB database with 1,331+ real chunks before Task 7's final real migration).
- **Never touch `confidentials/` or the real `data/chroma_db/` database** except in Task 7, which
  is explicitly the intended one-time real migration for this machine.

---

### Task 1: Swap the real embedding function to ChromaDB's built-in ONNX model

**Files:**
- Modify: `ingestion/embedder.py:24-31` (telemetry env vars — remove now-obsolete ones)
- Modify: `ingestion/embedder.py:77-107` (`get_embedding_function()` — swap the model)

**Interfaces:**
- Consumes: nothing new.
- Produces: `get_embedding_function()` still returns an object implementing ChromaDB's
  `EmbeddingFunction` interface (callable with a list of strings, returns a list of vectors).
  `get_embedding_model_name()` now returns the string `"onnx-all-MiniLM-L6-v2"` instead of
  `"all-MiniLM-L6-v2"` when the real (non-fallback) function loads successfully — this name
  change is required, not cosmetic (see Task 1, Step 5's note on why).

- [ ] **Step 1: Write the failing verification script**

Create a temporary script (do not commit this file — it's a throwaway verification script, same
convention used throughout this project's own development so far). Save it as
`/tmp/verify_embedder_swap.py`:

```python
import sys
sys.path.insert(0, "/Users/ylanki/vaulter_ai")

from ingestion.embedder import get_embedding_function, get_embedding_model_name, embed_texts
import numpy as np

model_name = get_embedding_model_name()
print("model name:", model_name)
assert model_name == "onnx-all-MiniLM-L6-v2", f"expected the new ONNX model name, got {model_name!r}"

vectors = embed_texts([
    "flood zone risk assessment",
    "inundation area hazard study",
    "unrelated topic about cats",
])
assert len(vectors) == 3
assert len(vectors[0]) == 384, f"expected 384 dimensions, got {len(vectors[0])}"

def cos(a, b):
    a, b = np.array(a), np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

sim_synonyms = cos(vectors[0], vectors[1])
sim_unrelated = cos(vectors[0], vectors[2])
print(f"sim(flood zone, inundation area) = {sim_synonyms:.3f}  (expect meaningfully positive)")
print(f"sim(flood zone, cats)            = {sim_unrelated:.3f}  (expect near zero or negative)")
assert sim_synonyms > 0.2, "synonym pair should score meaningfully higher than an unrelated pair"
assert sim_synonyms > sim_unrelated

print("ALL CHECKS PASSED")
```

- [ ] **Step 2: Run it to confirm it currently fails**

Run: `cd /Users/ylanki/vaulter_ai && ./venv/bin/python3 /tmp/verify_embedder_swap.py`

Expected: `AssertionError` on the `model_name == "onnx-all-MiniLM-L6-v2"` line, since the
unmodified code still reports `"all-MiniLM-L6-v2"` (the old sentence-transformers name).

- [ ] **Step 3: Edit the telemetry env-var block**

In `ingestion/embedder.py`, replace lines 24-31:

```python
# Must be set before chromadb/sentence-transformers/transformers are
# imported below -- these libraries print download progress bars and
# telemetry banners straight to stdout on first use, which would corrupt
# the MCP stdio connection to Claude Desktop (see mcp_server.py header).
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
```

with:

```python
# Must be set before chromadb is imported below -- it prints a telemetry
# banner straight to stdout on first use otherwise, which would corrupt
# the MCP stdio connection to Claude Desktop (see mcp_server.py header).
# (The three HF_HUB_/TRANSFORMERS_/TOKENIZERS_ env vars this block used
# to set are gone along with sentence-transformers/transformers/tokenizers
# -- those packages are no longer a dependency, so setting env vars only
# they read would be meaningless dead code.)
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
```

- [ ] **Step 4: Edit `get_embedding_function()`**

In `ingestion/embedder.py`, replace lines 77-107 (the whole function) with:

```python
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
```

Note: the model name is **deliberately changing** from `"all-MiniLM-L6-v2"` to
`"onnx-all-MiniLM-L6-v2"`, not staying the same. `ONNXMiniLM_L6_V2` is a different runtime
(ONNX-quantized) than `SentenceTransformerEmbeddingFunction`'s PyTorch model, and while both
represent "the same model" conceptually, their output vectors are not guaranteed to be
numerically identical. Reusing the old name would make `check_embedding_freshness()` (Task 4)
silently treat old and new vectors as equivalent, permanently mixing them in the same collection
with no way to detect or fix it. A new name forces the existing freshness/reindex machinery to
do its job — which is the whole point of Task 5.

- [ ] **Step 5: Run the verification script again to confirm it passes**

Run: `cd /Users/ylanki/vaulter_ai && ./venv/bin/python3 /tmp/verify_embedder_swap.py`

Expected output (exact similarity numbers may vary slightly, but the assertions must all pass):
```
model name: onnx-all-MiniLM-L6-v2
sim(flood zone, inundation area) = 0.396  (expect meaningfully positive)
sim(flood zone, cats)            = -0.054  (expect near zero or negative)
ALL CHECKS PASSED
```

- [ ] **Step 6: Confirm no stray stdout output during a genuinely cold model download**

This confirms the MCP-stdio-safety concern the file's own comments call out — specifically for
ChromaDB's tqdm-based download progress bar, which defaults to `sys.stderr` (not stdout), but
must be proven, not assumed, since a stray stdout write here would silently corrupt the MCP
connection to Claude Desktop in production.

Run:
```bash
mv ~/.cache/chroma/onnx_models ~/.cache/chroma/onnx_models.bak
cd /Users/ylanki/vaulter_ai
./venv/bin/python3 -c "
from ingestion.embedder import get_embedding_function
import sys
ef = get_embedding_function()
print('MARKER_STDOUT_IS_CLEAN', file=sys.stdout)
" > /tmp/stdout_capture.txt 2> /tmp/stderr_capture.txt
cat /tmp/stdout_capture.txt
```

Expected: `/tmp/stdout_capture.txt` contains **only** the line `MARKER_STDOUT_IS_CLEAN` — no
progress bar characters, no download banners, nothing else. (`/tmp/stderr_capture.txt` may
contain a tqdm progress bar and/or HF Hub warnings — that's fine, stderr is safe.)

Then restore the cache so later tasks don't re-download unnecessarily:
```bash
rm -rf ~/.cache/chroma/onnx_models
mv ~/.cache/chroma/onnx_models.bak ~/.cache/chroma/onnx_models
```

- [ ] **Step 7: Commit**

```bash
cd /Users/ylanki/vaulter_ai
git add ingestion/embedder.py
git commit -m "$(cat <<'EOF'
Swap semantic search engine to ChromaDB's built-in ONNX model

Replaces SentenceTransformerEmbeddingFunction (pulls in ~535MB of
torch as a transitive dependency) with ChromaDB's own
ONNXMiniLM_L6_V2 -- the same all-MiniLM-L6-v2 model, same 384-dim
vectors, no torch required. onnxruntime (what this runs on) is
already a mandatory chromadb dependency, so nothing new is added.

The embedding-model name tag changes from "all-MiniLM-L6-v2" to
"onnx-all-MiniLM-L6-v2" deliberately -- the two runtimes aren't
guaranteed to produce numerically identical vectors, so this forces
the existing check_embedding_freshness()/reindex_all() machinery to
correctly flag and re-embed old chunks rather than silently mixing
old and new vectors in the same collection.

Verified: 384-dim output, a real semantic-similarity sanity check
(synonym pair scores ~0.40 vs ~-0.05 for an unrelated pair), and that
loading the model produces no stray stdout output (which would
corrupt the MCP stdio connection to Claude Desktop) even on a cold,
first-time model download.
EOF
)"
```

---

### Task 2: Verify the hash-fallback path still works when the ONNX model can't load

**Files:**
- Test only — no source changes in this task.

**Interfaces:**
- Consumes: `get_embedding_function()`, `get_embedding_model_name()`, `LocalHashEmbedding` from
  Task 1 (unchanged in this task).
- Produces: nothing new — confirms an existing safety net still works after Task 1's change.

- [ ] **Step 1: Write and run the fallback-path verification script**

This simulates "the ONNX model can't be loaded" (e.g. no internet on first run) by making the
import inside `get_embedding_function()`'s try block fail, and confirms the code correctly falls
back to `LocalHashEmbedding` with the `"hash-fallback-v1"` tag — exactly like it already does
today for the sentence-transformers case, just with the new import target.

```bash
cd /Users/ylanki/vaulter_ai
./venv/bin/python3 -c "
import sys
from unittest import mock
sys.path.insert(0, '.')

import ingestion.embedder as embedder_module

# Force the singleton to re-initialize (it may already be cached from a
# prior import in this process) and simulate the ONNX import failing.
embedder_module._EMBEDDING_FUNCTION = None
embedder_module._EMBEDDING_MODEL_NAME = None

with mock.patch('chromadb.utils.embedding_functions.ONNXMiniLM_L6_V2', side_effect=RuntimeError('simulated: no internet, model download failed')):
    ef = embedder_module.get_embedding_function()
    name = embedder_module.get_embedding_model_name()

print('embedding function type:', type(ef).__name__)
print('model name:', name)
assert type(ef).__name__ == 'LocalHashEmbedding', f'expected LocalHashEmbedding, got {type(ef).__name__}'
assert name == 'hash-fallback-v1', f'expected hash-fallback-v1, got {name!r}'

# Confirm the fallback still actually produces usable (384-dim) vectors,
# not just that it loaded.
vec = ef(['test sentence'])
assert len(vec[0]) == 384
print('PASS: fallback to LocalHashEmbedding works correctly when the ONNX model cannot load')
"
```

Expected output:
```
embedding function type: LocalHashEmbedding
model name: hash-fallback-v1
PASS: fallback to LocalHashEmbedding works correctly when the ONNX model cannot load
```

- [ ] **Step 2: Commit**

No source files changed in this task — nothing to commit. (If your workflow requires a commit
per task regardless, skip; this task is verification-only.)

---

### Task 3: Remove sentence-transformers from requirements.txt and prove a fresh install no longer pulls torch

**Files:**
- Modify: `requirements.txt:14` (remove the `sentence-transformers` line)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing new — this is a dependency-list change, verified by an isolated install.

- [ ] **Step 1: Remove the line**

In `requirements.txt`, delete line 14:
```
sentence-transformers   # Real semantic embeddings (all-MiniLM-L6-v2) for RAG search quality
```

(Leave the surrounding lines — `chromadb>=0.5.20`, `numpy`, etc. — untouched.)

- [ ] **Step 2: Prove a genuinely fresh install no longer pulls torch**

This is the single most convincing piece of evidence for this whole change, so it's worth doing
as a real isolated install, not a grep-only check. Budget a few minutes for this — it downloads
real packages into a throwaway virtual environment.

```bash
cd /Users/ylanki/vaulter_ai
python3 -m venv /tmp/vaulter_footprint_check
/tmp/vaulter_footprint_check/bin/pip install --quiet -r requirements.txt
echo "--- checking for torch ---"
/tmp/vaulter_footprint_check/bin/pip show torch 2>&1 || echo "CONFIRMED: torch is NOT installed"
echo "--- checking for sentence-transformers ---"
/tmp/vaulter_footprint_check/bin/pip show sentence-transformers 2>&1 || echo "CONFIRMED: sentence-transformers is NOT installed"
echo "--- checking onnxruntime IS still installed (needed by chromadb) ---"
/tmp/vaulter_footprint_check/bin/pip show onnxruntime | head -2
echo "--- venv size ---"
du -sh /tmp/vaulter_footprint_check
```

Expected output: the `pip show torch` and `pip show sentence-transformers` commands each print
`WARNING: Package(s) not found: ...` and exit non-zero, so the `||` branch runs and prints the
`CONFIRMED` lines. `pip show onnxruntime` succeeds and prints a real version. The final `du -sh`
size should be noticeably smaller than this project's current real `venv/` (which measured
**1.5G** with torch present, per the design spec) — expect roughly 1 GB or less.

- [ ] **Step 3: Confirm the fresh install can actually import and use the new embedding path**

Don't just check package presence — confirm the fresh install's `ingestion/embedder.py` actually
works end to end:

```bash
cd /Users/ylanki/vaulter_ai
/tmp/vaulter_footprint_check/bin/python3 -c "
import sys
sys.path.insert(0, '.')
from ingestion.embedder import get_embedding_model_name, embed_texts
name = get_embedding_model_name()
assert name == 'onnx-all-MiniLM-L6-v2', name
vecs = embed_texts(['a quick sanity check'])
assert len(vecs[0]) == 384
print('PASS: fresh install (no torch) successfully loads and uses the ONNX embedding function')
"
```

Expected: `PASS: fresh install (no torch) successfully loads and uses the ONNX embedding
function`

- [ ] **Step 4: Clean up the throwaway venv**

```bash
rm -rf /tmp/vaulter_footprint_check
```

- [ ] **Step 5: Commit**

```bash
cd /Users/ylanki/vaulter_ai
git add requirements.txt
git commit -m "$(cat <<'EOF'
Remove sentence-transformers (and its torch dependency) from requirements.txt

No longer needed now that ingestion/embedder.py uses ChromaDB's own
built-in ONNXMiniLM_L6_V2 embedding function instead. Verified with a
genuinely fresh, isolated venv install: torch and sentence-transformers
are absent, onnxruntime (needed by chromadb itself) is present, and the
fresh install's embedder.py correctly loads and uses the ONNX model.
EOF
)"
```

---

### Task 4: Confirm `check_embedding_freshness()` correctly detects the old model tag as stale

**Files:**
- Test only — no source changes. Uses an isolated scratch ChromaDB, never the real project
  database.

**Interfaces:**
- Consumes: `get_collection()`, `check_embedding_freshness()` from `ingestion/embedder.py`
  (unchanged by this task — this task validates that Task 1's model-name change flows correctly
  through EXISTING, already-built logic).
- Produces: nothing new.

**Why an isolated scratch copy, not the real project:** `config.CHROMA_DIR` is computed from
`Path(__file__).parent` inside `config.py` — there's no environment-variable override for it.
Rather than add one (out of scope for this plan), this task copies the small, self-contained set
of files `ingestion/embedder.py` actually needs (`config.py`, `ingestion/__init__.py`,
`ingestion/embedder.py`) into a temp directory. Because `config.py`'s own `BASE_DIR` is based on
its own `__file__`, running from that temp copy naturally gives an isolated `CHROMA_DIR` under
the temp directory — no code changes, no new override mechanism, no risk to the real database.

- [ ] **Step 1: Build the isolated scratch copy**

```bash
mkdir -p /tmp/vaulter_freshness_check/ingestion
cp /Users/ylanki/vaulter_ai/config.py /tmp/vaulter_freshness_check/config.py
cp /Users/ylanki/vaulter_ai/ingestion/__init__.py /tmp/vaulter_freshness_check/ingestion/__init__.py
cp /Users/ylanki/vaulter_ai/ingestion/embedder.py /tmp/vaulter_freshness_check/ingestion/embedder.py
```

- [ ] **Step 2: Seed two chunks with different `embedding_model` tags and verify freshness detection**

```bash
cd /tmp/vaulter_freshness_check
/Users/ylanki/vaulter_ai/venv/bin/python3 -c "
import sys
sys.path.insert(0, '.')
from ingestion.embedder import get_collection, check_embedding_freshness, get_embedding_model_name

collection = get_collection()
current_model = get_embedding_model_name()
print('current model:', current_model)
assert current_model == 'onnx-all-MiniLM-L6-v2'

# Seed one chunk tagged with the OLD (pre-swap) model name, and one
# tagged with the CURRENT model name, using the real embedding function
# to produce valid vectors (only the metadata tag differs).
vecs = collection._embedding_function(['old chunk content', 'new chunk content'])
collection.upsert(
    ids=['old_chunk_1', 'new_chunk_1'],
    documents=['old chunk content', 'new chunk content'],
    embeddings=vecs,
    metadatas=[
        {'embedding_model': 'all-MiniLM-L6-v2'},   # stale -- old tag
        {'embedding_model': current_model},          # fresh -- current tag
    ],
)

result = check_embedding_freshness(sample_limit=10)
print('freshness result:', result)
assert result['needs_reindex'] is True, 'expected needs_reindex=True with one stale chunk present'
assert result['stale_chunks_sampled'] == 1, f\"expected exactly 1 stale chunk, got {result['stale_chunks_sampled']}\"
assert result['chunks_sampled'] == 2

print('PASS: check_embedding_freshness() correctly detects the old model tag as stale')
"
```

Expected output ends with:
```
PASS: check_embedding_freshness() correctly detects the old model tag as stale
```

- [ ] **Step 3: Verify freshness reports healthy once everything matches the current tag**

```bash
cd /tmp/vaulter_freshness_check
/Users/ylanki/vaulter_ai/venv/bin/python3 -c "
import sys
sys.path.insert(0, '.')
from ingestion.embedder import get_collection, check_embedding_freshness, get_embedding_model_name

collection = get_collection()
current_model = get_embedding_model_name()

# Overwrite the previously-stale chunk so BOTH chunks now carry the
# current tag.
vecs = collection._embedding_function(['old chunk content'])
collection.upsert(
    ids=['old_chunk_1'],
    documents=['old chunk content'],
    embeddings=vecs,
    metadatas=[{'embedding_model': current_model}],
)

result = check_embedding_freshness(sample_limit=10)
print('freshness result:', result)
assert result['needs_reindex'] is False, 'expected needs_reindex=False once nothing is stale'
assert result['stale_chunks_sampled'] == 0

print('PASS: check_embedding_freshness() correctly reports healthy once nothing is stale')
"
```

Expected output ends with:
```
PASS: check_embedding_freshness() correctly reports healthy once nothing is stale
```

- [ ] **Step 4: Clean up the scratch copy**

```bash
rm -rf /tmp/vaulter_freshness_check
```

- [ ] **Step 5: Commit**

No source files changed in this task — nothing to commit. This task is verification-only,
confirming Task 1's model-name change correctly triggers already-existing detection logic.

---

### Task 5: Automatically reindex when the applied update changes the embedding model

**Files:**
- Modify: `apply_update.py` (add a new `_reindex_if_needed()` function; wire it into
  `apply_pending_update()`)

**Interfaces:**
- Consumes: `PROJECT_ROOT`, `PENDING_UPDATE_DIR` pattern already established in `apply_update.py`.
- Produces: `_reindex_if_needed(project_root: Path) -> dict`, returning one of:
  - `{"reindexed": False}` — nothing needed it.
  - `{"reindexed": True, "stale_chunks_before": N}` — it ran successfully.
  - `{"reindexed": False, "error": "..."}` — the check or the reindex itself failed; never
    raises.

  `apply_pending_update()`'s returned dict (currently `{"applied", "version", "files_updated",
  "files_deleted", "dependencies_ok", "dependencies_message"}`) gains one new key: `"reindex"`,
  holding whatever `_reindex_if_needed()` returned.

**Why a fresh subprocess, not a direct import:** `apply_pending_update()` can run inside the
long-running MCP server process (via the `apply_pending_update` MCP tool), which may have already
imported the OLD `ingestion.embedder` module before this update's files were just synced onto
disk. Python does not re-read an already-imported module from disk, so a same-process check could
report stale, pre-update information. A fresh subprocess (verified below to correctly resolve
`ingestion.embedder`'s imports via `cwd`) always sees exactly what the just-updated code on disk
says right now.

- [ ] **Step 1: Write and run a script proving the subprocess-based check works against this real project (documents current, unmodified behavior — this step passes before any code change, since it's testing infrastructure this task will reuse, not the new function itself)**

```bash
cd /Users/ylanki/vaulter_ai
./venv/bin/python3 -c "
import subprocess, sys, json
result = subprocess.run(
    [sys.executable, '-c', 'from ingestion.embedder import check_embedding_freshness as f; import json; print(json.dumps(f()))'],
    cwd='/Users/ylanki/vaulter_ai', capture_output=True, text=True, timeout=60,
)
print('returncode:', result.returncode)
assert result.returncode == 0
freshness = json.loads(result.stdout.strip().splitlines()[-1])
print('freshness:', freshness)
assert 'needs_reindex' in freshness
print('PASS: a fresh subprocess with cwd set can correctly import and call ingestion.embedder')
"
```

Expected: `PASS: a fresh subprocess with cwd set can correctly import and call
ingestion.embedder` (the actual `needs_reindex` value depends on this machine's current real
database state — either is fine, this step only proves the mechanism itself works).

- [ ] **Step 2: Add `_reindex_if_needed()` to `apply_update.py`**

In `apply_update.py`, add this new function immediately after `refresh_dependencies()` (i.e.
after line 145, before `def apply_pending_update(...)`):

```python
def _reindex_if_needed(project_root: Path) -> dict:
    """
    Checks whether the just-applied update changed which embedding model
    is active, and if so, re-embeds every existing chunk so search isn't
    silently degraded for anyone until they happen to notice and run this
    by hand -- see check_embedding_freshness's own docstring in
    ingestion/embedder.py for why ChromaDB never does this automatically
    on its own.

    Runs the check in a FRESH subprocess rather than importing
    ingestion.embedder directly in THIS process -- this process (e.g. the
    long-running MCP server calling apply_pending_update()) may have
    already imported the OLD version of that module before the files
    were just synced above, and Python does not re-read an already-
    imported module from disk. A fresh subprocess is the only reliable
    way to see what the JUST-UPDATED code on disk actually considers
    current.

    Returns {"reindexed": False} if nothing needed it, or
    {"reindexed": True, "stale_chunks_before": N} if it ran. Never
    raises -- returns {"reindexed": False, "error": "..."} on any
    failure, so a problem here can't take down the rest of
    apply_pending_update()'s already-successful file sync.
    """
    check = subprocess.run(
        [sys.executable, "-c",
         "from ingestion.embedder import check_embedding_freshness as f; "
         "import json; print(json.dumps(f()))"],
        cwd=str(project_root), capture_output=True, text=True, timeout=120,
    )
    if check.returncode != 0:
        return {"reindexed": False, "error": (check.stderr or check.stdout).strip()[-500:]}

    try:
        freshness = json.loads(check.stdout.strip().splitlines()[-1])
    except Exception as e:
        return {"reindexed": False, "error": f"could not parse freshness check output: {e}"}

    if not freshness.get("needs_reindex"):
        return {"reindexed": False}

    reindex = subprocess.run(
        [sys.executable, "main.py", "reindex"],
        cwd=str(project_root), capture_output=True, text=True, timeout=1800,
    )
    if reindex.returncode != 0:
        return {"reindexed": False, "error": (reindex.stderr or reindex.stdout).strip()[-500:]}

    return {"reindexed": True, "stale_chunks_before": freshness.get("stale_chunks_sampled", 0)}
```

- [ ] **Step 3: Wire it into `apply_pending_update()`**

In `apply_update.py`, in `apply_pending_update()`, replace:

```python
    updated, deleted = apply_update(project_root, zip_path)
    deps_ok, deps_message = refresh_dependencies(project_root)

    zip_path.unlink(missing_ok=True)
    (PENDING_UPDATE_DIR / "ready.json").unlink(missing_ok=True)

    return {
        "applied": True,
        "version": version,
        "files_updated": updated,
        "files_deleted": deleted,
        "dependencies_ok": deps_ok,
        "dependencies_message": deps_message,
    }
```

with:

```python
    updated, deleted = apply_update(project_root, zip_path)
    deps_ok, deps_message = refresh_dependencies(project_root)
    reindex_result = _reindex_if_needed(project_root)

    zip_path.unlink(missing_ok=True)
    (PENDING_UPDATE_DIR / "ready.json").unlink(missing_ok=True)

    return {
        "applied": True,
        "version": version,
        "files_updated": updated,
        "files_deleted": deleted,
        "dependencies_ok": deps_ok,
        "dependencies_message": deps_message,
        "reindex": reindex_result,
    }
```

- [ ] **Step 4: Build an isolated scratch project to test `_reindex_if_needed()` without touching the real database**

Same rationale as Task 4 — copy the minimal working file set into a temp dir so
`config.CHROMA_DIR` naturally isolates itself there.

```bash
mkdir -p /tmp/vaulter_reindex_check/ingestion
cp /Users/ylanki/vaulter_ai/config.py /tmp/vaulter_reindex_check/config.py
cp /Users/ylanki/vaulter_ai/main.py /tmp/vaulter_reindex_check/main.py
cp /Users/ylanki/vaulter_ai/ingestion/__init__.py /tmp/vaulter_reindex_check/ingestion/__init__.py
cp /Users/ylanki/vaulter_ai/ingestion/embedder.py /tmp/vaulter_reindex_check/ingestion/embedder.py
```

- [ ] **Step 5: Seed the scratch project with a stale chunk, then confirm `_reindex_if_needed()` reindexes it**

```bash
cd /Users/ylanki/vaulter_ai
./venv/bin/python3 -c "
import sys
sys.path.insert(0, '/tmp/vaulter_reindex_check')
from ingestion.embedder import get_collection, get_embedding_model_name

collection = get_collection()
current_model = get_embedding_model_name()
vecs = collection._embedding_function(['a chunk that needs reindexing'])
collection.upsert(
    ids=['stale_chunk_1'], documents=['a chunk that needs reindexing'],
    embeddings=vecs, metadatas=[{'embedding_model': 'all-MiniLM-L6-v2'}],  # old tag -- stale
)
print('seeded 1 stale chunk')
"

./venv/bin/python3 -c "
import sys
sys.path.insert(0, '.')
from apply_update import _reindex_if_needed
from pathlib import Path

result = _reindex_if_needed(Path('/tmp/vaulter_reindex_check'))
print('reindex result:', result)
assert result.get('reindexed') is True, f'expected reindexed=True, got {result}'
assert result.get('stale_chunks_before', 0) >= 1
print('PASS: _reindex_if_needed() correctly detected and reindexed a stale chunk')
"

./venv/bin/python3 -c "
import sys
sys.path.insert(0, '/tmp/vaulter_reindex_check')
from ingestion.embedder import check_embedding_freshness

result = check_embedding_freshness(sample_limit=10)
print('freshness after reindex:', result)
assert result['needs_reindex'] is False, 'expected everything fresh after reindexing'
print('PASS: the previously-stale chunk is now fresh after _reindex_if_needed() ran')
"
```

Expected: both `PASS:` lines print, confirming the seeded stale chunk was detected and reindexed,
and that the collection reports healthy afterward.

- [ ] **Step 6: Confirm `_reindex_if_needed()` is a no-op when nothing is stale**

```bash
cd /Users/ylanki/vaulter_ai
./venv/bin/python3 -c "
import sys
sys.path.insert(0, '.')
from apply_update import _reindex_if_needed
from pathlib import Path

# Run again immediately -- the scratch project is now fully fresh from
# Step 5, so this must be a no-op, not a wasted re-embed of everything.
result = _reindex_if_needed(Path('/tmp/vaulter_reindex_check'))
print('reindex result (should be a no-op):', result)
assert result == {'reindexed': False}, f'expected a clean no-op, got {result}'
print('PASS: _reindex_if_needed() correctly does nothing when everything is already fresh')
"
```

Expected: `PASS: _reindex_if_needed() correctly does nothing when everything is already fresh`

- [ ] **Step 7: Clean up the scratch project**

```bash
rm -rf /tmp/vaulter_reindex_check
```

- [ ] **Step 8: Commit**

```bash
cd /Users/ylanki/vaulter_ai
git add apply_update.py
git commit -m "$(cat <<'EOF'
Auto-reindex after apply_pending_update() if the embedding model changed

Adds _reindex_if_needed(), run as the last step inside
apply_pending_update() (right after syncing files and refreshing
dependencies). Checks freshness in a fresh subprocess -- not a direct
import in this process, which may have already loaded the OLD
ingestion.embedder before this update's files were synced -- and runs
`python main.py reindex` only if the model actually changed.

This is what makes the embedding-engine footprint-shrink (see
ingestion/embedder.py's recent change) fully non-technical: nobody
needs to notice needs_reindex or run a command by hand, since applying
an update already happens via one conversational "yes, go ahead" in
Claude Desktop.

Verified against an isolated scratch project (never the real
database): a seeded stale chunk gets correctly detected and
reindexed, and a second run against an already-fresh collection is
correctly a clean no-op.
EOF
)"
```

---

### Task 6: Surface the reindex outcome in the `apply_pending_update` MCP tool

**Files:**
- Modify: `mcp_server.py:677-687` (the `apply_pending_update` tool's success-message formatting)

**Interfaces:**
- Consumes: the new `"reindex"` key in `apply_update.apply_pending_update()`'s return dict (from
  Task 5).
- Produces: nothing new — just adds a line to the tool's existing plain-English response.

- [ ] **Step 1: Edit the tool's success-message formatting**

In `mcp_server.py`, inside the `apply_pending_update` tool function, replace:

```python
            lines = [
                f"Applied version {result['version']}: {result['files_updated']} file(s) "
                f"updated, {result['files_deleted']} removed.",
            ]
            if not result["dependencies_ok"]:
                lines.append(f"Note: refreshing Python dependencies hit a problem: "
                              f"{result['dependencies_message']}")
            lines.append("")
            lines.append("Tell the user to fully quit and reopen Claude Desktop now — the new "
                          "code only takes effect on the next launch.")
            return "\n".join(lines)
```

with:

```python
            lines = [
                f"Applied version {result['version']}: {result['files_updated']} file(s) "
                f"updated, {result['files_deleted']} removed.",
            ]
            if not result["dependencies_ok"]:
                lines.append(f"Note: refreshing Python dependencies hit a problem: "
                              f"{result['dependencies_message']}")
            reindex = result.get("reindex", {})
            if reindex.get("reindexed"):
                lines.append("The search index was refreshed to match this update — this "
                              "happens automatically, no action needed.")
            elif reindex.get("error"):
                lines.append(f"Note: an automatic search-index refresh hit a problem: "
                              f"{reindex['error']}")
            lines.append("")
            lines.append("Tell the user to fully quit and reopen Claude Desktop now — the new "
                          "code only takes effect on the next launch.")
            return "\n".join(lines)
```

- [ ] **Step 2: Verify the MCP tool's full response text with a simulated "reindexed" result**

```bash
cd /Users/ylanki/vaulter_ai
./venv/bin/python3 -c "
import sys, asyncio
sys.path.insert(0, '.')
from unittest import mock
import mcp_server

fake_result = {
    'applied': True, 'version': 'abc1234', 'files_updated': 3, 'files_deleted': 0,
    'dependencies_ok': True, 'dependencies_message': '',
    'reindex': {'reindexed': True, 'stale_chunks_before': 42},
}

srv = mcp_server.create_mcp_server()

async def main():
    import apply_update
    with mock.patch.object(apply_update, 'apply_pending_update', return_value=fake_result):
        _, result = await srv.call_tool('apply_pending_update', {})
    return result['result']

output = asyncio.run(main())
print(output)
assert 'search index was refreshed' in output
print()
print('PASS: apply_pending_update tool correctly reports a completed reindex')
" 2>&1 | grep -v "^\[.*INFO\|HTTP Request\|WARNING.*HF_TOKEN"
```

Expected: the printed output includes the line "The search index was refreshed to match this
update — this happens automatically, no action needed." and the final `PASS:` line prints.

- [ ] **Step 3: Verify the MCP tool's response text when nothing needed reindexing**

```bash
cd /Users/ylanki/vaulter_ai
./venv/bin/python3 -c "
import sys, asyncio
sys.path.insert(0, '.')
from unittest import mock
import mcp_server

fake_result = {
    'applied': True, 'version': 'abc1234', 'files_updated': 3, 'files_deleted': 0,
    'dependencies_ok': True, 'dependencies_message': '',
    'reindex': {'reindexed': False},
}

srv = mcp_server.create_mcp_server()

async def main():
    import apply_update
    with mock.patch.object(apply_update, 'apply_pending_update', return_value=fake_result):
        _, result = await srv.call_tool('apply_pending_update', {})
    return result['result']

output = asyncio.run(main())
print(output)
assert 'search index was refreshed' not in output, 'should not mention reindexing when nothing happened'
assert 'restart' not in output.lower() or 'quit and reopen' in output.lower()
print()
print('PASS: apply_pending_update tool stays quiet about reindexing when nothing needed it')
" 2>&1 | grep -v "^\[.*INFO\|HTTP Request\|WARNING.*HF_TOKEN"
```

Expected: the output does NOT mention "search index was refreshed", and the final `PASS:` line
prints.

- [ ] **Step 4: Commit**

```bash
cd /Users/ylanki/vaulter_ai
git add mcp_server.py
git commit -m "$(cat <<'EOF'
Surface reindex outcome in apply_pending_update's chat response

Small addition to the MCP tool's existing plain-English success
message: mentions when an automatic search-index refresh ran (or hit
a problem) as part of applying an update, and stays silent about it
when nothing needed reindexing -- matching this project's existing
convention of only speaking up when there's something worth saying.

Verified both branches (reindexed vs not) via the real MCP tool call
path with a mocked apply_update result.
EOF
)"
```

---

### Task 7: Run the real one-time migration on this machine and do a final end-to-end smoke test

**Files:**
- No source changes — this is the actual production migration for the currently-live database,
  plus a full-system smoke test.

**Interfaces:**
- Consumes: everything built in Tasks 1-6, run for real against the real project.
- Produces: nothing new — confirms the whole change works end to end on a real, previously-live
  database, not just scratch copies.

- [ ] **Step 1: Check this machine's real database freshness status right now**

```bash
cd /Users/ylanki/vaulter_ai
./venv/bin/python3 -c "
import sys
sys.path.insert(0, '.')
from ingestion.embedder import check_embedding_freshness
print(check_embedding_freshness())
"
```

Expected: `needs_reindex: True` (this machine's real database has existing chunks tagged with the
old `all-MiniLM-L6-v2` name from before this plan's changes).

- [ ] **Step 2: Run the real reindex**

```bash
cd /Users/ylanki/vaulter_ai
./venv/bin/python3 main.py reindex
```

Expected: output similar to `Reindexing N chunks with the current embedding function...` followed
by `Done — reindexed N/N chunks.` This will take some real time proportional to how many chunks
exist (embedding is local/CPU-only, no external API calls or costs).

- [ ] **Step 3: Confirm the real database now reports fresh**

```bash
cd /Users/ylanki/vaulter_ai
./venv/bin/python3 -c "
import sys
sys.path.insert(0, '.')
from ingestion.embedder import check_embedding_freshness
result = check_embedding_freshness()
print(result)
assert result['needs_reindex'] is False, f'expected fresh after reindexing, got {result}'
print('PASS: the real database is fully migrated to the new embedding model')
"
```

Expected: `PASS: the real database is fully migrated to the new embedding model`

- [ ] **Step 4: Run a real search smoke test through the actual `search_database` MCP tool — confirm results still make sense, not just that vectors exist**

This uses the query `"commercial real estate market services"`, already confirmed (before any
change in this plan) to return real, sensible-looking results against this machine's actual
ingested content (a mix of scraped CBRE market-research text and property documents) — so a
before/after comparison is meaningful, not just "did it crash."

```bash
cd /Users/ylanki/vaulter_ai
./venv/bin/python3 -c "
import sys, asyncio
sys.path.insert(0, '.')
import mcp_server

srv = mcp_server.create_mcp_server()

async def main():
    _, result = await srv.call_tool('search_database', {'query': 'commercial real estate market services', 'n_results': 5})
    return result['result']

output = asyncio.run(main())
print(output[:1000])
assert output and 'No relevant data found' not in output, 'expected real search results, got none'
print()
print('PASS: search_database MCP tool returns sensible results after the embedding swap')
"
```

Expected: real text content is printed (not "No relevant data found for this query."), and the
final `PASS:` line prints — confirming search still works sensibly end to end through the actual
MCP tool path after the swap, not just that embedding functions run without crashing.

- [ ] **Step 5: Confirm `get_database_stats` no longer shows the stale-embedding-model warning**

Note: this specific warning (`"Note: N of M sampled chunks were embedded with a different search
model..."`) lives in the `get_database_stats` MCP tool (`mcp_server.py:879-885`), not in
`check_system_health` — `check_system_health` reports chunk counts but does not itself surface
`needs_reindex`. Testing the correct tool matters here since the two report different things.

```bash
cd /Users/ylanki/vaulter_ai
./venv/bin/python3 -c "
import sys, asyncio
sys.path.insert(0, '.')
import mcp_server

srv = mcp_server.create_mcp_server()

async def main():
    _, result = await srv.call_tool('get_database_stats', {})
    return result['result']

output = asyncio.run(main())
print(output)
assert 'embedded with a different search model' not in output, \
    'expected no stale-embedding-model warning after Step 2/3\'s real reindex'
print()
print('PASS: get_database_stats no longer warns about a stale embedding model')
"
```

Expected: the printed stats show real chunk/PDF/web/email counts with NO trailing "Note: ...
embedded with a different search model..." warning, and the final `PASS:` line prints.

- [ ] **Step 6: Final full-repo compile check**

```bash
cd /Users/ylanki/vaulter_ai
./venv/bin/python3 -m py_compile config.py mcp_server.py apply_update.py release.py \
  ingestion/embedder.py analysis/screening/pipeline.py safe_io.py main.py && echo "ALL COMPILE OK"
```

Expected: `ALL COMPILE OK`

- [ ] **Step 7: Confirm no stray scratch/temp artifacts were left behind in the real repo**

```bash
cd /Users/ylanki/vaulter_ai
git status --short
find . -maxdepth 2 -iname "__pycache__" -not -path "./venv/*" 2>/dev/null
```

Expected: `git status --short` shows only the commits already made in Tasks 1, 3, 5, and 6 (i.e.
a clean working tree if all prior commits succeeded), and no stray `__pycache__` directories
outside `venv/`. If any `__pycache__` is found, remove it: `rm -rf <path>`.

- [ ] **Step 8: No commit needed**

This task doesn't change any tracked files (the real database isn't tracked by git) — it's the
production migration plus final verification. If Steps 1-7 all pass, this plan is complete.
