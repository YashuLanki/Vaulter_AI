# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**How to read this file.** It is long on purpose: the explanations are why the
same mistakes have not come back. You rarely need all of it at once.

| If you are… | Read |
|---|---|
| new here | **What this is**, then **Repository layout** |
| running it | **Commands** |
| changing a component | that component under **Architecture** |
| about to ship to a teammate | **There is a live user**, **Hard-won lessons** |
| touching anything | **Conventions to preserve** |
| writing for a person to read | **Working guidelines** §0 |

## What this is

Vaulter AI Property Intelligence System — a Python system for a real estate investment
company that gives each team member searchable access to the firm's document library and
ranks CoStar listings by fit against the existing portfolio, entirely through their own local MCP
server connected to their own Claude Desktop (no separate UI). `system/main.py` is the single CLI
entry point; `system/mcp_server.py` is what actually runs in production, serving MCP tools over
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
pip install -r system/requirements.txt
python system/scripts/setup_wizard.py           # guided setup, ends by building the index

# Everyday
python system/main.py mcp                       # start the MCP server (what Claude Desktop runs)
python system/main.py index-corpus              # (re)build the document-library index (~2 min)
python system/main.py search "closing memo"     # search the library by filename/path
python system/main.py screen CostarExport.xlsx  # rank a CoStar export by portfolio fit (free)
python system/main.py screen export.xlsx 2.5    # ...at a 2.5x MOIC target instead of the 3x default
python system/main.py properties                # list the portfolio from the Project Master
python system/main.py stats                     # what this instance has available

# Checks -- run the one that matches what you touched (see "Three regression suites" below)
python system/scripts/check_screener.py             # 111 checks on the screener's arithmetic
python system/scripts/check_portfolio_comparison.py # 73 checks on the comparison index
python system/scripts/check_answers.py              # 7 checks on the knowledge answers come from
```

There is no lint/test framework configured (no pytest, no linter config) — the three
`check_*.py` suites above are hand-rolled scripts that print PASS/FAIL, and between them they
are the whole regression net. `.claude/hooks/check_python_syntax.py` runs `py_compile` on every
`.py` Claude edits — that hook is the only layer that runs unasked.

## Repository layout

Reorganized 2026-08-03 so the repo has the same shape as the folder a teammate receives —
previously opening the project showed 17 items with nothing indicating where to start:

```
vaulter_ai/
  quick_start/   the double-click installer (the only folder a teammate opens)
  system/        everything the program runs on -- also the only half that ships
  docs/          internal engineering notes; never packaged, never shipped
  .claude/       agents, skills, hooks (developer tooling)
  CLAUDE.md  HISTORY.md  .gitignore
```

`system/` is the boundary that matters: `scripts/build_handoff.py` and `scripts/release.py` both
package from it, so anything outside it is automatically excluded from what teammates receive.
That is load-bearing, not cosmetic — see the auto-update section for the confidentiality hole it
closed. `config.py` derives `BASE_DIR` from its own location, so `system/data/` and
`system/confidentials/` live beside it; paths in `.gitignore` are anchored accordingly.

## Architecture

### Cross-cutting: `system/config.py`
Every path, credential, and tunable constant lives here — this is the only file that
needs to change to port the project to a new machine. It cross-platform-detects Windows
vs Mac, loads `system/confidentials/.env` via `python-dotenv`, and creates all `system/data/` subfolders
on import. Nothing else in the codebase should hardcode a path or read `os.environ`
directly for these values.

**The document library's folder name is not in the code, and is found by shape (2026-08-11).**
It used to be a constant (`CORPUS_SUBFOLDER`) and appeared in ten places in this file's own
module — real SharePoint site detail in a deliberately public repo, the same category as a real
Windows username. It now comes from `VAULTER_CORPUS_SUBFOLDER` in `confidentials/.env` when set,
and otherwise `_find_corpus_subfolder()` detects it: OneDrive names a synced SharePoint library
`<Org> - <Site>`, while every folder it creates for the individual is a plain single name
(`Desktop`, `Documents`, `Pictures`, `Microsoft Teams Chat Files`). So "contains ` - `, is not
`SHARED_SUBFOLDER`, is not a known personal folder" identifies a library without naming one.
Two libraries synced → it **refuses and asks**, rather than guessing; the warning deliberately
does not print the folder names, since those are the detail being protected. This was also the
better design regardless of confidentiality: the exact name was never reliable across machines
(colleagues see different capitalization, confirmed 2026-07-29). `check_portfolio_comparison.py`
§5 covers every shape against throwaway folders.

**How far it looks, and why not further (2026-08-19).** Order: **ask OneDrive's own records**,
then the folder search. The records route is the one that answers "what if the library is somewhere else
entirely — another folder, another drive": OneDrive already knows every library's exact mount
point, so nothing is hunted for, at no filesystem-walking cost. It used to give up unless
`VAULTER_LIBRARY_URL` was set — which many machines don't have, **including the maintainer's own**
— so it now also asks OneDrive's list the same question the disk search asks: which synced library
contains `SHARED_SUBFOLDER`? Checked at each mount and one level inside it (a parent-library sync
mounts the parent). **Deduplicated by resolved path**: OneDrive commonly records both a library and
the account root containing it, so a plain list found the same library twice and then refused it as
"two candidates" — caught on a machine recording three entries for one library.

The folder search itself is breadth-first to `_MAX_LIBRARY_SEARCH_DEPTH` rounds, capped at
`_MAX_LIBRARY_SEARCH_LISTINGS` directory listings, and never descends into a folder that already
matched. Measured reach: **four levels below the OneDrive root** (level 5 is refused, asserted in
§5); 0.1s worst case over a 400-folder tree with no match, 0.001s on the real machine. **Unlimited
depth is not strictly better and was rejected deliberately**: this runs at import time, so its cost
lands on the first tool call of every conversation — the same place 5 seconds was just removed
from — and the library is hundreds of thousands of OneDrive placeholders, so an unbounded walk
would list its whole tree while a whole-drive walk would read the person's private files, the exact
boundary this module exists to hold. `VAULTER_CORPUS_DIR` pins a path by hand for anything stranger.

**§5 stubs `_library_from_onedrive_records` for the folder-shape checks, and covers it separately.**
Without that, every fake-layout check returns THIS machine's real library and passes for the wrong
reason — which is exactly what happened the moment the records route started working without a
configured address.

**The search descends into personal folders, and returning one is still forbidden (2026-08-19).**
The order is: ask OneDrive's own records, then find the folder that CONTAINS `SHARED_SUBFOLDER`,
then look one level down, then fall back to the name shape. That "one level down" pass used to
walk only the folders eligible to *be* a library — which excludes `Desktop`/`Documents`/… — so a
library sitting inside a folder literally named `Documents` was skipped before the search began.
Measured on a second teammate's machine: her layout was **not found** while both previously-fixed
layouts were, which is why this reads as a separate bug from the 2026-08-18 one rather than the
same one recurring. **Descending into a personal folder is not the same as indexing one**, and
only the second is the privacy risk: the search may look inside, but the only thing it can ever
return is a CHILD holding the team folder — a marker this system put there, not an inference about
what a folder contains. The personal-folder exclusion still applies in full to candidates chosen
by NAME, where no such marker exists to lean on. A configured `VAULTER_CORPUS_SUBFOLDER` is now
also looked for one level down, because the handoff package pre-sets that name from the *builder's*
machine — so on a nested layout the name is right and the path is wrong, and that used to report
the named library as absent from a computer that has it.

`SECRETS_DIR` is the project's own `system/confidentials/` folder on every OS. Windows also checks
one legacy hardcoded location (`C:\Users\<USERNAME>\Vaulter AI\confidentials`) as a
fallback, but *only* if that path already has a real `.env` and the project folder doesn't
— so the one pre-existing setup keeps working without being switched out from under it.

Two OneDrive paths are derived from a single detected account root and must not be
confused: `SHARED_DIR` (`Vaulter AI Shared`) is this system's own output, written to;
`CORPUS_DIR` (the firm's own SharePoint library name) is the firm's document library, read-only.

**`SHARED_DIR` now lives INSIDE `CORPUS_DIR` (2026-08-03), and the carve-out is what
makes that safe.** The library is a synced SharePoint library every teammate already has
on disk, so a shared folder placed there reaches everyone automatically — no folder to
share, and no OneDrive "Add shortcut to My files" click, which was the last manual step
in onboarding and the one most likely to be skipped. `corpus/index.py` skips
`Vaulter AI Shared` by name in `_SKIP_DIR_NAMES`, so nothing in it is ever indexed and
screening workbooks can never surface in a document search. **It sits inside the library
on disk but is not part of the document corpus** — this system's own space, walled off.
Verified on a real rebuild: hundreds of thousands of firm documents indexed, zero of our files among them.
Do not remove that skip entry without moving the folder back out. `_detect_shared_dir`
uses the in-library location only if it already exists and never creates it there — one
person sets it up deliberately rather than every install writing into the firm's document
store. So "read-only" still holds for the document corpus itself; the exception is this
one explicitly carved-out folder.
`CORPUS_DIR` is deliberately never `mkdir`'d — if it's missing, that means OneDrive isn't
syncing the library, which `check_system_health` needs to report rather than paper over
with an empty folder.

**Shape of `SHARED_DIR` (restructured 2026-08-03).** It had grown to eight sibling folders
mixing three unrelated things — what you drop in, what you go read, and machinery nobody
should open. Now:

```
Vaulter AI Shared/
  CoStar Drop/           inputs — deliberately at the top level so they stay
  Smartsheet Portfolio/    easy to find and drop files into
  property_summaries/    long-lived team knowledge, not run output
  output/                what a RUN produces, regenerated each time
    proximity/  screening/  screening_decisions/
  system/                machinery; nobody should need to open this
    geo_cache/  org_settings/  updates/
