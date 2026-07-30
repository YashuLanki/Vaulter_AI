# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Vaulter AI Property Intelligence System — a Python system for a real estate investment
company that gives each team member searchable access to the firm's document library and
runs a 4-phase CoStar listing screening pipeline, entirely through their own local MCP
server connected to their own Claude Desktop (no separate UI). `main.py` is the single CLI
entry point; `mcp_server.py` is what actually runs in production, serving MCP tools over
stdio on the main thread with **no background threads**.

**Rebuilt 2026-07.** The system used to ingest PDFs/emails/web data into a per-user
ChromaDB vector database. All of that is gone — see `docs/REBUILD_PLAN.md` §0–2 for what
was removed and why. In short: the firm's SharePoint library is already synced to disk by
OneDrive, so copying it into a local vector database was duplicating what the filesystem
already had; and Claude's M365 connector, which an earlier draft of the plan depended on,
was ruled out. Email ingestion was dropped deliberately, to be rebuilt later.

## Commands

```bash
# Setup
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements.txt
python scripts/setup_wizard.py           # guided setup, ends by building the index

# Everyday
python main.py mcp                       # start the MCP server (what Claude Desktop runs)
python main.py index-corpus              # (re)build the document-library index (~2 min)
python main.py search "closing memo"     # search the library by filename/path
python main.py screen CostarExport.xlsx  # rank a CoStar export by portfolio fit (free)
python main.py screen export.xlsx 2.5    # ...at a 2.5x MOIC target instead of the 3x default
python main.py properties                # list the portfolio from the Project Master
python main.py stats                     # what this instance has available
```

There is no lint/test framework configured (no pytest, no linter config).
`.claude/hooks/check_python_syntax.py` runs `py_compile` on every `.py` Claude edits —
that hook is the only automated safety net in the repo.

## Architecture

### Cross-cutting: `config.py`
Every path, credential, and tunable constant lives here — this is the only file that
needs to change to port the project to a new machine. It cross-platform-detects Windows
vs Mac, loads `confidentials/.env` via `python-dotenv`, and creates all `data/` subfolders
on import. Nothing else in the codebase should hardcode a path or read `os.environ`
directly for these values.

`SECRETS_DIR` is the project's own `confidentials/` folder on every OS. Windows also checks
one legacy hardcoded location (`C:\Users\<USERNAME>\Vaulter AI\confidentials`) as a
fallback, but *only* if that path already has a real `.env` and the project folder doesn't
— so the one pre-existing setup keeps working without being switched out from under it.

Two OneDrive paths are derived from a single detected account root and must not be
confused: `SHARED_DIR` (`Vaulter AI Shared`) is this system's own output, written to;
`CORPUS_DIR` (`Vaulter LLC - shaw`) is the firm's document library, read-only.
`CORPUS_DIR` is deliberately never `mkdir`'d — if it's missing, that means OneDrive isn't
syncing the library, which `check_system_health` needs to report rather than paper over
with an empty folder.

### Data access: the firm's document library (`corpus/`)
The firm's SharePoint library is synced to disk by OneDrive at `config.CORPUS_DIR`
(`OneDrive - Vaulter LLC/Vaulter LLC - shaw`). `corpus/` reads it; nothing writes to it.
Two properties govern every decision in that package:

**Scope is the privacy boundary.** `CORPUS_DIR` is the shaw library specifically, never
the OneDrive account root one level up — the root also holds the individual's own
`Desktop`, `Documents`, and `Microsoft Teams Chat Files`. `corpus.resolve_in_corpus()`
resolves and re-checks every path and raises `OutsideCorpus` on anything that escapes.
**Every new code path that touches a corpus path must go through it** — do not build a
path by string-joining onto `CORPUS_DIR` yourself.

**Search matches names, not contents — and this is load-bearing, not a shortcut.** The
library is ~493,000 files synced as OneDrive Files On-Demand *placeholders*: filenames
are local, file bytes are not, and opening one downloads it. Grepping the corpus would
download the entire library. So `corpus/index.py` caches names/sizes/mtimes in a SQLite
index (`data/corpus_index.db`, built by `main.py index-corpus`) and searches that;
`corpus/extract.py` reads content only for one file at a time, deliberately chosen.
If you add a retrieval feature, do not "improve" this into full-text search over the
library without solving hydration first.

