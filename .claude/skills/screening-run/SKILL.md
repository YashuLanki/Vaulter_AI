---
name: screening-run
description: Use when asked to screen a CoStar export or broker spreadsheet, run the 4-phase listing screening pipeline, or when the user invokes /screening-run <file> [top_n]
argument-hint: <filename-or-substring> [top_n]
---

# Run CoStar Screening Pipeline

Runs `analysis.screening.pipeline.run_full_screening()` — the single entry point
for all 4 phases (rules → ranking → Claude deep analysis → Google verification).
Never run phases individually, never edit `test_screening.py` for a one-off run.

## Before running — two checks

1. **Locate the file.** Match the same way `mcp_server.py::_resolve_costar_source`
   does — case-insensitive **substring** search of `data/watched_folder/` first,
   then `data/processed/` recursively (covers `unknown/` and `general/`). If the
   exact name the user gave doesn't exist, search with a shorter substring
   (e.g. `CostarExport`) before giving up.
2. **Confirm cost.** Phase 3 makes ~`top_n` Claude API calls; Phase 4 adds up to
   5 more plus Google Maps calls. Tell the user the approximate spend and get a
   go-ahead unless they already told you to run it.

## The command

From repo root, with the venv interpreter (`top_n` defaults to 10; pass what the
user asked for):

```powershell
.venv\Scripts\python.exe -c "from pathlib import Path; import config; from analysis.screening.pipeline import run_full_screening; r = run_full_screening(source_path=Path(r'<RESOLVED_PATH>'), anthropic_api_key=config.ANTHROPIC_API_KEY, google_api_key=(config.GOOGLE_MAPS_API_KEY or None), top_n=<N>); print(r['market'], '|', r['total_screened'], 'screened |', r['phase1_survivors'], 'survivors'); print('workbook:', r['workbook_path']); [print(' -', c['address'], '|', c.get('composite_score'), '|', c.get('recommendation_snippet')) for c in r['top_candidates']]"
```

- `google_api_key=... or None` preserves graceful degradation: unset key skips
  only Phase 4's Google enrichment, everything else still runs.
- Runtime is minutes, not seconds (real API calls) — use a generous timeout or
  run in background.

## After it finishes

- Combined workbook: `data/output/screening/screening_<Market>_<timestamp>.xlsx`
  (4 tabs, Phase4 → Phase1); `manifest.json` in the same folder is auto-updated.
- Report market, counts (total / Phase 1 survivors / finalists), workbook path,
  and the top candidates with scores.
- Offer the dashboard: the `open_screening_dashboard` MCP tool, or
  `analysis.screening.dashboard_server.start_dashboard_server(Path('.'))`.

## Common mistakes

| Mistake | Instead |
|---|---|
| Editing `COSTAR_FILE`/`TOP_N` in `test_screening.py` | It's a tracked file; use the one-liner above |
| Exact-name file search fails → give up | Substring search, watched_folder then processed |
| Bare `python` | `.venv\Scripts\python.exe` (deps live in the venv) |
| Running without mentioning cost | Confirm ~top_n Claude calls + Google spend first |
