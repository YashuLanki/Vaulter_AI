# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Vaulter AI Property Intelligence System — a Python system for a real estate investment
company that ingests PDFs/emails/web data into a vector database and exposes it to each
team member entirely through their own local MCP server connected to their own Claude
Desktop (no separate UI), and runs a 4-phase CoStar listing screening pipeline. Each
staff member runs their own fully-local instance (own ChromaDB, own Outlook auth) so
each person's email stays private to them — see mcp_server.py's header for details.
`main.py` is the single CLI entry point for every stage; `mcp_server.py` is what actually
runs in production (it starts the PDF watcher and scheduler as background threads, then
serves MCP tools on the main thread).

## Commands

```bash
# Setup
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements.txt

# Stage 1 — PDF ingestion
python main.py ingest                    # start the folder watcher
python main.py stats                     # database stats across all stages
python main.py query "flood zone Magic Ranch"

# Stage 2 — web & email pipeline
python main.py scrape ["Source Name"]    # web scrape (all, or one named source)
python main.py email [--days 30]         # pull Outlook emails
python main.py property-scrape ["Name"]  # scrape news/market data per property
python main.py properties                # list portfolio from Project Master
python main.py auth                      # one-time Outlook OAuth
python main.py schedule                  # run the background scheduler standalone

# Stage 3 — MCP server (what production actually runs)
python main.py mcp                       # stdio transport, no port (see mcp_server.py header)

# CoStar screening pipeline — standalone smoke test, no MCP round-trip needed
python test_screening.py                 # edit COSTAR_FILE at top of the file first
```

There is no lint/test framework configured (no pytest, no linter config).
`test_screening.py` is a manual smoke-test script, not a pytest suite — run it directly.

## Architecture

### Cross-cutting: `config.py`
Every path, credential, and tunable constant lives here — this is the only file that
needs to change to port the project to a new machine. It cross-platform-detects Windows
vs Mac, loads `confidentials/.env` via `python-dotenv`, and creates all `data/` subfolders
on import. Nothing else in the codebase should hardcode a path or read `os.environ`
directly for these values.

`SECRETS_DIR` resolves to a **hardcoded** `C:\Users\<USERNAME>\Vaulter AI\confidentials`
on Windows (not `BASE_DIR`-relative) — if the project ever moves off that exact path on
a Windows machine, update this line in `config.py`.

### Data flow: everything lands in ChromaDB, tagged by `type`
All four stages write into one shared ChromaDB collection (`ingestion/embedder.py`,
collection name in `config.CHROMA_COLLECTION_NAME`). Chunks are distinguished purely by
metadata `type`: `pdf` (default), `web_scrape`, `property_intelligence`,
`email` / `email_attachment_<kind>`. `analysis/rag_engine.py` is the only retrieval layer
— it offers four modes (`get_property_context`, cross-property, type-filtered,
`free_search`) and is what the MCP tools call into directly. Never query ChromaDB
directly from a new tool; go through `rag_engine`.

### Stage 1 — PDF ingestion (`ingestion/`)
`watcher.py` monitors `data/watched_folder/<State>/<Property>/file.*` (state and property
are read from the folder path, not fuzzy-matched from the filename, and validated against
the Project Master). `extractor.py` pulls text (OCR fallback via Tesseract/Poppler for
scanned PDFs), `chunker.py` splits it using tiered chunk sizes keyed by page count
(`config.CHUNK_TIERS` — note the `9999`-page sentinel tier exists specifically to keep
CoStar/Excel rows intact instead of hard-splitting pipe-separated data), `embedder.py`
stores it, `registry.py` dedupes via file hash. After processing, files move to
`data/processed/<state>/` (or `processed/sold/<state>/`, `processed/unknown/`).

### Stage 2 — web & email pipeline (`pipeline/`)
`web_scraper.py` scrapes fixed sources from `config.WEB_SOURCES`. `property_scraper.py`
scrapes per-property news/market data for all properties loaded from the Project Master.
`property_matcher.py` matches scraped/email content back to a specific property.
`email_reader.py` pulls Outlook via Microsoft Graph (auth in `outlook_auth.py`, MSAL) and
handles every attachment type (PDF, Word via mammoth, Excel via openpyxl, PowerPoint via
python-pptx, images via OCR). `scheduler.py` (APScheduler) automates all of the above —
when running under the MCP server this scheduler is started in a background thread by
`mcp_server.py`, not via `main.py schedule`.

