# Vaulter AI — Property Intelligence System

A local MCP server that gives a real estate investment firm's team searchable
access to the firm's document library and a 4-phase-turned-single-pass listing
screener, entirely through their own Claude Desktop or Claude Code — no
separate UI, no cloud backend, no shared server.

This repo is **deliberately public** — it's a portfolio piece. All real firm
data (deal names, prices, addresses, counterparties) has been kept out of
tracked files by design; see [Security](#security) below for how that's
enforced, not just intended. [`HISTORY.md`](HISTORY.md) has the full build
history: 150+ commits, including the parts that were built, measured, and then
deleted because the measurement said to.

## What it does

- **Search the firm's document library** by filename and path — ~490,000
  files synced from SharePoint via OneDrive, indexed locally, never uploaded
  anywhere.
- **Screen a CoStar export or broker spreadsheet** by fit against the firm's
  existing portfolio — ranks and explains every listing, eliminates nothing,
  self-calibrates to any market from peers inside the file itself.
- **Map what's actually near a property** — every business, school, and piece
  of infrastructure within a radius, from free public data (OpenStreetMap,
  FEMA, Census).
- **Compare a new deal to the firm's own history** — most-similar past deals,
  what happened to them, and market conditions each was bought into.
- **Maintain per-property summaries** the whole team shares, so the first
  real question about a property is the only one that pays to read the source
  documents.

No API keys anywhere in this system. Document search is local, ranking is
arithmetic, ground truth is federal open data, proximity is OpenStreetMap, and
the qualitative judgment happens in the Claude conversation that asked for it
— already paid for.

## Agentic environment

Each team member runs their **own local copy** of the MCP server
(`system/mcp_server.py`), launched by their **own** Claude Desktop or Claude
Code over stdio — never over a network. There's no shared secret and no
`MCP_API_KEY`; the access boundary is simply "is this your own computer,
logged in as you." The server runs on the main thread with **no background
threads at all** — a design constraint that survived a full rebuild
specifically because a stuck background thread is the kind of failure a
non-technical user can't diagnose.

### Three layers, not one undifferentiated pass

Generic agent-framework language calls this "workflows, agents, tools." Mapped
onto what actually exists here:

| Layer | What it is | Where it lives |
|---|---|---|
| **1. Skills** | Markdown playbooks — objective, which tools/subagents to use, expected output, edge cases, written the way you'd brief a colleague | `.claude/skills/*/SKILL.md` |
| **2. Agents** | Claude's own role (main session or a subagent) — reads the relevant skill, sequences tools/subagents, handles failures, asks when it should | `.claude/agents/*.md` |
| **3. Tools** | Deterministic Python — same input, same output, every time | `system/analysis/`, `system/pipeline/`, `system/corpus/`, `system/scripts/` |

The reasoning for keeping these separate rather than asking one agent pass to
do everything: accuracy compounds *downward* through a chain of probabilistic
steps (five steps at 90% each chains down to 59%), so anything that can be
deterministic *should* be, and the agent layer stays focused on orchestration
— sequencing, judgment calls, and knowing when to stop and ask.

### The desks

Skills and subagents are organized into seven domains ("desks"), each with one
lead and its workers. A subagent can't spawn other subagents, so every desk's
lead is a **skill** (a playbook the main session runs, fanning out to
workers) — except the desks small enough that the worker *is* the whole desk.

| Desk | Lead (skill) | Workers (subagents) | Deterministic core |
|---|---|---|---|
| CoStar screening | `vaulter-screening-pipeline` | screening-checker, report-checker, fact-checker | `fit_screen.py`, `report.py`, `check_screener.py` |
| Proximity mapping | `proximity-mapping` | onedrive-auditor, fact-checker | `proximity_tool.py`, `geo_providers.py`, `geo_federal.py` |
| Connector health | `mcp-health-check` | connection-doctor | `check_mcp_health.py` |
| Install & onboarding | agent-led | setup-tester | `setup_wizard.py`, `release.py` / `apply_update.py` |
| Documents & research | `document-research` | document-reader, city-researcher, fact-checker | `system/corpus/` |
| OneDrive shared folder | agent-led | onedrive-auditor | `system/config.py` path layer |
| Security | agent-led + hook | leak-guard | `.claude/hooks/check_no_leaks.py` |

### Agents used

| Subagent | What it's for |
|---|---|
| `vaulter-screening-checker` | Adversarially checks whether a CoStar/broker export was read correctly and ranked fairly, regardless of market or column shape |
| `vaulter-report-checker` | Verifies a generated screening report is factually correct and genuinely readable by someone with no background in the tool |
| `vaulter-document-reader` | Extracts findings from due diligence documents — PDFs, surveys, memos, scanned and very long files — with file+page citations |
| `vaulter-city-researcher` | Builds a jurisdiction dossier — capital improvement plans, zoning, utilities, school district, development trajectory |
| `vaulter-fact-checker` | Adversarially verifies one specific claim before it's trusted, especially a numeric threshold headed for an investment memo |
| `vaulter-connection-doctor` | Investigates and fixes a broken MCP connector, dispatched automatically the moment any tool call errors or hangs |
| `vaulter-setup-tester` | Verifies a teammate could install and connect from scratch — fresh clone, setup wizard, first MCP handshake |
| `vaulter-onedrive-auditor` | Audits the team's shared OneDrive folder for accumulation and duplication; proposes cleanup, never deletes unilaterally |
| `vaulter-leak-guard` | Audits this repo for real firm-confidential data leaking into tracked files, and that `.gitignore` actually covers what it should |

### Skills used

`screening-run`, `vaulter-screening-pipeline`, `proximity-mapping`,
`document-research`, `mcp-health-check`, `vaulter-rebuild`, `commit_git`,
`cleanup`, and `recap` — each a playbook under `.claude/skills/*/SKILL.md`
that a session runs directly or fans out from.

## Design principles

- **Rank, never eliminate.** A hard filter is a guess with no error message —
  measured directly: an earlier rules-based screener once eliminated 60 of 69
  real listings on grounds later shown not to be real dealbreakers.
- **Deterministic detection, never unattended writing.** Automation may
  *detect* a gap (a missing summary, a stale index) but never auto-writes
  content without a human in the conversation loop.
- **A fast, confident empty answer is the most dangerous answer.** A provider
  that returns "0 results" with no data for the region is worse than one that
  errors — several real incidents in this system's history came from
  conflating "nothing found" with "couldn't check."
- **Say what the data can't tell you.** Every screening run reports its own
  evidence coverage per state, and every comparison names its own confidence,
  rather than staying silent about a thin sample.

## Setup

```bash
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r system/requirements.txt
python system/scripts/setup_wizard.py               # guided setup, builds the index
```

## Everyday use

```bash
python system/main.py mcp                       # start the MCP server (what Claude Desktop runs)
python system/main.py index-corpus               # (re)build the document-library index
python system/main.py search "closing memo"      # search the library by filename/path
python system/main.py screen CostarExport.xlsx   # rank a CoStar export by portfolio fit
python system/main.py properties                 # list the portfolio
python system/main.py stats                       # what this instance has available
```

## Repository layout

```
vaulter_ai/
  quick_start/   the double-click installer (the only folder a teammate opens)
  system/        everything the program runs on -- also the only half that ships
  docs/          internal engineering notes; never packaged, never shipped
  .claude/       agents, skills, hooks (developer tooling)
  CLAUDE.md      the full engineering reference -- architecture, every design
                 decision, and why each one was made
  HISTORY.md     the pre-2026-07-29 commit history, preserved as a record
```

`CLAUDE.md` is the deep reference this README doesn't try to duplicate — every
module, every measured bug, and the reasoning behind each design decision.

## Security

This repo went through a full audit (2026-07-29) that moved every real name,
price, and address out of tracked files and into a gitignored appendix. That
alone doesn't stay true on its own, so a `PreToolUse` hook
(`.claude/hooks/check_no_leaks.py`) blocks any `git commit` or `git push`
whose diff contains a forbidden path, a credential shape, or a name on a
maintained (gitignored) blocklist — checked on every write, not just when
someone remembers to audit. The hook fails **closed**: if its name list is
ever unreadable (for example, in an isolated git worktree that doesn't
inherit gitignored files), it blocks rather than silently skipping the check.