```

**`output/` means "produced by a run" (refined 2026-08-10).** Anything under it can be deleted
and regenerated by re-running what made it. That's the line: `property_summaries/` moved OUT to
the top level because it's curated knowledge that took a reviewed document read to write and
cannot be regenerated — filing it under "output" invited exactly the wrong mental model
(disposable, machine-made) for the most carefully-built thing the team has, and
`_passed-on-deals.md` lives there for the same reason. `screening_decisions/` stays IN, but as a
sibling of `screening/` rather than mixed into it: those notes belong to a specific run, yet must
survive that run being redone. Each notes file is named after the run it belongs to
(`fit_screen_<export>.md` beside `fit_screen_<export>.xlsx`) so the pairing is obvious in a
file listing.

Inputs stay at the top on purpose: burying the drop folder is precisely what drove
teammates to paste files into the conversation instead, at ~43,000 tokens a file. The move
also pulled `geo_cache` into `config.GEO_CACHE_DIR` — it had been hardcoded as
`Path(SHARED_DIR) / "geo_cache"` in three separate modules, against this file's own "nothing
else hardcodes a path" rule, which is why it was the one folder that couldn't be relocated
without hunting down every copy. One known gap remains here, hygiene rather than
correctness: nothing ever cleans up `CoStar Drop/`. (The other — a stale local file
silently shadowing a newer shared one — was fixed 2026-08-03 by searching the shared
folder first; see the screener's file-resolution note below.)

### Data access: the firm's document library (`system/corpus/`)
The firm's SharePoint library is synced to disk by OneDrive at `config.CORPUS_DIR`
(the firm's own OneDrive account, under its own SharePoint library folder name).
`system/corpus/` reads it; nothing writes to it.
Two properties govern every decision in that package:

**Scope is the privacy boundary.** `CORPUS_DIR` is that library specifically, never
the OneDrive account root one level up — the root also holds the individual's own
`Desktop`, `Documents`, and `Microsoft Teams Chat Files`. `corpus.resolve_in_corpus()`
resolves and re-checks every path and raises `OutsideCorpus` on anything that escapes.
**Every new code path that touches a corpus path must go through it** — do not build a
path by string-joining onto `CORPUS_DIR` yourself.

**Search matches names, not contents — and this is load-bearing, not a shortcut.** The
library is hundreds of thousands of files synced as OneDrive Files On-Demand *placeholders*: filenames
are local, file bytes are not, and opening one downloads it. Grepping the corpus would
download the entire library. So `system/corpus/index.py` caches names/sizes/mtimes in a SQLite
index (`system/data/corpus_index.db`, built by `system/main.py index-corpus`) and searches that;
`system/corpus/extract.py` reads content only for one file at a time, deliberately chosen.
If you add a retrieval feature, do not "improve" this into full-text search over the
library without solving hydration first.

The MCP tool descriptions state the names-not-contents limitation explicitly, because the
dangerous failure is Claude concluding "the firm has no records on X" when it only means
"no filename matched X." Preserve that wording.

Ranking uses three signals: all terms must match, a filename hit beats a folder hit, and a
whole-phrase hit beats both. The phrase bonus is not cosmetic — without it, searching a
property named `<Name> 10` ranked the adjacent `<Name> 80` parcel's files first, because `10`
matches inside dates like `20260107`.

**Per-property summaries (`config.PROPERTY_SUMMARIES_DIR`).** Built 2026-07-30 to solve a real
cost, not a hypothetical one: reading a property's documents into a conversation costs real
tokens every time, for every user, regardless of whether the file was already downloaded —
downloading is already free on repeat (OneDrive keeps the hydrated copy), so caching *files*
saves nothing. What actually repeats is re-explaining the same document to Claude. So
`vaulter-document-reader` checks `Vaulter AI Shared/property_summaries/<property-slug>.md` before
reading anything, and writes/updates it after a real read — one property, one team-shared,
cited summary. The first real question about a property pays the full cost once; every later
question, from any user, reads a few hundred tokens instead of tens of thousands. Deliberately
lazy: properties nobody asks about never get a file here, so this never becomes a second copy of
the whole corpus. Each summary stamps the newest source file's mtime it was built from, so a
later check can tell whether new documents have shown up since and the summary might be stale.

### The portfolio (`system/portfolio.py`)
Reads the Smartsheet Project Master export. CSV and .xlsx only — the PDF/OCR parsing path
was dropped in the rebuild. Note only .xlsx can represent a sold deal (strikethrough via
`cell.font.strike`); a CSV export yields every row active and an empty sold list.

**Two locations, local first (2026-08-03).** `_portfolio_dirs()` checks this machine's own
`system/data/project_master/` and then `config.SMARTSHEET_PORTFOLIO_DIR`
(`Vaulter AI Shared/Smartsheet Portfolio`). The shared folder exists because a fresh install
had **no** portfolio data at all: `system/scripts/build_handoff.py` deliberately ships no firm data,
so a new teammate got "Portfolio: unavailable", cities falling back to state names, and
`run_proximity_for_property` refusing every property by name — verified live, not theorised.
Publishing the export to the shared folder once fixes all three for everyone. Local wins so
an existing machine's behaviour is unchanged and a deliberately-placed local file always
beats the team copy; `check_system_health` names which copy it used, because a stale local
file silently beating a fresh team one is the obvious way this goes wrong. The same
local-then-shared lookup covers `property_coordinates.csv` and `builtin_properties.json` —
but `coords_path()` returns the *local* path when neither exists, so a caller writing a new
table never writes into the folder the whole team reads.

`find_project_file()` explicitly skips `property_coordinates.csv` **and**
`builtin_properties.json` — both live alongside the Project Master and neither is one. The
second exclusion was a latent bug: an export whose filename lacked "project"/"master" could
lose the tie-break to `builtin_properties.json` and be silently ignored.

### Proximity (`system/pipeline/proximity_tool.py`)
One Overpass query returns every POI category at once within a radius, classified locally, and
exports CSV + XLSX to the shared folder. It is the only remaining OpenStreetMap consumer —
POI category search has no federal equivalent, so `geo_federal` cannot replace it.

Two entry points. **By name** (`run_proximity_for_property`) looks a portfolio property up in the
hand-verified `property_coordinates.csv` and **refuses** if it has none — do not add a
geocode-the-name fallback, that was measured at 5 wrong out of 8, two in the wrong country, and
it fails silently. **By coordinate** (`run_proximity_for_listing`) takes a rank from the screen
and uses the CoStar export's own coordinates; the refusal does not apply because nothing is being
guessed. Both produce the same format, so a candidate and an owned property compare directly.

**`property_coordinates.csv` records how precisely each point is known, not just where it is
(`precision`: `parcel` / `section` / `intersection` / `city`), because the failure mode here is
silent — a wrong or overstated coordinate points a 5-mile radius search at the wrong place and
nothing about the output looks wrong.** A re-check of all 49 properties on 2026-08-10 found
coverage complete and every point inside its correct state, but ten properties in four groups
were labelled `parcel` while sharing one identical coordinate with another property in the same
group (real groupings in `docs/EVIDENCE_APPENDIX.md`). Checking each group's legal description
confirmed the parcels genuinely sit inside the same PLSS section, so the point itself was fine —
up to ~0.7 miles off, immaterial at a 5-mile radius — but the label overstated what was actually
known. Added `section` as its own precision level rather than silently leaving this as `parcel`,
and `proximity_tool.py` now says so out loud: two properties in the same section return
byte-identical results, and a user comparing them needs to know why.

### MCP server (`system/mcp_server.py`)
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

**30 tools.** Don't maintain this list by hand — it drifted to 19 entries with one duplicated
and two missing. Get the truth from the code:

```bash
python -c "import asyncio; from mcp_server import create_mcp_server; \
  print(sorted(t.name for t in asyncio.run(create_mcp_server().list_tools())))"