### Stage 3 — RAG + MCP server
Most MCP tools (`search_database`, `get_property_info`, `get_risk_scan`,
`get_market_intelligence`, `get_email_highlights`, etc.) call `analysis/rag_engine.py`
directly and return raw retrieved context — no Claude API call happens in the code
itself; the requesting Claude Desktop session does the reasoning over that context as
part of its own (already-covered) conversation. The **only** tool that makes its own
direct Claude API call (and therefore needs real Anthropic Console credits, separate
from a Pro/Team chat subscription) is `screen_listings`, via the CoStar screening
pipeline's Phase 3/4 — see `analysis/screening/`.

`mcp_server.py` is the production entry point (`create_mcp_server()` registers all
`@mcp.tool()`-decorated functions; `run_mcp_server()` starts the watcher + scheduler
threads then calls `mcp.run(transport="stdio")`). This is deliberate, not a stopgap:
each staff member runs their own fully-local instance of this project (own ChromaDB,
own Outlook auth, own copy of this server), launched directly by their own Claude
Desktop app via stdio — never over a network. This is a privacy boundary as much as
an architecture choice: a staff member's own email is only ever ingested into their
own local database, never visible to a colleague's Claude session. claude.ai (the
web app) cannot be used with this server for the same reason ngrok would otherwise
be needed — it runs in the cloud and can only reach a network address, never a
process on someone's own machine; Claude Desktop or Claude Code are required.
There is no `MCP_API_KEY` / shared secret — the real access boundary is simply "is
this your own computer, logged in as you." (The README's ngrok/HTTP-connector
section describes a different, no-longer-intended shared-server design; treat it
as stale if you encounter it.) Tools exposed:
`check_system_health`, `apply_pending_update`, `search_database`, `get_property_info`,
`get_portfolio_list`, `get_properties_by_stage`, `check_inbox_now`, `get_email_highlights`,
`get_risk_scan`, `get_market_intelligence`, `get_database_stats`, `open_property_files`,
`open_general_files`, `open_proximity_files`, `get_screening_rules`, `test_screener`,
`screen_listings`, `open_screening_dashboard`, `run_google_places_export`.

`check_system_health` is the Priority 1 health-check tool from
`docs/MULTI_USER_TRANSITION.md` — its own tool description instructs Claude Desktop to
call it automatically once at the start of every conversation (not per-message, not
again later in the same conversation), stay silent if everything's healthy, and only
speak up when it finds a real problem (Outlook auth, scheduler, shared folder, portfolio
file, code version) — never blocking or delaying whatever the user actually asked for.
Scheduler job status is tracked in an in-memory `_scheduler_status` dict in
`mcp_server.py`, updated by each job's own try/except — deliberately not persisted, since
it describes this process's current run and should reset with the process.

### Auto-update (`release.py`, `apply_update.py`)
Priority 4 in `docs/MULTI_USER_TRANSITION.md`. `release.py` (run by whoever ships a
reviewed fix, never by staff) packages the current code — excluding `confidentials/`,
`data/`, any virtualenv, and `.git` — into a zip, and publishes it plus a version marker
to `config.UPDATES_DIR` (shared OneDrive). Staged rollout: `python release.py` publishes
to the `canary` channel only; `python release.py --promote` copies that same already-published
version's marker to the `general` channel once it's confirmed healthy. Each instance's
scheduler (`mcp_server.py::_check_and_stage_update`, daily at 5am) reads its own
`config.VAULTER_UPDATE_CHANNEL` (`.env`, defaults to `general`) and, if a newer version is
published there, downloads it into the local `config.PENDING_UPDATE_DIR` — it does **not**
apply it. `check_system_health` surfaces a staged update if one is waiting, and tells Claude
to ask the user whether to apply it now.

**Applying stays entirely inside the Claude Desktop conversation — no terminal, ever.**
Once the user says yes, Claude calls the `apply_pending_update` MCP tool, which calls
straight into `apply_update.py::apply_pending_update()`: syncs the new version's files into
place, then re-runs `pip install -r requirements.txt` with the same interpreter already
running the project (so a fix that adds/changes a dependency doesn't leave the app broken
for want of an uninstalled package), then clears the staging area. `apply_update.py`'s own
`python apply_update.py` CLI entry point (with a y/N prompt) still exists as a manual/
troubleshooting fallback, but is not the expected path. Either way, this first version of
the mechanism is deliberately confirm-then-apply, not fully automatic with zero human
involvement, given the "could break every instance at once" blast radius a bug in auto-apply
would have — the human decision just happens in chat instead of a terminal. The one manual
step that can't be automated at all: fully quitting and reopening Claude Desktop afterward,
since an MCP server can't restart its own parent application.

