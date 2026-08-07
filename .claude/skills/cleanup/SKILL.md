---
name: cleanup
description: Use when asked to clean up the repo — remove dead files, folders, docs, config constants, imports, or stale doc sections left behind by earlier work. Also for "what's dead in here?", "tidy this up", or after a large refactor.
---

# Cleanup: remove what the system no longer uses

Find things nothing uses any more, prove they're dead, propose them, then delete them
recoverably. The hard part is **not** finding candidates — it's not deleting something that
only *looks* unused. This skill is mostly about that.

Default posture: **propose, then delete on a go-ahead.** Deletion isn't an edit; a blanket
"you can edit files freely" does not authorize it. The one exception is build artifacts
(`__pycache__`, `*.pyc`), which are regenerated and can just go.

---

## Never touch — hard stops

Stop and say so if a cleanup would reach any of these. Do not "verify" them first; do not
list their contents as candidates.

| Path | Why |
|---|---|
| **`config.CORPUS_DIR`** (the firm's synced document library) | **The firm's actual documents.** Hundreds of thousands of real business records — deeds, title policies, closing memos. A deletion here is unrecoverable business data loss, and it syncs to everyone. This system is read-only there, always. |
| **`config.SHARED_DIR`** (`Vaulter AI Shared`) | Team-shared screening output. Deleting propagates to every colleague via OneDrive. |
| Anything else under `OneDrive - *` | Same reasoning. Cleanup operates on the **repo**, never on synced folders. |
| `system/confidentials/` | Live credentials. `.env.template` may be *edited*, never deleted. |
| `.git/` | Obvious. |
| `system/data/project_master/` | The live portfolio source, and `property_coordinates.csv` (hand-verified from deeds — see [[portfolio-coordinates-system]]). |
| `.venv/` | Not cruft, just big. Out of scope. |

If you catch yourself building a path by joining onto `CORPUS_DIR` or `SHARED_DIR` during a
cleanup, you have already gone wrong.

---

## Evidence standard

Something is dead when you can show **nobody reaches it**, not when grep came back quiet.

Before proposing removal, state which of these you checked:

1. **Static references** — `Grep` for the symbol/filename across `*.py`, `*.md`, `*.json`,
   `*.bat`, `*.command`. Use the ripgrep-backed Grep tool, not `grep -r .` — the latter crawls
   `.venv/` and `system/data/` and will hang.
2. **Lazy imports** — `system/mcp_server.py` imports almost everything *inside functions*
   (`from portfolio import load_properties` mid-body). A scan of top-of-file imports proves
   nothing here.
3. **String dispatch** — `system/main.py` routes on `args[0] == "index-corpus"`. The function is never
   referenced by name anywhere.
4. **Decorator registration** — every `@mcp.tool()` function is called by the MCP framework, never
   by this codebase. **A "no callers" check flags all 20 of them. None are dead.**
5. **Deliberate no-ops** — some things are retained on purpose with a comment saying so. Read the
   comment before believing the code.

If you cannot satisfy 1–5, the finding is *"possibly unused, needs a human"* — report it that
way. Never delete on suspicion.

---

## Known false positives in this repo

These have all bitten, or would. Do not propose them without a fresh reason:

- **`geo_providers.py`** — looks superseded by `geo_federal.py`, and is not imported by anything
  in `system/analysis/`. It is live: `system/pipeline/proximity_tool.py` uses it for POI category search, which
  has no federal equivalent. A reachability script that only follows `from x import y` where `y`
  is a *name* will miss `from analysis.screening import geo_providers` and report it dead.
- **`LEGACY_WATCH_DIR` / `LEGACY_PROCESSED_DIR`** (`system/config.py`) and `system/data/watched_folder/`,
  `system/data/processed/` — the pipelines that wrote them are gone, but `_resolve_costar_source` still
  *reads* them so an export already sitting on someone's machine doesn't vanish after an update.
  Deliberate fallback, not residue.
- **Empty `__init__.py`** — makes the package importable. Empty is correct.
- **`.gitkeep`** — exists precisely so an empty tracked directory survives.
- **`geo_providers.py` retry logic** — added in response to measured intermittent failures, not
  as a precaution. Looks like defensive bloat; isn't.
- **`docs/MULTI_USER_TRANSITION.md`** — superseded, deliberately kept as the record of *why* the
  old design had its problems. Superseded ≠ dead.
- **`quick_start/*.bat` / `*.command`** — referenced only from README prose and double-clicked by
  non-technical staff. Nothing imports them.

---

## Procedure

### 1. Inventory

Run these; they're the cheap high-yield ones:

```bash
# Build artifacts — safe to delete outright, no proposal needed
find . -name __pycache__ -type d -not -path "./.venv/*"
find . -name "*.pyc" -not -path "./.venv/*" | head

# Directories emptied by an earlier `git rm` but still on disk
for d in */; do [ -z "$(ls -A "$d" 2>/dev/null | grep -v __pycache__)" ] && echo "empty: $d"; done

# Untracked data left by removed subsystems, with sizes
for d in system/data/*/; do echo "$(du -sh "$d" 2>/dev/null | cut -f1)  $(find "$d" -type f | wc -l) files  $d"; done

# Stray IDE/editor folders nested where they don't belong
find . -name ".idea" -o -name ".DS_Store" -not -path "./.venv/*"
```

Then, for code and docs:
- Declared dependencies in `system/requirements.txt` that nothing imports (check the real import name —
  `python-dotenv` imports as `dotenv`, `Pillow` as `PIL`).
- Constants in `system/config.py` no module reads.
- Imports orphaned by your own edits.
- Doc sections describing deleted subsystems.

### 2. Classify

Three buckets, and keep them separate:

| Bucket | Action |
|---|---|
| **Build artifacts** | Delete now, mention in passing |
| **Proven dead** (evidence standard met) | Propose for deletion |
| **Possibly unused** | Report only. Never delete. |

### 3. Propose

One table. Path, what it is, size or line count, and the evidence that killed it. Total the
reclaimed space. Then ask for a go-ahead.

Call out anything load-bearing-looking even when you're confident — the user should be able to
veto individual rows, not just the whole batch.

### 4. Delete recoverably

- Tracked files: **`git rm`** — recoverable from history.
- Untracked files/dirs: plain delete, but say clearly in the proposal that these are **not**
  in git and so are gone for good. Untracked data (logs, caches, a 98 MB vector DB) is the one
  place a mistake actually costs something.
- Never `git clean -fdx` — it would take `system/confidentials/` and `system/data/project_master/` with it.

### 5. Remove the orphans your deletion created

Deleting a module usually strands things: its imports elsewhere, its config constants, its
`system/requirements.txt` entries, its mentions in `CLAUDE.md` / `README.md` / skills. A cleanup that
leaves a doc describing a deleted module hasn't finished.

Clean up **your own** orphans only. Pre-existing dead code you noticed but didn't create is a
*report*, not a task — per CLAUDE.md's surgical-changes rule.

### 6. Verify

Non-negotiable. Run it and paste the real result:

```bash
# everything still compiles
git ls-files '*.py' | xargs -I{} python -m py_compile {}

# every surviving module still imports, and the server still builds
python -c "
import asyncio, importlib
for m in ['config','portfolio','corpus','main','mcp_server','core.safe_io',
          'pipeline.proximity_tool','pipeline.property_coordinates',
          'analysis.screening.fit_screen','analysis.screening.geo_federal',
          'analysis.screening.geo_providers','analysis.screening.report']:
    importlib.import_module(m)
from mcp_server import create_mcp_server
print(len(asyncio.run(create_mcp_server().list_tools())), 'tools OK')
from portfolio import load_properties; print(len(load_properties()[0]), 'properties OK')
from corpus import search; print(len(search('closing memo')), 'search hits OK')
"
```

Expected: all compile, **20 tools**, **49 properties**, non-zero search hits. A drop in the tool
count means you deleted something live — restore it before reporting.

If the tool count *should* change because the cleanup removed a tool deliberately, say so and
give the new expected number.

---

## Reporting

- What was removed, grouped by bucket, with reclaimed space.
- What you **left alone and why** — this is the more useful half. "Looks dead, is load-bearing"
  is worth more to the reader than the delete list.
- Anything in the *possibly unused* bucket, as an explicit question.
- The verification output, quoted, not summarized as "tests pass."