The MCP tool descriptions state the names-not-contents limitation explicitly, because the
dangerous failure is Claude concluding "the firm has no records on X" when it only means
"no filename matched X." Preserve that wording.

Ranking uses three signals: all terms must match, a filename hit beats a folder hit, and a
whole-phrase hit beats both. The phrase bonus is not cosmetic — without it, searching a
property named `<Name> 10` ranked the adjacent `<Name> 80` parcel's files first, because `10`
matches inside dates like `20260107`.

### Proximity (`pipeline/proximity_tool.py`)
One Overpass query returns every POI category at once within a radius, classified locally, and
exports CSV + XLSX to the shared folder. It is the only remaining OpenStreetMap consumer —
POI category search has no federal equivalent, so `geo_federal` cannot replace it.

Two entry points. **By name** (`run_proximity_for_property`) looks a portfolio property up in the
hand-verified `property_coordinates.csv` and **refuses** if it has none — do not add a
geocode-the-name fallback, that was measured at 5 wrong out of 8, two in the wrong country, and
it fails silently. **By coordinate** (`run_proximity_for_listing`) takes a rank from the screen
and uses the CoStar export's own coordinates; the refusal does not apply because nothing is being
guessed. Both produce the same format, so a candidate and an owned property compare directly.

### The portfolio (`portfolio.py`)
Reads the Smartsheet Project Master export from `data/project_master/`. CSV and .xlsx
only — the PDF/OCR parsing path was dropped in the rebuild. Note only .xlsx can represent
a sold deal (strikethrough via `cell.font.strike`); a CSV export yields every row active
and an empty sold list. `find_project_file()` explicitly skips `property_coordinates.csv`,
which lives in the same folder but belongs to `pipeline/property_coordinates.py`.

### MCP server (`mcp_server.py`)
The production entry point. `create_mcp_server()` registers all `@mcp.tool()`-decorated
functions; `run_mcp_server()` calls `mcp.run(transport="stdio")` and nothing else — there
are **no background threads**. The PDF watcher and APScheduler thread were removed in the
rebuild, and with them the "the scheduler thread must never die" constraint. Do not
reintroduce a background thread here; if something needs scheduling, use an OS-level
scheduled task on one designated machine.

Each staff member runs their own local copy, launched by their own Claude Desktop over
stdio — never over a network. There is no `MCP_API_KEY` or shared secret; the access
boundary is "is this your own computer, logged in as you." claude.ai (the web app) cannot
be used with this server: it runs in the cloud and can only reach a network address, never
a process on someone's own machine. Claude Desktop or Claude Code are required.

**21 tools.** Don't maintain this list by hand — it drifted to 19 entries with one duplicated
and two missing. Get the truth from the code:

```bash
python -c "import asyncio; from mcp_server import create_mcp_server; \
  print(sorted(t.name for t in asyncio.run(create_mcp_server().list_tools())))"
```

Grouped by what they're for: **health & updates** — `check_system_health`,
`apply_pending_update`, `apply_pending_settings`, `get_pending_setup_details`.
**Documents** — `search_documents`, `read_document`, `browse_documents`.
**Portfolio** — `get_property_info`, `get_portfolio_list`, `get_properties_by_stage`,
`open_property_files`. **Screening** — `screen_listings`, `get_screening_rules`,
`test_screener`, `verify_listings`, `open_screening_dashboard`, `open_costar_folder`.
**Proximity** — `run_proximity_for_property`, `run_proximity_for_listing`,
`compare_proximity_to_portfolio`, `open_proximity_files`.

`check_system_health` is called automatically once at the start of every conversation (its
own tool description instructs Claude to do so), stays silent when healthy, and only speaks
up on a real problem — never blocking whatever the user actually asked for. It also runs
the once-daily check for a staged code update or org setting, which used to be a 5am
scheduled job; that is the only piece of the scheduler that survived, and it lives here
precisely so no thread is needed.