`apply_update.py`'s `PRESERVED_DIR_NAMES` must always match `release.py`'s
`EXCLUDED_DIR_NAMES` exactly — the apply step trusts that anything under those paths was
never in the package to begin with, so it never deletes or overwrites them.

`analysis/screening/pipeline.py`'s shared `manifest.json` entries are now stamped with a
`format_version` (`MANIFEST_FORMAT_VERSION`); `_find_cached_result` ignores any entry with a
*higher* format version than this code understands (falls through to a fresh screen) instead
of risking a misread — this is what lets an old and new version of the code share the same
manifest.json without corrupting each other mid-rollout. Bump `MANIFEST_FORMAT_VERSION` only
for a genuinely breaking shape change, not a purely additive one (old readers already ignore
fields they don't look for).

### CoStar Listing Screener (`analysis/screening/`)
A 4-phase pipeline, orchestrated end-to-end by `pipeline.py::run_full_screening()` (the
single entry point — called directly from the `screen_listings` MCP tool, no
subprocesses, no re-running upstream phases):

1. **`phase1_rules.py`** — deterministic hard-rule elimination (acreage, flood risk,
   land-use category, existing structures, stale listings).
2. **`phase2_ranking.py`** — composite score across 5 weighted dimensions for every
   Phase 1 survivor.
3. **`phase3_deep_analysis.py`** — sends top-ranked listings to Claude for qualitative
   writeups (strengths/risks/entitlement risk/MOIC fit) and a pursue/conditional/pass call.
4. **`phase4_verification.py`** — Google Maps ground-truth checks (elevation, places,
   roads, imagery, distance) on Phase 3's finalists, plus a final multimodal Claude
   verdict. Silently skips the Google enrichment step (not the whole phase) if
   `GOOGLE_MAPS_API_KEY` is unset — Phases 1-3 and finalist selection always run.

`workbook_builder.py` merges all 4 phases into one combined `.xlsx`, tracked in a
`manifest.json` in the same folder. Unlike everything else in this project,
`SCREENING_OUTPUT_DIR` (root `config.py`) is deliberately **shared** — it lives under
`SHARED_DIR`, auto-detected as the team's `OneDrive - Vaulter LLC` folder — so one
person's screening run is visible to the whole team instead of sitting only on their
own machine. `pipeline.py::run_full_screening()` hashes the input file's content and
checks the shared manifest before running Phase 3/4 — if this exact file was already
screened at the same `top_n`, it returns that cached result instead of re-paying for
Claude/Google Maps calls. Before that, it also reconciles any OneDrive conflict copies of
the shared manifest/caches back into the official files (C2), and if another machine has
a fresh in-progress marker for this exact file/settings, it **waits** (polling, up to 15
minutes) for that run's result instead of independently re-paying for Phase 3/4 (C3) — so
`screen_listings` can legitimately take a while to return in that specific case; that's
expected, not a hang. See Priority 2 / Part C in `docs/MULTI_USER_TRANSITION.md` for the
full concurrency-bug writeup these three fixes (C1-C3) address.
`dashboard_server.py` serves `dashboard/vaulter_dashboard.html`
(a local Pursue/Scrutinize/Pass viewer opened via `open_screening_dashboard`) with a
custom `translate_path` so it can read the shared output even though it lives outside
the project root. `config.py` (hard rules + output columns) and `scoring_config.py`
(approved scoring maps) inside this subpackage are screening-specific and separate
from the root `config.py`.

A CoStar file reaches `screen_listings` one of three ways (see
`mcp_server.py::_resolve_costar_source`): already ingested/dropped into
`data/watched_folder/` or `data/processed/` (searched by filename, optionally narrowed by
`property_name`), pasted directly into the Claude conversation as `file_content_b64`, or
neither — in which case the tool explains how to supply one.

## Conventions to preserve

- **Secrets never touch `config.py` or git.** All credentials go through
  `confidentials/.env` (gitignored) and are read once in `config.py` via `os.getenv`;
  every other module imports the resulting constant from `config`.
- **`main.py` (non-MCP mode) logs to both file and stdout; MCP mode logs to file only** —
  stdout is reserved for the MCP stdio transport, and any stray print/log to stdout there
  will break the connection to that instance's own Claude Desktop.
- **Missing optional API keys degrade gracefully, they don't crash.** `GOOGLE_MAPS_API_KEY`
  unset → skip Phase 4 enrichment only. Follow this pattern for any new optional integration.
- **The scheduler thread inside `mcp_server.py` must never die or exit** — its keepalive
  loop wraps everything in try/except specifically so a job failure can't take down the
  MCP server process. Preserve that isolation if you touch `_start_scheduler`.
