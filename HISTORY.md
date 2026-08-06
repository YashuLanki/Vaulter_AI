# Development history

This project's git history was reset on 2026-07-29. The repository is public by
design — it's a portfolio piece — and the earlier history contained the firm's
real deal names, prices, addresses and counterparties, which had no business
being publicly retrievable. Rewriting that history in place would have meant a
force-push over a public repo; deleting and recreating it was the simpler and
more reliable option.

Nothing about the code was lost in that reset. What *was* lost is the public
commit timeline, so it's preserved here: 152 commits from 2026-06-23 to
2026-07-29, in order. The subject lines are the record of how this system was
actually built — including the parts that were built, measured, and then
deleted because the measurement said to.

A few threads worth following in the list below:

- **The 2026-07 rebuild.** An ingest-and-embed pipeline (ChromaDB, per-user
  vector databases, a folder watcher, email ingestion, web scraping) was
  deleted in favour of reading a filesystem the company already had synced.
  Roughly 4,000 lines removed.
- **The screener replacing itself.** A four-phase rules pipeline was measured
  against the firm's own deal record, found to be eliminating listings on
  grounds the record showed were never dealbreakers, and replaced with a
  ranking model that eliminates nothing.
- **Costs stopped being invented.** A flat percentage assumption was replaced
  with per-lot figures measured from real budget workbooks, after the
  percentage turned out to be the wrong *shape*, not merely the wrong number.
- **Bugs that only a real file could find.** Several commits exist because a
  new spreadsheet template arrived with different columns and something
  silently misread it. Each one added an assertion to the test harness.

---