```

Grouped by what they're for: **health & updates** — `check_system_health`,
`apply_pending_update`, `apply_pending_settings`, `get_pending_setup_details`,
`get_install_status`.
**Documents** — `search_documents`, `read_document`, `browse_documents`.
**Team knowledge** (shared-folder files, deliberately outside the document index — each of
these tools is the ONLY door to its record; see "Where answers live" in the server's own
instructions) — `get_property_summary`, `update_property_summary`, `get_passed_on_deals`, `get_sold_deals`.
**Portfolio** — `get_property_info`, `get_portfolio_list`, `get_properties_by_stage`,
`open_property_files`, `open_property_document`.
**Screening** — `screen_listings`, `get_screening_rules`,
`test_screener`, `verify_listings`, `open_screening_dashboard`, `open_costar_folder`,
`compare_to_portfolio_history`, `record_screening_decision`, `get_screening_decisions`.
**Proximity** — `run_proximity_for_property`, `run_proximity_for_listing`,
`compare_proximity_to_portfolio`, `open_proximity_files`.

`check_system_health` is called automatically once at the start of every conversation (its
own tool description instructs Claude to do so), stays silent when healthy, and only speaks
up on a real problem — never blocking whatever the user actually asked for. It also runs
the once-daily check for a staged code update or org setting, which used to be a 5am
scheduled job; that is the only piece of the scheduler that survived, and it lives here
precisely so no thread is needed.

**It also checks every property in the current Project Master against `PROPERTY_SUMMARIES_DIR`
(added 2026-08-06),** so a newly-acquired property doesn't sit invisible until someone happens to
ask about it directly. Deliberately detection-only — it never writes a summary itself; whoever
sees the flag decides whether to ask Claude to build one, same reviewed, human-in-the-loop
process every other summary has ever gone through. Matching is substring-based on the fully
normalized (non-alphanumerics stripped) name, not exact — Project Master names often carry a
parenthetical alias or slash-suffix the summary's own filename dropped (e.g. a property name with
a parenthetical alias vs. a summary filename that dropped it), and exact match flagged those as
false positives when tested against the real 49-property list. Known, deliberately accepted
tradeoff: two properties sharing a name stem (e.g. a property name and a longer-named later-phase
sibling sharing the same stem) can mask each other here if only the shorter-named one has a
summary — a false negative, not a false positive, and chosen on
purpose: a wrong "you're missing this" claim damages trust in a tool built to stay silent unless
something is actually wrong, more than an occasional missed detection in one narrow case costs.

**It also flags summaries that have fallen *behind* their documents (added 2026-08-11)** — the
opposite and more insidious case, since such a summary exists, reads as authoritative, and answers
confidently while being months stale. Until now that was only ever noticed if someone happened to
ask about that exact property. Three deliberate constraints, each measured rather than guessed:

* **Active-stage properties only** (`ACTIVE_DEAL_STAGES` = Acquisition, Disposition). Those are
  where money is in motion and dates are running; everything else (Rezone, Pre-Plat, Final
  Engineering, Development, Site Maintenance) is real work on a multi-month clock where a summary
  a few weeks behind rarely changes an answer. Widening it would name 39 of 49 properties every
  conversation, and this check is trusted *because* it stays quiet. Non-active properties still
  get the full on-demand warning whenever someone asks about them by name.
* **It names the newest FILENAME, never a count.** A count cannot be trusted: OneDrive rewrites a
  file's modified-date when it re-syncs, so on one real property **years' worth of older documents
  all looked like they arrived this year** (their own filenames carry the true dates, in a
  `YYMMDD` prefix). A filename lets a reader tell a genuinely new contract from an old file that
  merely got re-synced; a bare "N new documents" is alarming and wrong. Same reasoning
  `_summary_staleness` already gives for naming files instead of counting them.
* **"Couldn't check" is never reported as "nothing new."** `_newer_readable_docs()` returns `None`
  (cannot tell) distinctly from `0` (checked, genuinely nothing), and callers must not conflate
  them — the same rule `geo_providers` follows for "provider unreachable" vs "provider says
  nothing is there." `check_portfolio_comparison.py` §4 asserts this specific collapse can't
  happen.

It also reports, separately, active-stage summaries carrying **no `Source files as of:` stamp at
all** (5 of 14 when built) — those can never be currency-checked, and saying so out loud beats
skipping them silently, which downstream reads as "checked, fine."

**The bug that prompted all of this is worth recording, because no feature would have caught it.**
On 2026-08-11 a currency check was run against a *stale copy of the document list* and reported
"no documents newer than 2026-08-03" as fact. There were 57. The document list itself was eight
days old — the check was answering honestly about a list that was already wrong. The rule this
yields: **never state "nothing new exists" without first confirming the list being read is
current.** A freshness claim inherits the freshness of its source, and this system's own history
says a confident empty answer is the most dangerous answer it can give.

That covers whether the *data behind* a healthy connector is in good shape. Whether the
*connector itself* is reachable and fast is a different failure mode (found 2026-07-30:
`check_system_health` itself hung 60-240+s on a stuck git subprocess — see
`docs/agents/connection-doctor/memory.md` for the full account and fix). `create_mcp_server()`'s own
`instructions=` string tells Claude to invoke the `vaulter-connection-doctor` subagent automatically,
for any teammate, the moment any `vaulter_ai` tool call errors or hangs — no scheduled/background
process involved, consistent with this file's "no background threads" rule; it fires only in
reaction to a real tool-call failure inside an active conversation. `system/scripts/check_mcp_health.py`
is the deterministic check it runs first — it drives a genuine `python system/main.py mcp` subprocess
over real stdio rather than importing `system/mcp_server.py` and calling a tool function in-process,
because the 2026-07-30 hang never reproduced through the in-process shortcut, only through the
real transport.

### Auto-update (`system/scripts/release.py`, `system/scripts/apply_update.py`)

**What the `system/` split means here (2026-08-03).** Both scripts resolve `PROJECT_ROOT` as
`Path(__file__).parent.parent`, which is now `system/` — so a package contains `system/`'s
contents and is applied back into `system/`. Symmetric, so shipping a new MCP tool or any code
change still reaches every teammate normally. Two consequences worth knowing:

* **It closed a real confidentiality hole.** `_iter_package_files()` walks the *filesystem*, not
  `git ls-files`, and `EXCLUDED_DIR_NAMES` never listed `docs` or `.claude`. While `PROJECT_ROOT`
  was the repo root, every update package would therefore have included the gitignored-but-
  present `docs/PORTFOLIO_STANDARD.md`, `docs/COMPANY_PROFILE.md`, `docs/EVIDENCE_APPENDIX.md`,
  `docs/jurisdictions/`, `docs/agents/*/memory.md` **and `.claude/hooks/leak_patterns.txt`** —
  the real-name blocklist itself — and pushed them to every teammate's machine. Verified against
  `git show` of the pre-move script, and verified harmless in practice: `UPDATES_DIR` was empty,
  so no release was ever actually published. Structural fix, not a rule change — those folders
  now simply sit outside the tree being walked. **Don't "simplify" `PROJECT_ROOT` back up a
  level.**
* **`quick_start/` and `.claude/` now DO reach installed machines (fixed 2026-08-19), in their own
  separately-signed package.** They sit beside `system/`, so for months a launcher fix or an agent
  instruction fix could only reach a *new* install via `build_handoff.py` — never an existing one.
  That was measured, not theoretical: on 2026-08-06 a real leaked property-name fragment was fixed
  in `.claude/agents/vaulter-document-reader.md` and pushed through the normal pipeline, and the
  live install's copy simply never changed (confirmed by reading the file, not by trusting the
  update's own success report; patched by hand as a stopgap).

  **A second zip, not extra entries in the main one, and that choice is the load-bearing part.**
  An install running the code of that day copies *every* file in the main package into its program
  folder — so adding `quick_start/` there would have put the installer inside `system/` on a real
  teammate's machine, and the folders that could never reach her still would not have. A separate
  file is simply never looked at by code that doesn't know about it, so an older install is
  genuinely unaffected, and the machinery to receive it ships in the same release. The marker gains
  two keys (`extras_zip_filename`, `extras_signature`) that old readers never ask for. Consequence
  worth stating plainly: **the release that adds this carries the receiving machinery, so the
  launcher/agent files themselves first arrive with the NEXT release.**

  Four rules here:
  - **Signed and verified exactly like the program.** A launcher is code that runs on someone's
    machine; it never gets "it's only the installer" treatment. Verified at staging and again
    immediately before writing.
  - **It fails closed on the extras WITHOUT blocking the code update.** A missing or
    signature-failing launcher package must never hold back a real fix, so the program still
    applies and the skipped extras are stated out loud. "Fail closed" here scopes to those files,
    not the whole update.
  - **Nothing under `quick_start/`/`.claude/` is ever DELETED**, unlike the program folder, which
    is synced to match exactly. These are instructions rather than running code, and someone may
    reasonably have added an agent of their own — throwing that away to remove a stale instruction
    file is the wrong trade.
  - **The receiving end enforces its own allowlist**, because this is the first thing in the
    update path that writes *outside* the program folder. Only `quick_start/` and `.claude/`;
    from `.claude/`, only `.md` (its first run caught a per-machine scheduled-task lock file
    holding a session id); never `.claude/hooks/` (that holds `leak_patterns.txt`, the real-name
    list) and never `settings*.json`. Absolute paths, `..`, and anything resolving outside the
    install root are refused. Both ends enforce this independently, so a hand-made or older
    package cannot bypass it.

Priority 4 in `docs/MULTI_USER_TRANSITION.md`. `system/scripts/release.py` (run by whoever ships a
reviewed fix, never by staff) packages the current code — excluding `system/confidentials/`,
`system/data/`, any virtualenv, and `.git` — into a zip, and publishes it plus a version marker
to `config.UPDATES_DIR` (shared OneDrive). Staged rollout: `python system/scripts/release.py` publishes
to the `canary` channel only; `python system/scripts/release.py --promote` copies that same already-published
version's marker to the `general` channel once it's confirmed healthy. Each instance's
scheduler (`mcp_server.py::_check_and_stage_update`, daily at 5am) reads its own
`config.VAULTER_UPDATE_CHANNEL` (`.env`, defaults to `general`) and, if a newer version is
published there, downloads it into the local `config.PENDING_UPDATE_DIR` — it does **not**
apply it. `check_system_health` surfaces a staged update if one is waiting, and tells Claude
to ask the user whether to apply it now.

**The staging folder keeps only the package it is actually offering
(`_prune_staged_packages`, 2026-08-20).** Applying an update clears the folder, so nothing
accumulates on a machine that stays current — but a machine that is *offered* updates and never
applies them keeps every package it was ever offered, and only one of them is reachable, because
`ready.json` names exactly one. Measured on the maintainer's own development copy: **75 packages,
17 MB**, down to two files and 0.4 MB. The placement is the part worth knowing: a cleanup at the
end of a successful download would have run **never again** on exactly the machine that has the
problem, since such a machine returns early every day at "already downloaded, waiting for a
human". So it is its own step, called on that early-return path as well, and it reads what to
keep from the marker rather than being handed a list — so any future caller gets it right by
construction. It never touches the marker, so the worst case is a folder that stays too big.

**The apply must finish inside a tool call, and once it did not (2026-08-21).** A real apply in
Claude Desktop ran long enough that the tool call timed out. Claude reported the update had
**FAILED** and advised not retrying — while it had in fact **SUCCEEDED**: the version file and the
cleared staging folder both confirmed it. That is the worst of both outcomes, because it sends
someone to fix a machine that is already fine. Two causes, both now fixed:

* **`refresh_dependencies` ran pip on every apply**, on the stated reasoning that "pip skips
  already-satisfied packages quickly, so this is safe and fast to run on every apply". That claim
  was in the docstring and was simply not true in production. It now runs **only when the release
  actually changed `requirements.txt`**, compared before and after the file sync. Almost no
  release changes it, so almost every apply now does no pip work at all: measured **0.4s skipped
  versus 1.8s not**, and both cases are asserted — a release that genuinely changes dependencies
  still installs them, which is the entire reason the step exists. If the old contents cannot be
  read that counts as "cannot tell" and pip runs; never skip on a maybe.
* **CAUSE FOUND by reproduction, after two wrong guesses (2026-08-21).** `refresh_dependencies`
  passed `capture_output=True`, which hands pip two pipes the parent must then drain —
  `subprocess.run` does that inside `communicate()`, using reader threads. **Inside this server's
  event loop those threads crawl.** Traced through the real stdio transport, stage by stage:

  ```
  sync starting → sync FINISHED     under 1 second
  deps starting → deps FINISHED     3 MINUTES 21 SECONDS
  ```

  The same pip call is ~2s from a terminal. Fixed by giving pip a **file** to write to instead of
  pipes: no reader thread exists, so nothing can starve it, and pip's output is still kept for the
  failure message. Re-measured through the real transport with pip forced to run: **1.8s.**

  **This codebase already knew this shape and had already paid for it once.**
  `_get_code_version` runs git on a background thread with its own queue timeout for precisely
  this reason, measured 2026-07-30 — "a stuck git subprocess can make `communicate()`'s internal
  reader-thread `.join()` hang for 60-240+s even with `timeout=5` set". The lesson did not
  generalise from the one call site that had been burned to every other call site with the same
  shape. **Any `subprocess` inside the MCP server that captures output through pipes is suspect;
  prefer a file, or a thread with its own timeout.**

  Two diagnoses were confidently offered and published before this: pip blocking on the inherited
  MCP pipe (measured afterwards against a dead pipe: 1.4s, no difference) and a slow first-time
  import of the signing library (0.08s). Both fitted the symptom. Both were false. The thing that
  actually found it was **stage-by-stage tracing through the real transport** — not reasoning.
  `stdin=subprocess.DEVNULL` stays as hygiene but explains nothing.

**A hang is invisible to error reporting, and that needed its own fix
(`_report_unfinished_apply`, 2026-08-21).** `_report_errors_to_team` finds trouble by scanning the
log for `[ERROR]`, `[CRITICAL]` or a traceback. **A hang writes none of those — it stops writing.**
So the worst failure this update path has actually produced was also the only one nothing could
report, and it was noticed purely because a person sat watching it. On a teammate's machine it
would have been silent. Now `apply_pending_update` writes `APPLY_IN_PROGRESS_FILE` when it starts
and deletes it on success; if it is still there on the next conversation, `check_system_health`
logs a real `[ERROR]` saying an update began and never recorded finishing, then clears the marker
so it reports once and does not nag. It runs **before** `_report_errors_to_team` in that same loop,
or its error would wait a whole conversation to travel. Asserted end to end: simulated hang → error
line → shared folder. The general rule: **when a failure mode produces silence, something has to
turn the silence into a signal — a detector that only recognises error text cannot see it.**

**Applying stays entirely inside the Claude Desktop conversation — no terminal, ever.**
Once the user says yes, Claude calls the `apply_pending_update` MCP tool, which calls
straight into `system/scripts/apply_update.py::apply_pending_update()`: syncs the new version's files into
place, then re-runs `pip install -r system/requirements.txt` with the same interpreter already
running the project (so a fix that adds/changes a dependency doesn't leave the app broken
for want of an uninstalled package), then clears the staging area. `system/scripts/apply_update.py`'s own
`python system/scripts/apply_update.py` CLI entry point (with a y/N prompt) still exists as a manual/
troubleshooting fallback, but is not the expected path. Either way, this first version of
the mechanism is deliberately confirm-then-apply, not fully automatic with zero human
involvement, given the "could break every instance at once" blast radius a bug in auto-apply
would have — the human decision just happens in chat instead of a terminal. The one manual
step that can't be automated at all: fully quitting and reopening Claude Desktop afterward,
since an MCP server can't restart its own parent application.

**Every published package is signed, and every instance verifies before trusting a download
(2026-08-07).** Before this, anyone with write access to the shared OneDrive update folder —
every teammate, by design — could place a zip there and every instance would download and (on
a human "yes") apply it: one compromised account meant arbitrary code execution everywhere. A
hash living in that same writable folder wouldn't have fixed this — an attacker who can write
the zip can just as easily rewrite the hash next to it. The fix is asymmetric:
`system/scripts/release.py` signs the package's SHA-256 digest with an Ed25519 private key that
never leaves the releasing machine and never touches the shared folder
(`system/confidentials/release_signing_key.pem`, gitignored, made once by
`system/scripts/generate_release_key.py`); every instance verifies against the public half
(`system/release_public_key.pem`, tracked — not secret, ships with every install). Verification
happens twice: once in `mcp_server.py::_check_and_stage_update` before a download is ever
written to `ready.json` (so a bad package is never even offered to the user as "ready to
apply"), and again in `apply_update.py::apply_pending_update` right before files actually get
overwritten. Both fail **closed** — a missing public key, a missing signature field, or a
genuine mismatch all refuse rather than silently proceeding, the same rule
`.claude/hooks/check_no_leaks.py` uses when its own name list goes missing. See
`system/core/release_signing.py` for the primitive itself.

**The three pieces are not interchangeable, worth being precise about.** The private key is the
only thing that can *create* a valid signature, and only one machine ever has it. A signature is
not a fixed value either — it's freshly produced from that specific release's own file contents
each time `release.py` runs, so two different releases have two different signatures even though
the same private key made both. The public key does the opposite job from the private key: it
can *check* a signature against the file it claims to belong to, but holding it gives you no way
to produce a new valid signature yourself — which is exactly why it's safe to ship publicly with
every install while the private key never leaves the one machine authorized to publish.

`system/scripts/apply_update.py`'s `PRESERVED_DIR_NAMES` must always match `system/scripts/release.py`'s
`EXCLUDED_DIR_NAMES` exactly — the apply step trusts that anything under those paths was
never in the package to begin with, so it never deletes or overwrites them.

`system/analysis/screening/pipeline.py`'s shared `manifest.json` entries are now stamped with a
`format_version` (`MANIFEST_FORMAT_VERSION`); `_find_cached_result` ignores any entry with a
*higher* format version than this code understands (falls through to a fresh screen) instead
of risking a misread — this is what lets an old and new version of the code share the same
manifest.json without corrupting each other mid-rollout. Bump `MANIFEST_FORMAT_VERSION` only
for a genuinely breaking shape change, not a purely additive one (old readers already ignore
fields they don't look for).

### CoStar Listing Screener (`system/analysis/screening/`)

**`fit_screen.py` is the live screener** and is what the `screen_listings` MCP tool and
`python system/main.py screen` both call. It ranks a CoStar export by **fit against the existing
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
  because in Pinal, small commercial parcels ask many times more per acre than large ones — a
  large spread driven purely by size. A current-use label like Agricultural maps to the
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
  and falls meaningfully with project size, so a percentage of purchase price was the wrong
  *shape*. `lots_per_acre` fell substantially from its old value; nothing supported the old one.
  Carry is charged at a measured tax rate over the *observed* hold, and is a floor.
  Non-residential rows carry no entitlement figure because none exists, so `Cost_Basis` states
  on each that the required exit is understated — uniform treatment, so ranking within a type
  is unaffected.
- **Horizontal development stays out of the arithmetic on purpose.** Measured on a real
  per-acre basis but **only in Pinal County**, and the firm sells entitled rather than improved
  land, so it applies only where the exit comp is improved. Quoted as context on wide-headroom
  rows. Applying a Pinal figure to a Texas listing would be inventing again.
- **The "this ask is huge" caution is measured against the listing's own market
  (`_purchase_reference`, 2026-08-13).** It used to quote one whole-portfolio figure at every
  listing, which is the same mistake `normalise_columns` and the peer-group logic already avoid:
  the portfolio is AZ/CA-heavy, so a global median tells a Texas listing where the firm has
  *operated*, not what a big ask means there. It now quotes the firm's own median purchase **in
  that state** — and only where there are at least `_MIN_STATE_SAMPLE` (5) priced deals to build
  it from. Below that it quotes the firm-wide figure, labels it as firm-wide, and says outright
  how thin the local record is. This threshold is not decoration: as of this writing Colorado and
  New Mexico have **exactly one** priced deal each, and a "median" drawn from a single purchase
  is a number pretending to be evidence — the same failure `_newer_readable_docs` avoids by
  separating "couldn't check" from "nothing there". Informational only, like every other caution:
  `check_screener.py` §19 asserts `Fit_Score` is byte-identical whichever market the reference
  came from, and that two different markets never receive the same sentence. The static
  `large_ask_reference_text` in `cost_assumptions.json` survives purely as the fallback for a
  machine with no comparison index.
- **Never set an asking price against a by-exit cost (2026-08-13).** The report's headline card
  shows what the top three listings would cost to buy, and set it against a static "typical cost
  per asset" range. That was two separate errors in the page's most prominent number. It was
  **never a range** — the two figures measure different things: the corporate deck's *average
  invested capital per project* (purchase **plus** entitlement spend, carry and taxes across the
  hold) and a second internal figure whose measure was never established. Printing them with a
  dash between implied a careful estimate where there was an open question. And an acquisition
  total is an **entry** figure, so comparing it to a **by-exit** figure made every shortlist read
  roughly four times cheaper against the firm than it was. `typical_purchase_millions` /
  `typical_purchase_n` are now **derived at run time** from the comparison index (so nothing real
  is stored in tracked code and the figure can't go stale), `avg_invested_capital_millions` is
  quoted as its own separate sentence, and the unexplained lower figure is withdrawn into
  `cost_assumptions.json` under `withdrawn_avg_asset_value_*` so the question survives for a
  partner. `check_screener.py` §20 asserts the two kinds of figure stay apart and that a machine
  with no deal record says less rather than printing a zero. Same family as the market-aware
  caution above: **before comparing two numbers, confirm they measure the same thing.**
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

**`cost_assumptions.json` is read local-then-shared (2026-08-13)** — this machine's
`system/data/`, then `config.ORG_SETTINGS_DIR` in the team folder — the same two-location pattern
`portfolio.py::_portfolio_dirs()` uses, and for a measured reason. A fresh-install test found that
a teammate with no cost record **scored 170 of 216 rows differently and shared only 3 of the top
10** with the maintainer's machine: without measured entitlement costs the screen cannot work out
what a deal must sell for, so it ranks on less information. Both runs were honest — every affected
row says `entitlement cost not included — no record for this type` — but nobody comparing two
shortlists would have guessed that was the reason they disagreed. Publishing the file once makes
every instance agree (verified: 0 of 216 rows differ). Local still wins, so a deliberately-placed
local file beats the team copy. `check_screener.py` §21 asserts the published copy exists and
matches. The file itself stays gitignored — it is published to OneDrive, never to this public repo.

Every tunable lives in `ASSUMPTIONS` at the top of the module, deliberately in one place so a
partner can argue with it. Each now carries its source. **The four `WEIGHTS` are the only
numbers left with no evidence at all** — two document searches found nothing in the corpus that
ranks or weights selection factors. They need a senior partner's judgment, not another search.
(Real names and figures behind every genericized citation in this file live in
`docs/EVIDENCE_APPENDIX.md`, local-only — this repo is deliberately public.)

`system/scripts/check_screener.py` runs **111 checks** across deformed market shapes. Run it after
any change to `fit_screen.py`. Note it covers the screener only — **`geo_providers.py` has no
automated coverage at all**, and that is where the worst measured bug of 2026-07-29 lived (see
the proximity note below). It is one of three suites: `check_portfolio_comparison.py` (73 checks)
covers the comparison index, and `check_answers.py` (7) covers the shared knowledge answers are
built from — see "Three regression suites" below for what each one can and cannot catch.

#### What was removed with it
`pipeline.py`, `phase1_rules.py`, `phase2_ranking.py`, `phase3_deep_analysis.py`,
`phase4_verification.py`, `workbook_builder.py`, `scoring_config.py`, `market_utils.py` and the
screening-local `system/config.py` are all deleted — about 2,500 lines. They were reachable from
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
`mcp_server.py::_resolve_costar_source`): by filename, searched in
`config.COSTAR_DROP_DIR` (`Vaulter AI Shared/CoStar Drop`) and the local
`system/data/drop/` — both plain folders, nothing watches either — then in the document
library, optionally narrowed by `property_name`; pasted directly into the Claude
conversation as `file_content_b64`; or neither — in which case the tool explains how to
supply one. The

**These are not equally cheap, and the tool descriptions now say so.** Measured 2026-08-03
on the real exports: passing a 216-row file as `file_content_b64` costs **~43,000 tokens
purely to transfer it**, before a single listing is read; by filename it costs nothing,
because the file never enters the conversation. This was found in live use — a teammate
attached exports to the chat and Claude dutifully base64'd each one in a loop. The shared
`CoStar Drop` folder exists because the local one sat at `<install>/system/data/drop`,
where no non-technical person will ever navigate; `open_costar_folder` opens the shared
one. **The shared folder is searched FIRST (changed 2026-08-03)** — it is the team's
source of truth, and searching local first meant a leftover copy on one machine silently
shadowed a newer export the team had just published: same filename, older data, no
warning, every downstream number wrong. Local remains the fallback, so a local-only file
still resolves and a pasted file still lands locally — one person's paste shouldn't appear
in the team's folder. Which folder a file came from is logged on every resolve.

pre-rebuild `system/data/watched_folder/` and `system/data/processed/` trees are still searched last, so
an export already sitting on an existing machine doesn't become invisible after an update.

### Portfolio comparison (`system/analysis/screening/portfolio_comparison.py`)

Built 2026-08-06 to answer a specific ask from a team meeting: "name the projects that are
similar, how did we approach those, and can that method be applied to this listing." Deliberately
**characteristics-only** — location, land type, plan type (rezone/subdivide/entitle-only/annex/
acquire-finished-lots/hold-only/assemble-resell/recapitalization), and size. It never compares price and never issues a
pursue/don't-pursue verdict; both of those need either a human or the still-open peer-pricing
decision for standalone properties (on hold as of this writing). What it returns: the 3-5 most
similar past deals, why each matched, what actually happened (still held / sold / pending), and —
via `market_eras.py`'s hand-authored, publicly-sourced timeline — what broader market conditions
each was bought into, so a person can judge whether that playbook still applies today.

**`acquire-finished-lots` exists because the taxonomy was hiding the firm's own best pattern
(added 2026-08-10).** A blind re-read of source documents found properties filed as `hold-only`
that had actually been bought as already-platted or finished lots — the firm did no entitlement
work because none was needed, and the value-add was the acquisition itself (price, timing, a
distressed seller). Filed under a label that reads as "no plan," that pattern was invisible: ask
"have we done a distressed finished-lot package before?" and the system answered with deals where
the firm apparently did nothing. `hold-only` is deliberately kept for a genuine buy-raw-and-sit
case — as of this writing no property in the portfolio is one.

**A correction, and the reason the provenance field exists (2026-08-11).** The original version
of this note claimed **every** former `hold-only` property had been bought as finished lots. That
generalisation was made from a sample and was not safe: reading one parcel's own due-diligence
report, filed three weeks before its closing, found *raw land with an approved-but-unrecorded,
since-expired plat requiring resubmission and re-engineering* — the opposite of finished lots.
That parcel is now `entitle-only`, cited. The label had been applied from the property's own
summary and was correctly carrying `plan_type_source: summary`, so the system was already
rendering it as unconfirmed rather than fact — which is exactly what that field is for, working
on its first real test.

**All eight were then re-checked against source documents, and the generalisation failed for
three of them — 5 confirmed, 3 wrong.** Every one of the three is part of the same
multi-parcel project family, and their failure modes differ: one was raw land whose plat was
drafted but never executed (the seller's own file names it `...UNRECORDED.pdf`, signature blocks
blank); one was raw land where **no plat, current or expired, was ever found** — its "271 platted
& engineered lots" traces to an internal broker memo built on a *conceptual lot grid drawn over an
aerial photo*, not a recorded instrument; the third is the parcel above. The five that held up are
evidenced about as well as this question can be: an engineer's report dated the closing day, an
issued owner's title policy, a deed listing 53 individual lot numbers against a 2005 plat, and one
property corroborated by five independent primary sources including the city's own acceptance of
public improvements.

The lesson is not "the label was wrong" but **where the label came from**. Every confirmation
rests on a recorded instrument or a professional's contemporaneous report; every failure rests on
marketing or broker language that had propagated into the record unchallenged. When classifying a
deal, weight a recorded plat, title policy, ALTA survey or Phase I over any internal memo
describing what land *will be* — and treat a lot count with no recorded instrument behind it as a
claim, not a fact.

**Extended to the whole portfolio (2026-08-11).** Every one of the 49 records is now provenance-
tagged, and the 31 that carried no source were re-read from documents the same way: 20 confirmed,
7 corrected, 2 correctly left as-is (one genuinely `unclear` — a pre-LOI deal naming Vaulter in
no document reviewed — one where the only candidate source document was confirmed to be about a
*different* deal and the reader wrote nothing rather than force a match). **48 of 49 classifications
now rest on a document; one still rests on its own summary.** Two corrections were substantial
enough to change the firm's own stated value-add mechanism for that deal, not just its label —
in both cases, recorded plats showed most of the lot inventory had already been platted by a
*prior* owner, with the firm's real work being infrastructure entitlement on the remainder, not
the ground-up subdivision its label implied. Where a reader found text asserting something a
recorded instrument disproved, it deleted that text in place and replaced it with the correct fact
and a citation — never left both standing side by side, except in the small number of cases where
two real sources genuinely disagree and neither is shown wrong (those stay visible, flagged, for a
human to adjudicate). Full findings, corrections and citations live in each property's own summary
in `Vaulter AI Shared/property_summaries/`, dated 2026-08-11.

That same re-read corrected three labels that had captured a **corporate event instead of the
land activity** — an entity consolidation and a refinancing were both recorded as the "approach"
while the land underneath was being subdivided — and stood one brand-new acquisition down to
`unclear` rather than assert a strategy its own operating agreement says hasn't been approved
yet. The measured lesson: classifications carrying **no source citation were wrong 2 times in 3**,
against 1 in 8 for cited ones. Treat an uncited `plan_type` as a hypothesis.

**The comparison index is agent-curated, not deterministically derived, and that's deliberate.**
Every property now has an `## Approach & Outcome` section, but classifying a deal's land type or
plan type from free-text prose is a judgment call a regex can't reliably make — the same reason
`fit_screen.py`'s own `normalise_columns()` needs pattern-plus-value-check rather than name alone.
So the index (`system/data/portfolio_comparison_index.json` — gitignored, real firm data) is built
by having an agent read each summary and tag it against a fixed category list, refreshed whenever
new summaries are added or backfilled. Everything downstream of that index — the scoring in
`find_similar_deals()` — is ordinary deterministic Python, with its own `ASSUMPTIONS` dict in the
same spirit as `fit_screen.py`'s: reasonable defaults, not measured results, meant to be argued
with. `system/scripts/check_portfolio_comparison.py` is its regression suite — run it after any
change to either file. One calibration bug it caught before shipping: a single soft signal (a
shared plan-type label plus a loosely similar size band, with no location or land-type match at
all) scored just high enough to look like a real match against a deliberately unrelated test deal
— the reporting threshold was raised specifically to require at least one real anchor (state or
land type), not just a shared strategy label.

Exposed two ways: the standalone `compare_to_portfolio_history` MCP tool, and — wired in
2026-08-06 — automatically for every row of every `screen_listings` run, via
`add_portfolio_comparison()` in `fit_screen.py` and a `Portfolio_Comparison` column that flows
into both the workbook and the HTML report's per-listing detail view ("Similar to the firm's own
history"). A listing's own free-text land-use column is classified into the fixed vocabulary by
`classify_land_type()`, reusing `fit_screen.py`'s own pattern families for consistency (with
mixed-use broken out as its own category, since `fit_screen.py` folds it into commercial for
*pricing* purposes — correct there, but it would have hidden every real mixed-use deal in the
portfolio from ever matching a mixed-use listing here). No `plan_type` is passed for an incoming
listing — the firm hasn't decided an approach yet for something it doesn't own, and guessing one
would misrepresent an unmade decision as a known fact.

`market_eras.py`'s timeline is intentionally separate from the cited, per-deal facts in the index:
it's general public-record economic history (recession dates, Fed rate-cycle turns), never a claim
about a specific deal, so mixing the two would blur the citation discipline that's kept these
summaries trustworthy.

Wiring this into a real 216-row export caught a real bug on the first run: `_size_band` crashed
with `StopIteration` on any row with a blank acreage. `float("nan")` passes Python's own `float()`
conversion without raising, but `NaN` compares `False` against every band limit including infinity,
so the "pick the first band it's under" logic found nothing. Not a rare shape — a real CoStar
export routinely has rows with no `Land Area (AC)` value. Fixed by checking for `NaN` explicitly
before banding; `check_screener.py`'s own section 14 and `check_portfolio_comparison.py` both now
assert this specific case doesn't crash.

**Three fields were added 2026-08-11 to stop the index stating more than it knows.**
`plan_type_source` records how each classification was arrived at — `documents` (independently
re-read from source), `summary` (taken from the property's own write-up), or `unrecorded` (never
written down). This is not bookkeeping: the blind re-read measured `unrecorded` classifications
wrong **2 times in 3**, against 1 in 8 for cited ones, so an unrecorded one now renders as
`[unconfirmed]` everywhere and `compare_to_portfolio_history` says outright to confirm it before
relying on it. `disposition_detail` splits `still-held` — which covered "never marketed",
"marketed for years, no buyer", and "capital already returned" under one label — but **only where
the property's own note evidences which**; 10 of 38 as of this writing, and the other 28 stay
plain rather than being assigned a story.

**A deliberate backfill attempt on 2026-08-14 recovered 3 of 19, and the low yield is the finding.**
Every still-held property bought 2016 or earlier without a recorded reason had its summary re-read
for evidence of *why* it is still held. Three were plain enough to record: one whose summary states
outright that it was "listed for sale as raw/partially-entitled land multiple times without a
completed transaction"; one whose Disposition/Offers, /Marketing and /CTC folders were verified
empty by folder listing on two separate dates; and one with a named broker's active listing and a
stated asking price. **Sixteen stayed unrecorded**, because their summaries simply do not say —
the question was never asked when they were written, so the documents behind them were read for
what the deal *was*, not for what happened to it since. Two candidates were deliberately rejected
rather than stretched: one with an executed 2026 LOI on a parcel (marketed and selling, not
marketed and stuck), and several whose only evidence was a folder name nobody had opened. New
entries carry `disposition_source` and a `disposition_source_note`; the original 10 predate those
fields. **Do not re-run this against the summaries expecting a better result — the ceiling is what
the summaries record. Raising it means re-reading source documents with this specific question in
hand, which is a documents-desk pass, not a re-parse.**

And `pipeline/property_registry.py` gives every property
a durable internal ID (never shown to a user) with every observed spelling recorded as an alias,
because the same property is named four different ways across the four files describing it —
harmless only while nothing joins them by name. Its first build merged a project with its
later phase (48 IDs for 49 properties), so the canonical source now matches **exactly**, never by
substring; one alias whose extra words sit mid-name can't be matched by any rule and is recorded
by hand, which is the argument for a registry over re-deriving the link each run.

**`looks_like_finished_lots()` is the one exception to "never pass a plan_type for a listing".**
Land already platted is a *fact about the asset*, not a guess at what the firm would do with it,
and without it the eight `acquire-finished-lots` deals — the best-documented profitable pattern in
the portfolio — could never surface as precedent for the one kind of listing they apply to. It
fires only on explicit language, never a bare "lot". The real 216-row export carries no platting
language in any column, so it is **dormant there and correctly changes nothing**;
`check_screener.py` §17 proves on synthetic platted data that it changes which deals are cited
while leaving `Fit_Score` and `Fit_Tier` byte-identical.

**`summarize_match()` renders one compact line per matched deal for the `Portfolio_Comparison`
column and the report's detail view — approach, outcome, a verification flag, and the shortest
useful slice of the note.** Before 2026-08-10 the column was just `"<name> (<outcome>)"` repeated
for each match — three names and the same two words, with no way to tell an entitlement play from
a finished-lot purchase even though those imply opposite lessons for the listing in front of the
reader. `find_similar_deals()` did not even return `plan_type` on a match, so the approach was
invisible to every caller, not just this one. It now reads (real example in
`docs/EVIDENCE_APPENDIX.md`) `"<name> — bought already-finished lots, still held [verified]:
13-year hold against a 3-4yr plan..."`. The `[verified]` flag surfaces the 2026-08-10
blind-verification markers (see above) so a reader can tell independently-confirmed history from
a summary's own wording. Prices are stripped defensively with a regex guard before the string is
built — one existing note mentions a sale figure, and `check_screener.py` asserts price never
reaches this column, since the tool compares characteristics and history, never price.

### Jurisdiction dossiers (`system/analysis/screening/jurisdiction_notes.py`) — 2026-08-12

**Found by auditing what the screener actually opens, rather than what the comments mention.**
A 9,000-character researched dossier for one Arizona city had sat in `docs/jurisdictions/` for
weeks, read by **zero** code — with a section literally headed "What this changes about
screening <city> listings". `docs/` is also never shipped, so it could never have reached a
teammate even in principle. This module is the wire that was missing.

It answers the one question nothing else in the screen can: **is this jurisdiction going
anywhere?** Water and sewer capacity, impact fees, annexation posture and capital plans decide
whether an entitlement play is possible at all, and a CoStar export says none of it.

* **Dossiers moved to `SHARED_DIR/jurisdictions/<city>-<state>.md`**, beside the property
  summaries, so the whole team has the same research — the same reasoning that put summaries
  there rather than in the repo.
* **`Jurisdiction_Note` never touches `Fit_Score` or `Fit_Tier`.** Same rule as `Cautions` and
  `Portfolio_Comparison`: a dossier is prose a human wrote, and letting prose move a score turns
  research into arithmetic. `check_screener.py` §18 proves the score is byte-identical with the
  dossier folder present and moved away.
* **It quotes the dossier's own screening section rather than summarising the whole thing** — a
  summary of research is a new claim, and nothing here invents a signal. A dossier with no such
  section contributes nothing, which is honest: background research that never reached a
  conclusion should not sit beside a ranking as though it had.
* **Silence where there is no dossier.** The real 216-row export spans 30 cities; one dossier
  covered 11 listings. An empty string, never a placeholder implying the city was assessed.

**One trap worth recording, because it looked right and was dangerous.** Matching a state code
to a spelled-out name by prefix is wrong: `"arizona"` starts with `"ar"`, which is **Arkansas**.
An earlier version also fell back to "the only dossier with this city name" and handed Arizona's
water findings to a listing in Coolidge, *Texas* — same-named towns exist in several states.
Both are now an explicit code lookup, and both are asserted in §18.

### Sold-deal precedent (`_sold-deals.md`, `get_sold_deals`) — 2026-08-12

The mirror image of `_passed-on-deals.md`, and requested straight out of a team meeting: the
firm's **completed round trips** are the only place the whole argument was tested end to end —
entry basis, plan, execution and buyer. A still-held property tells you what was *intended*; a
sold one tells you what actually cleared. Four deals as of this writing (two AZ, one CA, one TX),
plus four more under contract to sell that will materially reshape it when they close.

Hand-written from the four properties' own cited summaries, in the same spirit as
`passed_on_patterns.py`'s `KNOWN_PATTERNS`: **never auto-derived**, and it feeds nothing. It is
read in a conversation, exactly like `get_passed_on_deals`. The file's own opening rule is
"precedent, never a formula" — the same discipline the passed-on record carries, in the opposite
direction, and for the same measured reason (the hard-filter incident that discarded 60 of 69
real listings).

**Exit figures were added to the comparison index 2026-08-12**, from each property's own
settlement statements: `exit_price_usd`, `exit_year`, `hold_years`, `exit_form` and
`gross_price_multiple`. The field is named **gross price multiple, never a return** —
entitlement spend, carry and taxes are not deducted, and one property's own summary states that
its actual re-entitlement costs across a 15-year hold were never established. A deal whose entry
basis is genuinely unknown (one is a carve-out of a legacy position) gets **no** multiple rather
than a fabricated one. `check_portfolio_comparison.py` §4a asserts all of that.

**What the numbers show, and it is why they were worth extracting.** The three exits with a known
basis came in at roughly **2.9x over 2 years**, **13x over 15 years**, and **1.76x over 10
years**. That last is about **6% a year gross, before any cost is deducted at all** — after
entitlement spend and carry it may not be a profit in real terms. **So a completed sale is
evidence that an exit was achievable, not evidence that the deal worked.**

That distinction is the answer to "should sold deals boost the ranking?" — asked directly
2026-08-12. Boosting "resembles a sold deal" would promote listings that look like the *weakest*
outcome in the record, because the record cannot currently tell a good exit from a bail-out. The
best result here came from selling early and abandoning the original plan. Note also the sample
asymmetry: **4 completed exits against 28 properties still held 10+ years**, five of them bought
1999–2003. The better-evidenced signal is the inverse — a listing resembling something the firm
has been stuck in — and that too belongs in `Cautions`, not the score. (Real names and figures:
`docs/EVIDENCE_APPENDIX.md`, gitignored.)

Three findings from it worth knowing, because they are the firm's own record rather than
anyone's assumption: **every one of the four exited by selling the entitled position — none was
built out**, confirming the stated model four times over; **three of four had a distressed or
discounted basis** (an expired plat, a seller needing speed, a bank disposing of REO), so the
edge was usually bought rather than created; and **holds ran long against plan** (3–4 years
underwritten → ~10 actual on one; a ~5-year resale expectation → ~15 on another). The one deal
that beat its schedule did so by abandoning the plan and selling early. The file states its own
survivorship problem out loud: these are the deals that closed, while 21 properties bought
2011–2015 are still held, so the completed set is the faster half of a slower portfolio by
construction.

### Passed-on-deal patterns (`system/analysis/screening/passed_on_patterns.py`)

Surfaces a documented pattern from `_passed-on-deals.md` (the firm's own passed-on/lost-deal
history) as a **caution** on a matching new listing — informational only, wired into
`fit_screen.py`'s existing `add_cautions()`, never a score or rank change. Built 2026-08-06 after
a direct question in a team conversation: should this history become a rule the screener applies?
No — for the same reason `screen_listings` never eliminates anything: a hard filter built from
just three rejection grounds once threw out 60 of 69 real listings on grounds that weren't real
dealbreakers, and most of `_passed-on-deals.md`'s own causation is honestly uncertain (locked in
unreadable archived email). But a caution — the same non-eliminating mechanism flood risk and an
oversized ask already use — is safe, so that's what this is.

**`KNOWN_PATTERNS` is a short, hand-curated table, deliberately not a miner over the whole
passed-on-deals file**, in the same spirit as `fit_screen.py`'s own `ASSUMPTIONS`/`WEIGHTS`: add
to it by hand when real, multi-deal, documented evidence exists — never auto-derive a pattern from
prose whose own causation is uncertain, which would manufacture exactly the kind of unverified
signal this project has repeatedly measured and removed elsewhere (a mirror that answered a flood
question with a confident, wrong empty result; the hard-filter incident above). The one pattern in
the table as of this writing: multiple of Colorado's documented Weld County dead deals cite
active or legacy oil & gas wells, leases, or contamination as a real complication — worth a caution
on a new Weld County listing even though no single termination notice states it as the cause.

Verified with a mathematical proof, not just a manual check: ran the real 216-row export twice,
identical except one copy's `County Name` was set to "Weld" and the other to a different Colorado
county, and asserted `Fit_Score` and `Fit_Tier` come back **byte-for-byte identical** between the
two — the caution genuinely cannot influence ranking, because `add_cautions()` runs into its own
`Cautions` column and the score is computed only from `_proximity_score`/`_pricing_score`/
`_distress_score`/`_size_score`, which never read it.

### Screening decisions (`record_screening_decision` / `get_screening_decisions`)

Built 2026-08-10 to close the one feedback loop this system has never had: **the screener's
ranking has never been checked against what the firm actually chose to do.** A partner says
"pursue that one anyway, the seller's motivated" in a meeting, and that judgment — the most
valuable signal available about what the firm really values — evaporates. `REBUILD_PLAN.md` §4's
own note calls logging human overrides the highest-compounding accuracy gain available over a
year of use.

One markdown file per screening run, in `config.SCREENING_DECISIONS_DIR`
(`Vaulter AI Shared/output/screening_decisions/`), named to match that run's workbook:
`fit_screen_<export>.md` beside `fit_screen_<export>.xlsx`. **Its own folder, not alongside the
workbooks, and that separation is the point** — a re-screen regenerates the workbook, and human
judgment must never be overwritten by a machine re-run. Append-only with a date and (optional)
name on every entry, same pattern as `update_property_summary`: nothing is ever edited or
deleted, so the worst case is an extra line a human removes.

**It is a diary, not a dial.** Nothing here feeds back into scoring, ever — same rule
`_passed-on-deals.md` carries, and for the same measured reason (the hard-filter incident that
threw out 60 of 69 real listings). If a pattern eventually emerges in these notes, a *human*
decides whether the screener should change. The server's own instructions tell Claude to offer
to save a decision when the user states one, once, without nagging.

### Who has it installed (`get_install_status`) — 2026-08-19

Built to answer a question nothing could answer before: **who has Vaulter AI, what version are
they running, and does anything need fixing on their machine?** Until this existed the only way
to know was to read a file on that person's own computer. The prompting case was concrete — the
maintainer's own install sat **17 versions behind for six days**, invisible, because nothing had
launched it since the release.

Each install leaves one small note in `config.INSTALLS_DIR`
(`Vaulter AI Shared/system/installs/<user>--<machine>--<folder fingerprint>.json`): version,
channel, last seen, and whether the library, shared folder, portfolio and file index are in good
shape, plus any update downloaded-but-not-applied. `get_install_status` reads them all and
reports in the conversation.

Five things here are load-bearing:

* **Written from `check_system_health`, never a background process** — that is already the
  once-per-conversation shared-folder visit, and this project does not run threads.
* **Gated to once a day, and the gate must not need the version.** Checking in every
  conversation added **5 seconds to the first tool call of every conversation**. The 5 seconds
  was not the shared write (0.05s warm) — it was `_get_code_version()` falling through to a
  `git` call that times out whenever no `VERSION` file is present. An earlier draft compared
  versions inside the gate and therefore saved **nothing**; measured, not assumed. Daily costs
  no usefulness, since the page reports "last used" in whole days. `apply_pending_update`
  deletes the stamp so a freshly-updated install reports its new version immediately rather
  than tomorrow.
* **The filename needs all three parts.** Account plus computer alone silently merges two
  installs on one machine into one entry — not hypothetical, that is exactly the maintainer's
  working install plus development copy, and whichever ran last would erase the other.
* **A missing field means "could not check", never "fine".** `_install_problems()` reports only
  what a record positively states. Same rule as `_newer_readable_docs`' "couldn't check ≠
  nothing new".
* **There is deliberately no HTML page (removed 2026-08-19, hours after being added).** It was a
  file, so it only refreshed when someone asked for it — and asking already produces the current
  answer in the conversation. So the page could never be fresher than the request that made it,
  which for a feature whose whole job is spotting stale machines is the wrong failure to build in.
  Deleted rather than maintained. If a visual view is ever wanted again, generate it on demand and
  do not leave it lying in the shared folder implying it is live.
* **This list is never a complete roster, and both tools say so.** Someone appears only after
  they install the version that added this, and their entry only refreshes when they open a
  conversation. A stale "last used" is the signal, not a defect — it means that person has not
  picked up recent fixes. Reading it as "everyone who has it" would be exactly the confident
  empty answer this project distrusts everywhere else.

Records are read through `_json_object()`, so a wrong-shaped file in that
every-teammate-can-write-to folder is skipped rather than crashing the tool. A record stamped
with a **newer** `format_version` is still shown, reading only known fields — the opposite of the
screening manifest's ignore-newer rule, and deliberately: hiding a teammate is the failure this
feature exists to fix.

### The morning round (`system/scripts/team_status.py`, `daily_round.cmd`) — 2026-08-20

A Windows scheduled task at 8am that checks every teammate's install and writes a
plain-English briefing to `SHARED_DIR/system/daily_status/` (`latest.md` plus a dated copy).
Asked for directly: an assistant that reports each morning on who has it, what state they are
in, whether answers are still right, and whether anything is completely dead — rather than
those questions being asked only when something already went wrong.

**Two layers, and the split is the design.** `team_status.py` gathers the countable facts —
versions, dates, error counts, whether a check passed — deterministically, free, and it cannot
get a number wrong. Claude then reads those facts and writes the briefing, which is the
judgement a script cannot make ("she is quiet AND two versions behind AND reported an error, so
she is worth a nudge"). If the model layer fails entirely the facts are still saved to
`system/data/logs/daily_round_facts.txt`, so the morning is never a total blank. The collector
is also fine to run by hand any time: `python system/scripts/team_status.py`.

**A scheduled task, because this project runs no background threads** — the same rule the MCP
server follows, and the same mechanism the nightly file-list refresh already used.

**It runs at 9am, not 8am, and the reason is the machine's sleep schedule (2026-08-21).** The
intent was an hour after the 7am file-list refresh, so the round reads a list rebuilt that
morning. On the first real morning neither task ran on time: this machine sleeps overnight and
woke at **08:31**, `WakeToRun` is off, so both slots passed while it was asleep. Windows' catch-up
then fired **both at 08:34, in the same minute** — which quietly destroyed the ordering the two
times existed to create. 9am is after the observed wake, so the round runs on time and the refresh
has finished. Two things make this a mild bug rather than a serious one, and both are worth
knowing before anyone "simplifies" them away:

* **`build_index` builds into a temporary database and swaps it in at the end**, so a reader can
  never see a half-built list — it gets yesterday's complete one or today's, never a partial
  count. That is what stops a race here from producing a wrong number.
* **The round states the list's age every day**, so reading yesterday's list is visible rather
  than silent. The scheduling fix makes the common case right; that report is what makes the
  uncommon case honest.

**Task Scheduler history is disabled on this machine and could not be enabled without admin**, so
a skipped run leaves no trace in the event log. This is the same blind spot as the refresh that
reported success for weeks while its program did not exist: **the only trustworthy evidence about
a scheduled job is the artifact it produces** — the briefing's own timestamp, and the file list's
own timestamp. Check those, never `LastTaskResult`.

Four things here are load-bearing:

* **`ANTHROPIC_API_KEY` is cleared for the run.** When set it takes precedence over the
  signed-in Claude account, and on this machine that key has no credit — so the round would
  fail with "credit balance too low" while looking like a scheduling problem. Measured, not
  guessed.
* **The prompt forbids inventing a cause**, and the first real run honoured it: faced with a
  check that had started failing, it wrote *"I could not establish why the number rose — that
  needs a person"* rather than supplying a plausible reason. Same rule as
  `_newer_readable_docs`' "couldn't check ≠ nothing new"; the difference is that here it is
  enforced in a prompt rather than in code, so it is worded as a rule and not a preference.
* **It is read-only and says so twice** — in the tool allowlist and in its own prompt. It never
  applies an update, publishes anything, or edits a summary. Anything needing action goes in a
  "what I would do next" list for a person. An agent that fixed things unattended on a machine
  every teammate's install syncs to is a blast radius nobody asked for.
* **It states the AGE of the file list every day, and checks it directly.** Registering this
  found that the 7am refresh had been pointing at `%TEMP%ealinstall_.../Vaulter AI` — a
  throwaway folder from an install test, deleted weeks earlier — so **it had been failing
  silently every night**, while Windows recorded `LastTaskResult: 0` because the `pythonw.exe`
  it was told to run did not exist to fail. Nothing in the system noticed, and a hand-run
  rebuild was the only thing keeping the list current. That is the precise setup behind the
  2026-08-11 wrong answer ("no documents newer than 2026-08-03" — there were 57), so
  `_file_list_age()` reads the list's own timestamp and never infers freshness from whether the
  refresh reported success. **The only trustworthy evidence about a file list is the file list.**

**Nothing this routine writes grows for ever (`tidy_up`, 2026-08-20).** Three files would have.
The team folder gains one dated briefing a day (`latest.md` is always current, so the dated ones
are only for looking back — 30 days kept); the local run log gained a few lines every morning
with nothing ever removing them; and the per-machine error report is capped, but the cap
**trimmed the front of the whole file**, which took the header with it. That header is the only
thing saying whose computer the file describes, which is the entire point of it — and the cut
landed mid-entry, leaving the oldest one half-written. It now holds the header, drops whole
entries from the oldest end, and says out loud that something was removed. Two edges found by
testing rather than reasoning: **the cap silently did nothing at all** whenever the file already
existed (the separator it split on was only defined while writing a first-time header, so every
later run threw `NameError` into the catch-all and reported nothing), and when a single crash was
larger than the whole ceiling it kept **nothing** — now it keeps that entry's date line and its
*end*, because a crash names what failed on its last line, not its first.

## There is a live user (2026-08-13)

**A teammate other than the maintainer is connected to `vaulter_ai` on her own Claude Desktop and
using it.** This is a standing constraint on every change, not a status note: a mistake used to
cost a rebuild on one machine, and now it reaches someone else's working install.

**Three ways a change reaches her with no action from her:**

* **Anything under `system/`** — published to the update channel, offered inside her own
  conversation, applied on a "yes". Every commit touching the program is a change to her computer.
* **Anything published to the shared OneDrive folder** — `cost_assumptions.json`,
  `portfolio_comparison_index.json`, property summaries, jurisdiction dossiers. No update needed;
  OneDrive syncs it. This is why a wrong-*shaped* file there is a whole-team outage rather than one
  person's problem (see the shape-validation convention below).
* **`check_system_health` runs at the START of her every conversation.** Anything slow, noisy or
  crashy there is the first thing she experiences, every single time.

`quick_start/` and `.claude/` reach her too, since 2026-08-19 — in their own signed package, so a
launcher or agent-instruction fix is no longer fresh-zip-only. There is no remaining category of
change that cannot reach an installed machine. See the auto-update section for why it is a second
package and not extra entries in the first.

**The practice this requires: for anything touching library/shared-folder detection, a
shared-folder reader, or the launcher's install path, simulate the ALREADY-WORKING layouts and
assert the answer is unchanged** — don't only test that the new case now works. That is what caught
both real regressions the week this was written: the library-detection rewrite (checked against
three working layouts, all had to give the same answer as before) and the in-place upgrade, whose
first attempt **silently destroyed a machine's own `.env`** because robocopy replaced the whole file
while the comment above it claimed that could not happen. Only a test with a fake old install
carrying real settings found it.

And never assume this machine is representative — it has the cost file locally, the library under
one particular name, OCR installed and Python already working. Every teammate bug found in
2026-08 lived in a state this machine has never been in.

## Three regression suites, and the third one checks answers (2026-08-14)

`check_screener.py` (**111 checks**) and `check_portfolio_comparison.py` (**59**) both test
deterministic Python, and both pass while the answer a person actually receives is still wrong —
because the wrongness lives in the knowledge the answer was built from, not in the arithmetic.
`system/scripts/check_answers.py` (**7 checks**) is the third suite, and that knowledge is what it
tests.

**Why it exists.** On 2026-08-11 Claude stated as fact that no documents newer than 2026-08-03
existed for a property. There were 57. Every test passed, the code was flawless, and the answer
was false. Nothing in this repo could catch a wrong **answer** as opposed to broken code, and that
was the entire gap.

`python system/scripts/check_answers.py` reads file **names** only, out of the local index — it
opens no documents, downloads nothing, and makes no network or model calls, so it is free and safe
to run as often as you like. It measures four things against every property summary and the local
file index. **The observed values are not repeated here** — they live in `check_answers.py`'s own
baseline constants and in its console output, because a count of the firm's own citations and
documents is real corpus detail and this repo is public:

* **Cited documents are real files** — most exact citations resolve; a minority do not, and that
  minority was confirmed genuine rather than an index gap: the index holds paths far longer than
  any cited name, so nothing was truncated; some cited names have no near match at all, and others
  differ from the real filename by enough that a reader could not find the file. Deliberately
  abbreviated citations (a wildcard or an ellipsis) are **excluded**, not counted as failures.
* **Every summary can be currency-checked** — all carry a `Source files as of` line, but a
  handful state it as prose no code can read, which is the same "cannot be checked" state
  `check_system_health` already reports out loud rather than skips.
* **Every summary declares what it did NOT read** — all have a Gaps section.
* **Citation coverage** — the share of substantive bullets carrying a source. Reported as a
  proportion, never pass/fail: prose legitimately contains connective sentences.

It then **generates the question set at run time** from the summaries — questions with a known
answer, plus a set **that must be REFUSED**, the latter taken from Gaps entries saying something
was not established. It writes to `system/data/eval/question_set.json`, which is **gitignored**
(`system/data/eval/*`): every entry is a real firm fact and this repo is public. Deriving it each
run also means it can never go stale against the summaries it came from.

`.claude/skills/answer-eval/SKILL.md` drives the half no script can do alone: run that question set
through the real MCP tools and score each answer on **fact, source and honesty**. Whether the
must-refuse questions are actually refused is the part that matters most — this system's own
history says a confident empty answer is the most dangerous answer it can give.

**All three thresholds are measured, not chosen.** Unresolvable citations, prose date stamps and
coverage: each baseline is simply the value observed the day the check was written. They are
**regression detectors** — "has this got worse?" — and explicitly **not standards anyone
ratified**. The first draft asserted a 60% coverage threshold with nothing behind it; that was
removed, because inventing a number is the exact habit this project removes everywhere else
(the four `WEIGHTS` still sit unset rather than guessed; `cost_load = 0.35` was deleted for being
the wrong *shape*, not merely the wrong value). The rule: **lower a baseline as things get fixed,
never raise one to make a run go green.**

**Baseline rather than zero, deliberately.** A permanently-red suite gets ignored, and this
project's checks are trusted precisely because they stay quiet unless something is actually wrong —
the same reasoning already recorded for restricting the health check's staleness warning to
active-stage properties. Any **new** failure still fails immediately, which is the whole point.

**And verify the checker before believing the finding.** Its own first run reported two failures
that were the checker being wrong about the thing it was checking: it demanded an exact `## Gaps`
heading when real ones vary (`## Gaps / caveats`, `### Gaps (this verification pass)`), and it
counted deliberately-abbreviated citations as fabricated. Both fixed. A new check has no track
record, so its first failures are evidence about the check at least as much as about the data.

## Hard-won lessons

Each of these cost a real failure on a real machine. They are kept in full because the reasoning is the part that transfers.

### "Claude Desktop isn't installed" — stop making detection load-bearing (2026-08-19)

Four separate fixes have gone into finding Claude Desktop, each adding a place to look after a real
machine defeated the last one: the settings folder vs the program folder (2026-08-12), the uninstall
registry (2026-08-13), Windows app packages (2026-08-18), and now two more — **Windows' own package
registration** and the **running process**, the latter being the only route that cannot be wrong
about whether the app exists.

The registration route closes a gap every earlier route shared, and it was found by declining to
accept "we already check five places" as an answer: `LOCALAPPDATA\Packages\Claude*` is created when
a Store app first **runs**, while `HKCU\Software\Classes\ActivatableClasses\Package` is written
when it is **installed**. So a Store install that has never been opened appeared in none of the
checked places — which is exactly the state that began this whole bug family on 2026-08-12.
Confirmed present on a real machine before being added, and asserted both ways (never-opened →
found; genuinely absent → still not found). Worth
knowing why packages matter: Claude Desktop is commonly a Microsoft Store app executing from
`Program Files\WindowsApps\Claude_...`, a path none of the folder checks cover. Verified on the
maintainer's own machine, which has BOTH a classic install folder and a Store package, and is
actually running the Store one.

**The real fix was not a sixth place to look.** `_find_claude_desktop()`'s result is used ONLY as
proof the app exists — the connection is written to Claude Desktop's own settings file, whose
location is fixed and independent of where the program lives. So a failed search was withholding
something it did not gate. `setup_claude_desktop()` now **writes the connection anyway** and says
what it could not confirm: harmless if the app is genuinely absent, exactly right if it is present
and merely unrecognised, and either way the person installs the app later and it works on first
launch with nothing to redo. It still returns False so the summary flags it, because claiming
"connected" would state something the run never verified.

The generalisable lesson, and it applies well beyond this function: **when a check has been wrong
four times, stop improving the check and ask what it is gating.** Often the answer is nothing that
needs gating. A dead end built on an unreliable signal is worse than proceeding with an honest
caveat. Same family as `_newer_readable_docs`' "couldn't check ≠ nothing there".

### The empty team folder at the OneDrive root is a SYMPTOM (2026-08-19)

Seen on a real teammate's machine: `Vaulter AI Shared` sitting at her OneDrive **root**, beside
`Documents`, while the firm's library sat nested *inside* `Documents`. That root folder is not the
team's — it is an empty one **her own install created**. `_detect_shared_dir()` prefers
`corpus / SHARED_SUBFOLDER`, falls back to `ONEDRIVE_ROOT / SHARED_SUBFOLDER` when the library
cannot be found, and the `mkdir` at import time then brings it into existence.

**Why that matters far more than it looks:** `UPDATES_DIR` lives under `SHARED_DIR`, so an install
in this state reads its update channel from its own empty folder and is **never offered an
update** — which means no fix can reach it automatically, including a fix for this. Measured
2026-08-19; an earlier claim in the same session that "updates can still reach her" came from a
test where the team folder *was* inside the library, and was wrong for her actual layout. **A
machine whose library detection fails is cut off from the update channel, so it needs a fresh
package, not a published release.**

**Fixed at the root cause the same day: that folder is no longer created or used.** The OneDrive
root was the team folder's real home until 2026-08-03; returning it afterwards was a leftover from
the old design, and it did active harm rather than nothing — it put an unrequested folder in
someone's OneDrive, it left `SHARED_DIR_IS_FALLBACK` **False** so the blunt "NOT connected" warning
never fired, and it silently became the update channel. `_detect_shared_dir()` now returns
`_LOCAL_FALLBACK_DIR` instead, which is named for what it is and makes the health check say the
team folder is not connected — which is true. **A root folder that genuinely HAS content is still
preferred**, so a machine set up before the move keeps working exactly as it did; only the empty
case changed. Five layouts asserted, including the two that already worked.

The "present but EMPTY" message still exists for the legacy and still-syncing cases, and now
**tests** which
cause applies (library not found at all / library found but the folder is not inside it / still
syncing) instead of asserting one, and no longer tells anyone to have the folder shared with them
and use "Add shortcut to My files" — a step deleted 2026-08-03 when the folder moved inside the
library precisely so nobody would need it. That wording had survived six weeks past the design it
described.

The robust route out is `_library_from_onedrive_records()`: it matches OneDrive's own records by
**SharePoint address**, which is identical on every machine and finds a library wherever it is
mounted. It needs `VAULTER_LIBRARY_URL`, which `build_handoff.py` writes into the package — so a
package built before that existed (or any install whose `.env` lacks it) has no access to the one
check that does not care where the folder sits.

### Setup messages: never name a cause the code didn't test (2026-08-12)

The first real teammate install found four bugs in ten minutes, all the same shape: **the code
checks a symptom and the message confidently asserts a cause.** This is invisible on the
maintainer's machine, because a working setup only ever exercises the success path. Only a
machine in a state yours has never been in reaches these branches.

* **"Claude Desktop isn't installed"** — when she had it. `setup_claude_desktop()` checked
  Desktop's *settings* folder (`%APPDATA%\Claude`), which the app creates on first **run**, while
  the program installs at `%LOCALAPPDATA%\AnthropicClaude`. A missing settings folder means
  "never opened", not "not installed". This one genuinely **blocked** — it returned False and
  never wrote the connection. Now it looks for the program and, finding it, creates the folder
  and writes the config, removing the round trip entirely.
* **"The document library isn't on this machine"** — one message for three conditions, and wrong
  for one of them: a machine syncing two SharePoint libraries *has* the library;
  `_find_corpus_subfolder` refuses to guess, by design. Telling someone their files are missing
  when they can see them is the same failure. Each cause now gets its own instruction.
* **Shared-folder advice for a step deleted six weeks earlier** — "ask someone to share it, then
  Add shortcut to My files" was true until `SHARED_DIR` moved *inside* `CORPUS_DIR` on
  2026-08-03, specifically so that step would disappear. The function's own docstring still
  asserted the old model, which is how it survived.
* **Refusing to guess was itself a dead end** for a non-technical person who doesn't know the
  folder name — the goal is just "connect to the firm's drive". Now the library is identified by
  **content, not name**: ours is the one containing `SHARED_SUBFOLDER`. That works whatever it is
  called on a given machine, needs nothing typed, and keeps the real name out of this public
  repo. `VAULTER_CORPUS_HINT` (a distinctive word) is an opt-in last resort, deliberately blank
  in the tracked `.env.template` — the word belongs in a machine's own gitignored `.env`.

**The rule, which generalises well past install:** before shipping a message that tells someone
*why* something failed, confirm the code actually tested that. When one condition can be false
for several reasons, either distinguish them or describe only the symptom. **A confidently wrong
cause is worse than "something isn't right here"** — it sends people to solve a problem they do
not have. Same family as `_newer_readable_docs`' "couldn't check ≠ nothing new" and
`geo_providers`' "unreachable ≠ nothing there".

Worth keeping in proportion: the system itself was never broken. A full wipe-and-reinstall the
same morning passed end to end. What was broken was its ability to explain itself.

## Conventions to preserve

- **Secrets never touch `system/config.py` or git.** All credentials go through
  `system/confidentials/.env` (gitignored) and are read once in `system/config.py` via `os.getenv`;
  every other module imports the resulting constant from `config`.
- **`system/main.py` (non-MCP mode) logs to both file and stdout; MCP mode logs to file only** —
  stdout is reserved for the MCP stdio transport, and any stray print/log to stdout there
  will break the connection to that instance's own Claude Desktop.
- **There are no API keys, and that is worth defending.** Document search is local,
  ranking is arithmetic, ground truth is federal open data, proximity is OpenStreetMap, and
  the qualitative read happens in the Claude conversation that asked for it — already paid
  for. `ANTHROPIC_API_KEY` and `GOOGLE_PLACES_API_KEY` were both removed after their last
  readers went; a blank `system/confidentials/.env` is a working setup. Adding a key back means
  adding a dependency on someone's billing, so look for the free equivalent first — every
  one of the removed keys had one.
- **A shared file can be the wrong SHAPE, not just missing or corrupt (2026-08-13).** Every
  local-then-shared JSON file resolves to a copy in a OneDrive folder **every teammate can
  write to**, so a truncated sync or a hand-edit yields *well-formed JSON that is not the
  expected type*. That is a third failure mode, distinct from "missing" and "unparseable", and
  the one every reader here missed: `except (OSError, ValueError)` catches the first two and
  lets the third through to somebody's `.get()`. Found by feeding each reader a list, a string,
  a number and `null`. Real consequences measured: a list in `cost_assumptions.json` crashed
  `fit_screen` **at import**, killing the whole MCP server before it served one tool; a
  wrong-shaped `portfolio_comparison_index.json` broke `compare_to_portfolio_history` and every
  screen; a wrong-shaped update marker broke `check_system_health`, which runs at the **start of
  every conversation**. All now validate type and degrade to "no data", which every use site
  already handles — `mcp_server._json_object()` is the shared helper. This is a direct
  consequence of publishing to the shared folder: while a file was local, a bad one broke one
  machine; now it would break everyone at once. `check_screener.py` §22 asserts it.
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
- **`system/mcp_server.py` runs no background threads.** The watcher and scheduler threads (and
  the "the scheduler thread must never die" rule that existed to contain them) were removed
  in the rebuild. Don't reintroduce one — use an OS scheduled task on one designated machine.
- **Never widen the corpus scope.** Every corpus path goes through
  `corpus.resolve_in_corpus()`. The folder above `CORPUS_DIR` contains the user's personal
  files, so a path built by string-joining instead is a privacy bug, not a style question.
- **Any subprocess this server starts must have stdin CLOSED, not inherited (2026-08-21).**
  Under MCP, this process's stdin is the pipe Claude Desktop talks to us on. `capture_output=True`
  redirects only the two *output* streams — stdin stays inherited — so a child that reads it
  blocks on a pipe that will never answer, or worse, consumes bytes meant for the protocol.
  Measured: `_get_code_version`'s `git rev-parse` did exactly that, hit its 5s guard **twice**,
  and put **10.3 seconds on the first tool call of every conversation**; with
  `stdin=subprocess.DEVNULL` the same call is **0.4s**. Two things about how it hid are the real
  lesson. It **only reproduces through the real stdio transport** — the identical call costs 0.4s
  in a plain process and 1.7s in-process, so no in-process test could ever have seen it, the same
  reason `check_mcp_health.py` drives a genuine subprocess instead of importing the module. And it
  only bites an install with **no `VERSION` file**, which means a git clone — so every teammate
  was fine and only development machines paid it, the exact inverse of the usual
  "works-on-my-machine". The file-opener launchers had the same hole; they never blocked because
  they are fire-and-forget, which is precisely why nobody noticed. The `_get_code_version` guard
  did its job throughout: it bounded a hang to 5s instead of the 60–240s measured in 2026-07-30.
  **A guard that turns a hang into a delay hides the cause as effectively as it contains it** —
  so when something is merely slow, suspect a guard that is firing, not code that is heavy.

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
`vaulter-screening-pipeline`, `proximity-mapping`, `document-research`, `answer-eval`, `commit_git`,
`cleanup`, `recap`, `vaulter-rebuild`, `mcp-health-check`, and `full-sweep` are all Layer 1. This is this project's
"workflows/" — there is no separate directory by that name, and one should not be created; the
skill *is* the workflow doc.

**Layer 2 — Agents (the decision-maker).** Claude's own role, whether the main session or a
subagent in `.claude/agents/*.md`. Read the relevant skill, run tools/subagents in the correct
sequence, handle failures, ask clarifying questions when needed — connect intent to execution
without trying to do every step in one undifferentiated pass. `vaulter-screening-checker`,
`vaulter-report-checker`, `vaulter-onedrive-auditor`, `vaulter-document-reader`,
`vaulter-fact-checker`, `vaulter-city-researcher`, `vaulter-leak-guard`,
`vaulter-connection-doctor`, and `vaulter-setup-tester` are all Layer 2.

**Layer 3 — Tools (the execution).** Deterministic Python: `system/analysis/screening/fit_screen.py`,
`system/pipeline/proximity_tool.py`, `system/corpus/`, `system/scripts/` (including `check_mcp_health.py`, a real-stdio-
subprocess health check for the connector itself), and the `@mcp.tool()` functions in
`system/mcp_server.py`. Consistent, testable, fast. Credentials live only in `system/confidentials/.env` — see
"Conventions to preserve" above; this is this project's version of "never store secrets anywhere
else."

**The desks (2026-07-30).** The skills and subagents are organized into seven domains, each with
one lead and its workers. **A subagent cannot spawn other subagents** — only the main session
dispatches agents — so every desk's "lead" is a Layer 1 skill (a playbook the main session runs,
fanning out to workers), except the three single-agent desks where the worker is the whole desk.
Don't create a new standalone agent without placing it on a desk; don't create a new desk without
asking.

| Desk | Lead (Layer 1) | Workers (Layer 2) | Deterministic core (Layer 3) |
|---|---|---|---|
| CoStar screening | `vaulter-screening-pipeline` | screening-checker, report-checker, fact-checker | `fit_screen.py`, `report.py`, `check_screener.py` |
| Proximity mapping | `proximity-mapping` | onedrive-auditor (output hygiene), fact-checker (memo-bound claims) | `proximity_tool.py`, `geo_providers.py`, `geo_federal.py` |
| Connector health | `mcp-health-check` (+ auto-dispatch from the server's own MCP instructions) | connection-doctor | `check_mcp_health.py` |
| Install & onboarding | agent-led | setup-tester | `setup_wizard.py`, `release.py`/`apply_update.py` |
| Documents & research | `document-research`, `answer-eval` | document-reader, city-researcher, fact-checker | `system/corpus/`, `check_answers.py` |
| OneDrive shared folder | agent-led | onedrive-auditor | `system/config.py` path layer |
| Security | agent-led + hook | leak-guard | `.claude/hooks/check_no_leaks.py` (the hook is the only layer that can actually *block*) |

`vaulter-fact-checker` deliberately serves three desks — it's a shared verification worker, one
agent per claim, not a desk of its own.

`answer-eval` (2026-08-14) is the documents desk's **second lead**, not a desk of its own: what it
scores is the property summaries that desk writes. It is the one place the two halves are visible
side by side — `check_answers.py` is the deterministic half (do the citations point at real files,
can every summary be dated), the skill is the model-in-the-loop half (are the answers themselves
right, sourced and honest). Nothing else in the repo tests the second thing.

**Why this matters:** when a single agent pass tries to handle every step of reasoning directly,
accuracy compounds downward — five steps at 90% each chains down to 59%. Offloading execution to
deterministic code and keeping the agent layer focused on orchestration is why the QA loop caught
real bugs reliably instead of missing them the way an unstructured "does this look okay?" pass
did before it existed.

**How to operate:**
1. **Look for an existing skill, subagent, or tool first.** Check `.claude/skills/`,
   `.claude/agents/`, and `system/analysis/`/`system/pipeline/`/`system/scripts/` before writing new logic. The QA
   subagents exist because nothing did this checking before; don't rebuild what's already there.
2. **When something fails, fix the tool, verify, then record what was learned.** This project's
   regression net is three suites under `system/scripts/`, and which you run depends on what you
   touched: `check_screener.py` after any `fit_screen.py` change (real file or synthetic),
   `check_portfolio_comparison.py` after any change to the comparison index or its scoring, and
   `check_answers.py` after anything that changes the property summaries or how answers are built
   from them. What was learned goes in the `context.md` /
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
recorded in `docs/agents/screening-checker/memory.md` and this file's own history.

**Where things go:**
- **Deliverables** (what a person actually looks at) → `Vaulter AI Shared` (OneDrive), never
  local-only. This project's equivalent of "cloud services" as the deliverable destination.
- **Intermediates** → `system/data/` subfolders — gitignored, safe to delete and rebuild, the same role
  a generic `.tmp/` would play.
- **Secrets** → `system/confidentials/.env` only, per "Secrets never touch `system/config.py` or git" above.

## Working guidelines

Behavioral guidelines to reduce common LLM coding mistakes. These apply alongside the
project-specific instructions above.

**Tradeoff:** these bias toward caution over speed. For trivial tasks, use judgment.

### 0. Write for a non-technical reader — always

**Standing instruction from this project's owner, given more than once.** Every user-facing
explanation goes in plain English, as if to someone who does not write code. This is the
required register, not a simplification to fall back on when asked.

The people who read this system's output are the ones who can tell whether a number is right.
An explanation pitched at a developer never gets corrected by the person who actually knows
the deal — so jargon does not merely read badly, it hides mistakes from the only people able
to catch them.

**Where it always slips: reporting finished work.** Commit summaries and "here's what I fixed"
messages are exactly where `skiprows`, `mtime`, "schema", "placeholder", "whitelist" and
"subprocess" creep back in. That is the moment plain language matters most, because it is the
part that actually gets read.

Say **spreadsheet** not CSV, **column headings** not header row, **downloaded onto your
computer** not hydrated, **the list of files** not the index, **checks the numbers make sense**
not assertions. Lead with what broke and what it means for the reader; put mechanism last, or
leave it out.

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
  rebuild removed and why, and the measurements behind the corpus design. **Now mostly a record
  of *why*, not a plan** (revised 2026-08-18): §§1–4 are built, §5.0 and §5.1's Tier B are built,
  and §8's running order is worked through. What remains is §5.1's **Tier A (Census/BLS) and
  Tier C (agenda monitoring)** and the open questions in §7. Its §6 codebase map was **deleted**
  rather than corrected — it drifted out of date twice, and this file's own Repository layout and
  Architecture sections are the maintained copy. Don't reintroduce a second map there.
- **`docs/COMPANY_PROFILE.md`** — the firm's screening standard, derived from the portfolio and
  deal history. Intended to supersede threshold-based screening: the system should reason about
  *fit* against this profile rather than filtering on numeric cutoffs, because a wrong hard
  filter silently destroys deal flow with no error message. **Draft, unratified — derived from
  documents, confirmed by nobody.** Do not wire any number in it into a hard filter without
  human sign-off.
- **`docs/MULTI_USER_TRANSITION.md`** — historical. Still the best record of *why* the old
  design had the problems it had, but its Priority 0–2 roadmap is superseded: those problems
  lived in code that no longer exists.

`read_document` handles PDF/Word/Excel/CSV/text but **not `.msg` — and that is a settled
decision, not a gap.** REBUILD_PLAN §7 closed it 2026-07-29, and the project owner re-confirmed
it directly on 2026-08-10 with the reason stated plainly: **the firm does not want archived
Outlook email readable through this system by its users.** A deliberate privacy boundary, not a
missing feature — do not propose `extract-msg` or any other email-reading path again.
