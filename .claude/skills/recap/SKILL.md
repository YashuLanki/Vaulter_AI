---
name: recap
description: Use when the user wants to reorient on this project — where we left off, what's next, what we're trying to do overall — or when they invoke /recap. Good for the start of a new conversation/session.
---

# Recap: Where We Left Off

Answer three questions, in order, as a short synthesized brief — never dump raw git/tool
output. This is for reorienting after time away, not a changelog.

## 1. What we're building (one line, skip if already obvious from conversation)
Vaulter AI Property Intelligence System: searchable access to the firm's OneDrive-synced
SharePoint document library, plus a CoStar screener that ranks listings by fit against the
existing portfolio — both through each staff member's own local MCP server + Claude Desktop. Full detail is in CLAUDE.md — don't
re-read it, just don't contradict it.

Note it was **rebuilt across 2026-07-27/28**: the ingest/ChromaDB/email/scraping pipelines went
first, then the 4-phase screening pipeline itself. If something refers to ChromaDB, the PDF
watcher, Outlook ingestion, the M365 connector, `run_full_screening`, a phase module, the
threaded dashboard server, or an API key, that is history — none of it exists. The project now
calls no paid service at all.

## 2. What just happened
Run in parallel:
- `git log -20 --oneline`
- `git status`

Read `docs/REBUILD_PLAN.md` — this is the project's living roadmap. §0 records the "no
connectors" verdict, §§1–4 are what's built, §§5–7 are what's next (buy-box standard as a
readable document, area intelligence tiers A/B/C, open questions).

`docs/MULTI_USER_TRANSITION.md` is **historical** — its Priority 0–2 items described problems
in code that no longer exists. Don't present them as open work.

Match recent commit subjects against REBUILD_PLAN's sections. If commits don't obviously map
to any of them, say so plainly instead of forcing a fit.

Then check for saved project memory: read
`C:\Users\YashuLanki\.claude\projects\C--Users-YashuLanki-vaulter-ai\memory\MEMORY.md` if it
exists, and open any linked memory files whose description looks relevant (project-type
entries especially — they carry deadlines/decisions git history won't show, like *why* a
priority was reordered or who's waiting on what).

If `git status` shows uncommitted changes, read enough of the diff to describe what's
in-flight, not just that something is dirty.

## 3. What's next
From REBUILD_PLAN §§4–7, name the next concrete unfinished item. Only claim something is
"done" if the code actually supports it (e.g. grep for a specific function/tool name) — don't
infer completion from commit messages alone when it's cheap to check.

## Output shape

Keep it to a short brief, roughly:

- **Building:** one line
- **Last worked on:** priority/part + what specifically landed, with rough dates from `git log`
- **In progress / uncommitted:** only if `git status` is dirty
- **Next:** the concrete next step

## After recapping
If this surfaced project context that isn't yet captured in memory (a priority reorder, a new
decision, a deadline) and would be useful in a future session, save it as a project-type memory
per the memory system instructions already in context — don't ask permission first, just do it
silently as part of wrapping up.