- `2026-06-23` — Initial commit — Vaulter AI Stage 1-3 complete
- `2026-06-23` — Stage 3 complete — MCP server with watcher, scheduler, file explorer tool
- `2026-06-23` — Updated mcp_server.py — email every 30min, open_property_files tool added
- `2026-06-23` — Fix email routing — weak matches go to processed/general/ not wrong property folder
- `2026-06-23` — Fix email routing, add open_general_files tool, email every 30min
- `2026-06-24` — Email pipeline — remove date filter, always pull latest 50, registry handles deduplication
- `2026-06-24` — Code audit fixes: remove hardcoding, fix bugs, clean duplicates, improve email body processing
- `2026-06-25` — Add four-stage listing screener with adaptive Stage 0 calibration
- `2026-06-25` — Fix screener: pre-assign verdicts, all-listings dashboard, investment thesis
- `2026-06-25` — Fix ChromaDB concurrent access — singleton client with threading locks
- `2026-06-25` — Fix Excel chunking in email_reader — double newline + 8000-char chunk tier keeps rows intact
- `2026-06-26` — Remove hardcoded categories from proximity_tool — now reads from config.json
- `2026-06-26` — Wire Google Places API key through config.py, move proximity_output to data/
- `2026-06-26` — Add proximity_search MCP tool — reads config from config.py, project master from data/project_master/
- `2026-06-26` — Fix duplicate highways in summary output
- `2026-06-26` — MCP proximity tool returns simple confirmation — full data in CSV
- `2026-06-26` — Fix proximity_search tool description — prevent Claude from using web search instead
- `2026-06-26` — Rename proximity_search to run_google_places_export to prevent Claude from overriding with web search
- `2026-06-26` — Fix MCP disconnect — suppress stdout logging in MCP mode to preserve stdio transport
- `2026-06-26` — Fix MCP disconnect — start MCP server immediately before background threads
- `2026-06-26` — Fix MCP crash — isolate background thread errors from killing MCP server
- `2026-06-29` — Fix MCP scheduler crash on Python 3.14; move proximity_tool to pipeline/; remove stale scripts
- `2026-06-29` — Fix screener: dynamic columns, top-10 pipeline, missing data scoring, pagination; fix MCP scheduler crash
- `2026-07-13` — screen and email
- `2026-07-13` — organized data files
- `2026-07-13` — Update .gitignore for data/registry, data/output, data/screening_uploads reorg
- `2026-07-13` — Route CoStar uploads through watched_folder instead of screening_uploads; clean up gitignore
- `2026-07-13` — update mcp_server
- `2026-07-13` — mcp_server
- `2026-07-13` — fix output of proximity and screening
- `2026-07-13` — delete
- `2026-07-13` — sample
- `2026-07-15` — fixation
- `2026-07-15` — Add CLAUDE.md with architecture and command reference for Claude Code
- `2026-07-16` — Add CLAUDE.md with architecture guidance for Claude Code
- `2026-07-16` — Merge with remote CLAUDE.md
- `2026-07-20` — Delete test_screening.py
- `2026-07-20` — Delete test_screening.py
- `2026-07-20` — Restore .env.example, extend .gitignore for Claude Code local settings, fix mcp_server.py typo
- `2026-07-20` — Restore .env.example, extend .gitignore for Claude Code local settings, fix mcp_server.py typo
- `2026-07-20` — Fix critical/high MCP bugs, switch to real embeddings, redesign for per-user local privacy
- `2026-07-20` — Fix critical/high MCP bugs, switch to real embeddings, redesign for per-user local privacy
- `2026-07-20` — Reduce screening API cost, fix a path-traversal bug, and fix a startup crash
- `2026-07-20` — Reduce screening API cost, fix a path-traversal bug, and fix a startup crash
- `2026-07-20` — Fix data-loss and correctness bugs found in the re-audit (batch 1)
- `2026-07-20` — Fix data-loss and correctness bugs found in the re-audit (batch 1)
- `2026-07-20` — Fix screening cache key bug, add crash-safe/race-safe file storage everywhere
- `2026-07-20` — Fix screening cache key bug, add crash-safe/race-safe file storage everywhere
- `2026-07-20` — Fix several small MCP tool bugs found in the re-audit (batch 2)
- `2026-07-20` — Fix several small MCP tool bugs found in the re-audit (batch 2)
- `2026-07-20` — Fix remaining README doc drift and a Roads API ambiguity bug
- `2026-07-20` — Fix remaining README doc drift and a Roads API ambiguity bug
- `2026-07-20` — Fix chunk-tier and chunker bugs found in the re-audit
- `2026-07-20` — Fix chunk-tier and chunker bugs found in the re-audit
- `2026-07-21` — Fix remaining ingestion/pipeline and screening bugs from the re-audit
- `2026-07-21` — Fix remaining ingestion/pipeline and screening bugs from the re-audit
- `2026-07-21` — Fix Excel Project Master not filtering sold/struck-through properties
- `2026-07-21` — Fix Excel Project Master not filtering sold/struck-through properties
- `2026-07-21` — Add multi-user transition analysis and roadmap, including meeting-transcript workstream
- `2026-07-21` — Add multi-user transition analysis and roadmap, including meeting-transcript workstream
- `2026-07-21` — Fix the three loose ends from the multi-user roadmap's Priority 0
- `2026-07-21` — Fix the three loose ends from the multi-user roadmap's Priority 0
- `2026-07-21` — Document confirmed architecture: portfolio docs move to existing OneDrive
- `2026-07-21` — Document confirmed architecture: portfolio docs move to existing OneDrive
- `2026-07-21` — Expand Priority 3 with concrete onboarding-simplicity findings
- `2026-07-21` — Expand Priority 3 with concrete onboarding-simplicity findings
- `2026-07-21` — Make the health-check tool proactive instead of on-demand
- `2026-07-21` — Make the health-check tool proactive instead of on-demand
- `2026-07-21` — Expand Priority 4 with a concrete auto-update design
- `2026-07-21` — Expand Priority 4 with a concrete auto-update design
- `2026-07-21` — Assume unmanaged machines for onboarding; fix corrupted title
- `2026-07-21` — Assume unmanaged machines for onboarding; fix corrupted title
- `2026-07-21` — Implement Priority 1: proactive health-check MCP tool
- `2026-07-21` — Implement Priority 1: proactive health-check MCP tool
- `2026-07-21` — Expand Priority 2's Part C with two verified findings
- `2026-07-21` — Expand Priority 2's Part C with two verified findings
- `2026-07-21` — Fix C1: refuse to silently wipe shared screening files
- `2026-07-21` — Fix C1: refuse to silently wipe shared screening files
- `2026-07-21` — Fix C2: recover OneDrive conflict copies of shared screening files
- `2026-07-21` — Fix C2: recover OneDrive conflict copies of shared screening files
- `2026-07-21` — Fix C3: avoid duplicate-paying when the same file is already being screened
- `2026-07-21` — Fix C3: avoid duplicate-paying when the same file is already being screened
- `2026-07-21` — Fix C4, C5, and a diagnostic-surfaced gap in PDF ingestion
- `2026-07-21` — Fix C4, C5, and a diagnostic-surfaced gap in PDF ingestion
- `2026-07-21` — Implement Priority 3: guided setup wizard for easy onboarding
- `2026-07-21` — Implement Priority 3: guided setup wizard for easy onboarding
- `2026-07-22` — Implement Priority 4: version stamping + non-technical auto-update
- `2026-07-22` — Implement Priority 4: version stamping + non-technical auto-update
- `2026-07-22` — Add double-click launchers for fully non-technical setup
- `2026-07-22` — Add double-click launchers for fully non-technical setup
- `2026-07-22` — Add design spec: shrink install footprint by dropping torch
- `2026-07-22` — Add design spec: shrink install footprint by dropping torch
- `2026-07-22` — Add implementation plan for the embedding footprint shrink
- `2026-07-22` — Add implementation plan for the embedding footprint shrink
- `2026-07-22` — Swap semantic search engine to ChromaDB's built-in ONNX model
- `2026-07-22` — Swap semantic search engine to ChromaDB's built-in ONNX model
- `2026-07-22` — Remove sentence-transformers (and its torch dependency) from requirements.txt
- `2026-07-22` — Remove sentence-transformers (and its torch dependency) from requirements.txt
- `2026-07-22` — Auto-reindex after apply_pending_update() if the embedding model changed
- `2026-07-22` — Auto-reindex after apply_pending_update() if the embedding model changed
- `2026-07-22` — Fix get_collection() crashing on any pre-existing database after the embedding swap
- `2026-07-22` — Fix get_collection() crashing on any pre-existing database after the embedding swap
- `2026-07-22` — Close the data-loss window in the embedding-function migration
- `2026-07-22` — Close the data-loss window in the embedding-function migration
- `2026-07-22` — Surface reindex outcome in apply_pending_update's chat response
- `2026-07-22` — Surface reindex outcome in apply_pending_update's chat response
- `2026-07-22` — Fix stale sentence-transformers reference in setup_wizard.py comment
- `2026-07-22` — Fix stale sentence-transformers reference in setup_wizard.py comment
- `2026-07-22` — Add design spec: one-click .mcpb Desktop Extension packaging
- `2026-07-22` — Add design spec: one-click .mcpb Desktop Extension packaging
- `2026-07-22` — Reorganize project structure: move setup/utility scripts to scripts/ and core/ folders
- `2026-07-22` — Reorganize project structure: move setup/utility scripts to scripts/ and core/ folders
- `2026-07-22` — Merge branch 'main' of https://github.com/YashuLanki/Vaulter_AI
- `2026-07-22` — Merge branch 'main' of https://github.com/YashuLanki/Vaulter_AI
- `2026-07-22` — Close a .gitignore gap that let a real database backup get committed
- `2026-07-22` — Move proximity mapping output to shared OneDrive
- `2026-07-22` — Merge: keep proximity output on shared OneDrive
- `2026-07-22` — Add MEETINGS_DIR to config for shared Monday-meeting transcripts
- `2026-07-22` — Add implementation plan for one-click .mcpb packaging
- `2026-07-22` — Remove chroma_db_corrupted_backup from repo
- `2026-07-23` — Fix regressions from earlier scripts/ reorganization
- `2026-07-23` — Fix wrong file path silently zeroing out email/web counts in health check
- `2026-07-23` — Fix Windows console encoding crash affecting CLI and setup wizard
- `2026-07-23` — Fold Outlook sign-in into the setup wizard's last step
- `2026-07-23` — Match Claude Desktop config key to this team's actual established convention, fix broken repo link
- `2026-07-23` — Guard cmd_auth() against a failed device-code flow crashing the launcher
- `2026-07-23` — Add org-wide settings distribution for new features, close a .gitignore gap
- `2026-07-23` — Organize setup/auth helper scripts into quick_start/ folder
- `2026-07-27` — Rebuild: replace ingest pipeline with direct library access, drop Google API keys
- `2026-07-28` — Add repo-cleanup skill, remove dead config left over from the rebuild
- `2026-07-28` — Remove the .mcpb one-click packaging plan and its design spec
- `2026-07-28` — Screen CoStar exports by portfolio fit instead of hard-rule elimination
- `2026-07-28` — Price listings against the exit product, not same-size peers
- `2026-07-28` — Verify listings against federal ArcGIS; add Coolidge jurisdiction dossier
- `2026-07-28` — Make the screener market-portable; add a multi-market check harness
- `2026-07-28` — Surface the four component scores behind Fit_Score
- `2026-07-28` — Generate a visual screening report; retire the threaded dashboard server
- `2026-07-28` — Retire the 4-phase pipeline; repoint the rule tools at fit_screen
- `2026-07-28` — Remove the last paid-API dependency; prune geo_providers
- `2026-07-28` — Run proximity on a screened listing, not just an owned property
- `2026-07-28` — Rename the proximity tool; remove the last API key
- `2026-07-28` — Update the recap skill to describe the screener that actually exists
- `2026-07-28` — Replace the screener's invented costs with figures measured from the deal record
- `2026-07-28` — Fix a crash and two silent misreadings exposed by a thinner CoStar template
- `2026-07-28` — Resolve the columns each CoStar export actually has, instead of assuming one shape
- `2026-07-28` — Match columns by pattern and value, and find the header wherever it is
- `2026-07-28` — Preload pandas before the event loop starts -- the cause of the "server not responding" hangs
- `2026-07-29` — Fine-tune screening and proximity; fix a wrong answer, a crash, and a report a version behind
- `2026-07-29` — Add screening/dashboard QA subagents, then use them to catch three real bugs on a new market
- `2026-07-29` — Stop assuming the document library's folder name matches this machine's exactly
- `2026-07-29` — Fix a permanent re-staging loop in auto-update -- version tracking never moved
- `2026-07-29` — Stop shared-folder output from accumulating forever; add a subagent to keep checking it
- `2026-07-29` — Keep the firm's data out of a deliberately public repo, and add a hook that enforces it