That covers whether the *data behind* a healthy connector is in good shape. Whether the
*connector itself* is reachable and fast is a different failure mode (found 2026-07-30:
`check_system_health` itself hung 60-240+s on a stuck git subprocess — see
`docs/agents/mcp-doctor/memory.md` for the full account and fix). `create_mcp_server()`'s own
`instructions=` string tells Claude to invoke the `vaulter-mcp-doctor` subagent automatically,
for any teammate, the moment any `vaulter_ai` tool call errors or hangs — no scheduled/background
process involved, consistent with this file's "no background threads" rule; it fires only in
reaction to a real tool-call failure inside an active conversation. `scripts/check_mcp_health.py`
is the deterministic check it runs first — it drives a genuine `python main.py mcp` subprocess
over real stdio rather than importing `mcp_server.py` and calling a tool function in-process,
because the 2026-07-30 hang never reproduced through the in-process shortcut, only through the
real transport.

### Auto-update (`scripts/release.py`, `scripts/apply_update.py`)
Priority 4 in `docs/MULTI_USER_TRANSITION.md`. `scripts/release.py` (run by whoever ships a
reviewed fix, never by staff) packages the current code — excluding `confidentials/`,
`data/`, any virtualenv, and `.git` — into a zip, and publishes it plus a version marker
to `config.UPDATES_DIR` (shared OneDrive). Staged rollout: `python scripts/release.py` publishes
to the `canary` channel only; `python scripts/release.py --promote` copies that same already-published
version's marker to the `general` channel once it's confirmed healthy. Each instance's
scheduler (`mcp_server.py::_check_and_stage_update`, daily at 5am) reads its own
`config.VAULTER_UPDATE_CHANNEL` (`.env`, defaults to `general`) and, if a newer version is
published there, downloads it into the local `config.PENDING_UPDATE_DIR` — it does **not**
apply it. `check_system_health` surfaces a staged update if one is waiting, and tells Claude
to ask the user whether to apply it now.

**Applying stays entirely inside the Claude Desktop conversation — no terminal, ever.**
Once the user says yes, Claude calls the `apply_pending_update` MCP tool, which calls
straight into `scripts/apply_update.py::apply_pending_update()`: syncs the new version's files into
place, then re-runs `pip install -r requirements.txt` with the same interpreter already
running the project (so a fix that adds/changes a dependency doesn't leave the app broken
for want of an uninstalled package), then clears the staging area. `scripts/apply_update.py`'s own
`python scripts/apply_update.py` CLI entry point (with a y/N prompt) still exists as a manual/
troubleshooting fallback, but is not the expected path. Either way, this first version of
the mechanism is deliberately confirm-then-apply, not fully automatic with zero human
involvement, given the "could break every instance at once" blast radius a bug in auto-apply
would have — the human decision just happens in chat instead of a terminal. The one manual
step that can't be automated at all: fully quitting and reopening Claude Desktop afterward,
since an MCP server can't restart its own parent application.

`scripts/apply_update.py`'s `PRESERVED_DIR_NAMES` must always match `scripts/release.py`'s
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

**`fit_screen.py` is the live screener** and is what the `screen_listings` MCP tool and
`python main.py screen` both call. It ranks a CoStar export by **fit against the existing
portfolio** rather than against absolute thresholds, makes **no API calls**, and
**eliminates nothing**.

Read `fit_screen.py`'s module docstring before changing it — it records the measured reason
it replaced Phase 1/2. On a real 216-row export, Phase 1 eliminated 69 listings and **60 of
those died on grounds `docs/COMPANY_PROFILE.md` §5 explicitly lists as *not* dealbreakers**
(46 flood, 14 existing structure), including 11 sitting within 3 miles of a property the firm
already owns. Phase 1 also scored long days-on-market as risk, when the firm's own stated #1
rationale on one of its best-returning acquisitions was a distressed basis.

Four rules that must survive any edit:
- **Never eliminate.** Rank and explain. §8.1 — rejection history is thin, so a hard filter
  is a guess with no error message.
- **Never hardcode a market.** Every market-relative figure comes from a peer group found
  inside the export (Submarket Cluster → Submarket → County → Market → whole file, first one
  with enough rows), keyed by land type as well as geography. Feed it Texas or Colorado and it
  recalibrates. Type belongs in the key: without it an agricultural parcel priced against
  commercial peers scored as needing 0.1x the peer median to exit — flattering nonsense.
- **Price from the investor's seat, not the user's.** Vaulter is an opportunistic value-add
  predevelopment land investor targeting 2.5–3x MOIC by selling entitled positions to users
  and developers. The screen reports the exit each listing must reach to hit that multiple,
  never a user/spec-developer comp.
- **Compare to the exit product, never to same-size peers.** The value-add mechanism is
  subdivision and entitlement, so a 293-acre parcel exits as 20–100 acre parcels, and those
  exit as sub-20-acre ones. `Exit_Headroom` divides what the market pays for that *exit*
  product by what the deal needs it to pay; above 1.0 clears. This was a measured bug, not a
  refinement: comparing every listing to same-size peers made big parcels look like bargains
  because in Pinal, commercial land asks ~$239k/ac under 20 acres and ~$25k/ac over 100 — a
  10x spread driven purely by size. A current-use label like Agricultural maps to the
  residential/commercial product it would actually exit as (`_EXIT_TYPE_CANDIDATES`), taking
  the cheaper candidate so the test is never flattered.
- **No two CoStar exports have the same columns, and the header is not always row 1.** Every
  export is shaped by whoever built the report, so **nothing indexes a raw column name** —
  `normalise_columns()` resolves each concept by alias, then by *pattern plus a value check*, then
  derives it (square feet → acres; acreage parsed out of a listing title like "±73.55 acres at
  NWC Moore Rd"), and reports where each one came from. Both halves of the match are needed:
  name alone put `Floodplain Area` in the acreage slot, and **`Land Area (SF)` read 1.7 million
  square feet as 1.7 million acres** — silently, with every downstream figure wrong. Hence the
  `avoid` patterns and a plausibility range capped near the firm's largest evaluated deal
  (roughly 4,500 acres). `_header_row()` finds the real header, because a broker's
  spreadsheet often opens with a title block — and on CSV that must be `skiprows`, not `header=`,
  or pandas fixes the column count from the junk line and throws. `Proposed Land Use` outranks `Property Type` deliberately: on a land export the latter is
  the constant "Land", present but useless, and would mask the better column. A real 24-column
  Tucson export recovered its land type from `Proposed Land Use` on 41 of 50 rows this way.
  `screen_listings` then prints what was found elsewhere, what is sparse and what is absent —
  silence about that is what made a near-empty file read as a flat, dull market.
- **Tiers rank with `method="max"`, never `"min"`.** With min, every row in a tied group inherits
  the rank of the group's *first* member, so 197 listings tied at the bottom took rank 20 and the
  whole file landed in Tier 1. And when every score is identical there is no ranking to express:
  that reports `Unranked — nothing in this file separates them` rather than promoting everything.
- **Report confidence, never fabricate a comp.** `Pricing_Confidence` and `Exit_Comp_N` travel
  with every row. A 20-row export legitimately yields low confidence; a file with no prices
  yields "untestable" on every row. Both are correct outputs — silence about sample size is not.
- **Costs are measured, and a cost with no record is declared, never estimated.** Rewritten
  2026-07-28 from the firm's own budgets and settlement statements — see
  `docs/PORTFOLIO_STANDARD.md` for every source path. The invented `cost_load` (0.35 of
  purchase) is gone: entitlement is priced **per lot** in every budget the firm has produced
  and falls with project size ($8,891/lot at 48 lots → $2,000/lot at 220), so a percentage of
  purchase price was the wrong *shape*. `lots_per_acre` fell 8.0 → 3.5; nothing supported 8.
  Carry is charged at a measured tax rate over the *observed* hold, and is a floor.
  Non-residential rows carry no entitlement figure because none exists, so `Cost_Basis` states
  on each that the required exit is understated — uniform treatment, so ranking within a type
  is unaffected.
- **Horizontal development stays out of the arithmetic on purpose.** Measured at
  $70–99k/acre but **only in Pinal County**, and the firm sells entitled rather than improved
  land, so it applies only where the exit comp is improved. Quoted as context on wide-headroom
  rows. Applying a Pinal figure to a Texas listing would be inventing again.
- **Say what the portfolio can't tell you.** Every run returns `evidence_coverage` per state.
  Arizona is fully evidenced; California partial; everywhere else has none. An unevidenced
  market still ranks normally and says so — marking it down would rank the firm's own data
  coverage rather than the deals, the same bug the neutral proximity floor prevents.
- **Keep the time reality visible.** `vaulterup.com` publishes 2.40x @5yr, 1.71x @10yr,
  1.61x @15yr. Measured holds ran **5.9–15.1 years** against 30–60 months underwritten, and 21
  properties bought 2011–2015 are *still held*, so the completed-deal sample is
  survivorship-biased. The gap is explained, not mysterious: entitlement schedules slip
  **2.5–4x** (one measured project went from a 9.6-month plan to 23.5 months with the start
  date unmoved). The screen prints implied IRR at both horizons so a 3x pro forma can't be
  read innocently.

Every tunable lives in `ASSUMPTIONS` at the top of the module, deliberately in one place so a
partner can argue with it. Each now carries its source. **The four `WEIGHTS` are the only
numbers left with no evidence at all** — two document searches found nothing in the corpus that
ranks or weights selection factors. They need a senior partner's judgment, not another search.
(Real names and figures behind every genericized citation in this file live in
`docs/EVIDENCE_APPENDIX.md`, local-only — this repo is deliberately public.)

`scripts/check_screener.py` runs **68 assertions** across deformed market shapes. Run it after
any change to `fit_screen.py`. Note it covers the screener only — **`geo_providers.py` has no
automated coverage at all**, and that is where the worst measured bug of 2026-07-29 lived (see
the proximity note below).

#### What was removed with it
`pipeline.py`, `phase1_rules.py`, `phase2_ranking.py`, `phase3_deep_analysis.py`,
`phase4_verification.py`, `workbook_builder.py`, `scoring_config.py`, `market_utils.py` and the
screening-local `config.py` are all deleted — about 2,500 lines. They were reachable from
nothing once `screen_listings` moved to `fit_screen`. Phase 4's ground truth lives on in
`geo_federal.py`, which checks flood over the parcel's **area** rather than its centre point;
that difference caught a real wrong answer. Phase 3's qualitative pass belongs in the
conversation, where it costs nothing.

`get_screening_rules` and `test_screener` now describe `fit_screen`. They previously read the
deleted hard rules, so they answered "what rules does the screener use?" with a rulebook that
had not run in weeks — worse than dead code, because it was confidently wrong.

`report.py` writes a **single self-contained HTML report** next to the workbook, opened by
`open_screening_dashboard`. It replaced `dashboard_server.py`, which was retired for two
reasons: it read Phase1/Phase2/Phase3/Phase4 sheet names the current screener no longer
writes, so it displayed nothing at all; and it ran an HTTP server on a background daemon
thread, the last one in the codebase. A file with its data inlined needs neither, and a
colleague can open it straight from OneDrive.

The report layers for three readers — the decision (three candidates, the money, the county
concentration), then the map and shortlist, then every listing and every assumption. Clicking
anything anywhere opens the same detail view, so there is one place to learn what a property
is rather than four partial ones. Basemap vectors (county, city and road outlines from Census
TIGERweb) are cached per rounded bounding box in the shared folder; aerial photography is
opt-in via `include_imagery` because it costs a couple of minutes.

A CoStar file reaches `screen_listings` one of three ways (see
`mcp_server.py::_resolve_costar_source`): dropped into `data/drop/` (a plain folder —
nothing watches it) or already filed in the document library, searched by filename and
optionally narrowed by `property_name`; pasted directly into the Claude conversation as
`file_content_b64`; or neither — in which case the tool explains how to supply one. The
pre-rebuild `data/watched_folder/` and `data/processed/` trees are still searched last, so
an export already sitting on an existing machine doesn't become invisible after an update.

## Conventions to preserve

- **Secrets never touch `config.py` or git.** All credentials go through
  `confidentials/.env` (gitignored) and are read once in `config.py` via `os.getenv`;
  every other module imports the resulting constant from `config`.
- **`main.py` (non-MCP mode) logs to both file and stdout; MCP mode logs to file only** —
  stdout is reserved for the MCP stdio transport, and any stray print/log to stdout there
  will break the connection to that instance's own Claude Desktop.
- **There are no API keys, and that is worth defending.** Document search is local,
  ranking is arithmetic, ground truth is federal open data, proximity is OpenStreetMap, and
  the qualitative read happens in the Claude conversation that asked for it — already paid
  for. `ANTHROPIC_API_KEY` and `GOOGLE_PLACES_API_KEY` were both removed after their last
  readers went; a blank `confidentials/.env` is a working setup. Adding a key back means
  adding a dependency on someone's billing, so look for the free equivalent first — every
  one of the removed keys had one.
- **A per-call provider failure is not the same as a finding.** `geo_providers` reports
  "provider unreachable" distinctly from "provider says there is nothing there," and the
  prompt formatter renders the difference. Conflating them once made an unconfigured API
  look like "possible landlocked parcel" on every verdict — don't reintroduce that.
- **A fast empty answer is the most dangerous answer.** Measured 2026-07-29: an in-town Phoenix
  listing reported **"0 results found"** because a **Switzerland-only** Overpass mirror answered
  fastest with a confident, structurally valid empty body. Retrying could never have fixed it —
  the mirror simply has no US data. Mirrors are now coverage-probed and quarantined, and an
  empty result is only believed from a mirror known to hold the region. The general rule this
  is the third instance of: **a uniform or empty result across all rows is a broken query until
  proven otherwise.**
- **Two lessons worth more than the code they came from.** (1) An import inside a running
  asyncio loop can hang for minutes on Windows — hence the pandas preload in
  `run_mcp_server()`; a stack dump found it, reasoning never would have. (2) When a tool
  crashes, Claude Desktop tells the user *"the server isn't responding"* — so a crash and a
  hang are indistinguishable from the outside, and every tool must degrade rather than raise.
- **`mcp_server.py` runs no background threads.** The watcher and scheduler threads (and
  the "the scheduler thread must never die" rule that existed to contain them) were removed
  in the rebuild. Don't reintroduce one — use an OS scheduled task on one designated machine.
- **Never widen the corpus scope.** Every corpus path goes through
  `corpus.resolve_in_corpus()`. The folder above `CORPUS_DIR` contains the user's personal
  files, so a path built by string-joining instead is a privacy bug, not a style question.
- **Never read corpus file contents in bulk.** Names are free; bytes download. Anything
  that opens files in a loop over search results will silently pull gigabytes through
  OneDrive. Read one deliberately-chosen file at a time.

## Agent architecture: Skills, Agents, Tools

This project runs on a three-layer separation between probabilistic reasoning and deterministic
execution — worth naming explicitly, since the 2026-07-29 QA-agent work built exactly this
pattern without a name for it at the time. Generic version of this framework calls the layers
"Workflows, Agents, Tools"; mapped onto what actually exists here:

**Layer 1 — Skills (the instructions).** Markdown playbooks in `.claude/skills/*/SKILL.md`. Each
one defines the objective, which tools or subagents to use, the expected output, and how to
handle edge cases, in plain language — the same way you'd brief a colleague. `screening-run`,
`vaulter-screening-pipeline`, `commit_git`, `cleanup`, `recap`, `vaulter-rebuild`, and
`mcp-health-check` are all Layer 1. This is this project's "workflows/" — there is no separate
directory by that name, and one should not be created; the skill *is* the workflow doc.

**Layer 2 — Agents (the decision-maker).** Claude's own role, whether the main session or a
subagent in `.claude/agents/*.md`. Read the relevant skill, run tools/subagents in the correct
sequence, handle failures, ask clarifying questions when needed — connect intent to execution
without trying to do every step in one undifferentiated pass. `vaulter-screening-qa`,
`vaulter-dashboard-qa`, `vaulter-shared-folder-qa`, `vaulter-doc-analyst`,
`vaulter-claim-verifier`, `vaulter-jurisdiction-researcher`, `vaulter-security`, and
`vaulter-mcp-doctor` are all Layer 2.

**Layer 3 — Tools (the execution).** Deterministic Python: `analysis/screening/fit_screen.py`,
`pipeline/proximity_tool.py`, `corpus/`, `scripts/` (including `check_mcp_health.py`, a real-stdio-
subprocess health check for the connector itself), and the `@mcp.tool()` functions in
`mcp_server.py`. Consistent, testable, fast. Credentials live only in `confidentials/.env` — see
"Conventions to preserve" above; this is this project's version of "never store secrets anywhere
else."

**Why this matters:** when a single agent pass tries to handle every step of reasoning directly,
accuracy compounds downward — five steps at 90% each chains down to 59%. Offloading execution to
deterministic code and keeping the agent layer focused on orchestration is why the QA loop caught
real bugs reliably instead of missing them the way an unstructured "does this look okay?" pass
did before it existed.

**How to operate:**
1. **Look for an existing skill, subagent, or tool first.** Check `.claude/skills/`,
   `.claude/agents/`, and `analysis/`/`pipeline/`/`scripts/` before writing new logic. The QA
   subagents exist because nothing did this checking before; don't rebuild what's already there.
2. **When something fails, fix the tool, verify, then record what was learned.** This project's
   regression net for the screener is `scripts/check_screener.py` — run it after any
   `fit_screen.py` change, real file or synthetic. What was learned goes in the `context.md` /
   `memory.md` pair each QA subagent keeps under `docs/agents/<name>/` — that is this project's
   "update the workflow" step, scoped per-agent rather than one shared log.
3. **Keep skills current; don't create or overwrite one without asking**, unless told to
   explicitly — the same rule "Surgical changes" below already states, restated for the skill
   layer specifically, since a skill is a durable instruction set other sessions will follow, not
   disposable scratch.

**The self-improvement loop**, already running: identify what broke → fix the tool → verify (the
regression suite plus a fresh real-world test, not just "it imports") → record what was learned
(a subagent's own memory.md, or a project-type memory entry) → move on with a measurably stronger
system. The 2026-07-29 session is the worked example: a real bug found → `fit_screen.py` fixed →
`check_screener.py` + a fresh-process re-test confirmed it → the fix and the reasoning behind it
recorded in `docs/agents/screening-qa/memory.md` and this file's own history.

**Where things go:**
- **Deliverables** (what a person actually looks at) → `Vaulter AI Shared` (OneDrive), never
  local-only. This project's equivalent of "cloud services" as the deliverable destination.
- **Intermediates** → `data/` subfolders — gitignored, safe to delete and rebuild, the same role
  a generic `.tmp/` would play.
- **Secrets** → `confidentials/.env` only, per "Secrets never touch `config.py` or git" above.

## Working guidelines

Behavioral guidelines to reduce common LLM coding mistakes. These apply alongside the
project-specific instructions above.

**Tradeoff:** these bias toward caution over speed. For trivial tasks, use judgment.

### 1. Think before coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity first

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: every changed line should trace directly to the user's request.

### 4. Goal-driven execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:

```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work")
require constant clarification.

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due
to overcomplication, and clarifying questions come before implementation rather than after
mistakes.

## Rebuild (2026-07)

- **`docs/REBUILD_PLAN.md`** — read §0 first. It records the "no connectors" verdict, what the
  rebuild removed and why, and the measurements behind the corpus design. §§1–4 are **built**;
  §§5–7 (buy-box standard as a document, area intelligence, open questions) are still plan.
- **`docs/COMPANY_PROFILE.md`** — the firm's screening standard, derived from the portfolio and
  deal history. Intended to supersede threshold-based screening: the system should reason about
  *fit* against this profile rather than filtering on numeric cutoffs, because a wrong hard
  filter silently destroys deal flow with no error message. **Draft, unratified — derived from
  documents, confirmed by nobody.** Do not wire any number in it into a hard filter without
  human sign-off.
- **`docs/MULTI_USER_TRANSITION.md`** — historical. Still the best record of *why* the old
  design had the problems it had, but its Priority 0–2 roadmap is superseded: those problems
  lived in code that no longer exists.

Known gap: `read_document` handles PDF/Word/Excel/CSV/text but **not `.msg`**, and the library
holds a great deal of archived correspondence in that format. Adding `extract-msg` would unlock
it — an open decision, see REBUILD_PLAN §7.
