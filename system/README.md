# Vaulter AI Property Intelligence System

An AI system built for a real estate investment company: searchable access to the
firm's document library, and a screener that ranks inbound CoStar listing
exports by fit against the existing portfolio. Each team member runs their own local instance, used entirely
through their own Claude Desktop app via an MCP server — no separate UI.

> **Rebuilt July 2026.** This used to be a data pipeline that ingested PDFs,
> emails, and scraped web content into a per-person ChromaDB vector database.
> All of that was removed. The firm's SharePoint library is already synced to
> disk by OneDrive, so copying it into a local vector database was duplicating
> what the filesystem already had. See `docs/REBUILD_PLAN.md` for the full
> account of what was removed and why. **Email ingestion was dropped and is not
> coming back** — the inbound flow is being retired at the source.

---

## System Overview

| Part | Description |
|------|-------------|
| **Document library** (`system/corpus/`) | Searches ~493,000 files in the firm's OneDrive-synced SharePoint library by name and folder path, and reads any one of them on request — PDF (with OCR for scanned pages), Word, Excel, CSV, text |
| **Portfolio** (`system/portfolio.py`) | The active property list, read from the Smartsheet Project Master export |
| **CoStar screener** (`system/analysis/screening/fit_screen.py`) | Ranks any market's listings by proximity to existing holdings, size-in-context, MOIC-based pricing, and distress signals. Free, instant, eliminates nothing. |
| **Screening report** (`system/analysis/screening/report.py`) | One self-contained HTML file next to the workbook — the decision, then the map and shortlist, then every listing and every assumption. Opens straight from OneDrive |
| **Ground truth** (`system/analysis/screening/geo_federal.py`) | FEMA flood over the parcel's *area*, Census TIGER roads, incorporated-place status, terrain relief. Federal open data, keyless, national |
| **Proximity** (`system/pipeline/proximity_tool.py`) | What's within a radius of a listing or an owned property — one OpenStreetMap query, all categories, exported side by side so a candidate and a holding compare directly |
| **MCP server** (`system/mcp_server.py`) | Exposes all of the above as tools each person's own Claude Desktop can call |

### How document search works — read this before using it

Search matches **file and folder names, not the text inside documents.**

That isn't a shortcut, it's forced by how the library is stored. OneDrive syncs
it as Files On-Demand *placeholders*: the filenames are on your disk, the file
contents are not. Opening a file downloads it. Searching the *contents* of half
a million files would mean downloading all of them — gigabytes, per question.

In practice this works well, because the library's naming convention is dense:

```
!PROPERTIES/ARIZONA/<Property Name>/01. Legal/Acquisition/<Property Name> Closing Memo.docx
```

...carries the property, the phase, and the document kind. So the workflow is
two steps: **find candidates by name, then read the ones that look right.**

The important caveat: an empty search means *no filename matched*, not *the firm
has no records on this*. The MCP tools tell Claude this explicitly.

---

## CoStar Listing Screener (`system/analysis/screening/fit_screen.py`)

Ranks inbound CoStar exports and broker spreadsheets by **fit against Vaulter's
existing portfolio** — not against absolute thresholds. Free, instant, no API
calls, and it works on any market (AZ, TX, CO, UT, ...).

```bash
python system/main.py screen CostarExport.xlsx        # 3x MOIC target (default)
python system/main.py screen CostarExport.xlsx 2.5    # 2.5x MOIC target
```

...or ask Claude Desktop to screen it, via the `screen_listings` tool.

### It eliminates nothing

Every listing is ranked and given a reason. Low-fit listings sink to the bottom;
they don't vanish. This is deliberate: the firm's documented *rejection* history
is thin, so a hard filter is a guess that destroys deal flow with no error
message. When the older hard-rule pipeline was measured against a real 216-row
export, 60 of its 69 eliminations were on grounds the firm's own deal history
shows it buying through — floodplain, and existing structures on site.

### What it scores

| Signal | Why |
|---|---|
| **Proximity to existing holdings** *(heaviest)* | Clustering is the firm's strongest revealed preference — roughly 34 of 57 holdings sit in one of 15 clusters — and it's exactly checkable from coordinates |
| **Size, judged in context** | Not "small good, large bad." Small infill inside a cluster is normal; large as an assemblage near holdings is normal; **large *and* standalone in an unfamiliar market** is the documented failure mode |
| **Pricing, from the investor's seat** | See below |
| **Distress, as upside** | Long days-on-market, lender/REO ownership, and asking at or below prior basis are *positive* — a distressed basis was the stated #1 rationale on one of the firm's best-returning acquisitions |
| **Cautions, surfaced not applied** | Floodplain as a net-acreage adjustment, structures as possible income, oversized asks |

### The pricing lens

Vaulter is an opportunistic, value-add **predevelopment** land investor. It does
not underwrite to user or spec-developer comps. It buys raw or distressed land,
does the entitlement work, and sells the entitled position to users and
developers targeting **2.5–3x MOIC**.

So the screen doesn't ask "is this priced fairly?" It asks: *at this ask, what
must the entitled position sell for to return 3x — and is that leap plausible
here?*

The comparison is to **the product the parcel would exit as, never to same-size
peers.** The value-add mechanism is subdivision and entitlement, so a 293-acre
parcel exits as 20–100 acre parcels and those exit as sub-20-acre ones. This was
a measured bug, not a refinement: comparing every listing to peers its own size
made big parcels look like bargains, because in Pinal, small commercial parcels
ask many times more per acre than large ones — a large spread driven purely
by size. Every price in the comparison is derived from the export itself, so it
needs no entitled-land price feed and recalibrates to whatever market the file
covers.

### The costs are measured, not assumed

Rewritten July 2026 against the firm's own budget workbooks, settlement
statements and entitlement schedules. `docs/PORTFOLIO_STANDARD.md` records every
figure and the document it came from.

| | |
|---|---|
| **Entitlement** | Priced **per lot**, and it falls meaningfully with project size. It tracks lots created and plan sheets a jurisdiction demands, not what the land cost |
| **Lot yield** | **3.5 lots/acre** (range 2.5–4.2 reported alongside). The previous 8.0 was roughly double anything in the record |
| **Carry** | Charged at a measured property-tax rate over the **observed** hold, not the underwritten one |
| **Horizontal development** | Deliberately **excluded** — see below |

Streets, utilities and grading are measured on a real per-acre basis, but only
in Pinal County, and the firm sells entitled rather than improved land. So the figure is
quoted as *context* on wide-headroom rows rather than folded into the
arithmetic, and anything above 4x headroom is flagged as "the comp is probably
improved land" rather than celebrated as a bargain.

The rule underneath all of it: **a cost with no record is left out and declared,
never estimated.** Non-residential rows carry no entitlement figure because none
exists in the firm's documents, and every one of those rows says so — the
required exit shown is understated. Ranking within a type is unaffected, because
the treatment is uniform. That is what makes an honest absence safe and a
plausible guess dangerous.

Every run also reports what the evidence can and cannot cover. The record is
overwhelmingly Arizona; a Texas export ranks normally and says plainly that
there is no Texas cost, timing or exit-price history to read it against.

**It also prints the time reality on every run.** The firm's published multiples
are 2.40x at 5 years, 1.71x at 10, and 1.61x at 15 — and documented holds ran
12–16 years against 36–48 months underwritten. A 3x over 4 years is 31.6% IRR;
the same 3x over 14 years is 8.2%. Both are shown so a pro forma can't be read
innocently.

### It works on whatever shape the export arrives in

Broker and CoStar exports vary: different column names, a title row and a filter
summary above the real header, whole fields simply missing. The screener finds
the header wherever it is (scanning the first dozen rows for one that looks like
a header) and resolves each field from whatever the file provides.

**Where it can't, it abstains and says so.** A `Price/Acre` column is *not*
accepted as the asking price; a `Lot Size` column holding square feet is *not*
accepted as acres. Both were real misreads that made every downstream number
wrong with nothing to indicate it. A missing field is reported as missing — the
HTML report opens with a summary of what this particular file did and didn't
carry, so a thin export never reads as confidently as a complete one.

### Assumptions are not ratified

Every tunable — the MOIC target, the measured cost anchors, the scoring weights —
sits in `ASSUMPTIONS` at the top of `fit_screen.py` and in the workbook's
"Assumptions" tab, deliberately in one place so a partner can argue with it. Each
value carries a comment naming the deal and document it came from.

The cost and return figures are measured; **nobody has signed off on them.** The
four scoring weights have no evidence behind them at all — they are the weakest
input in the whole model. `docs/COMPANY_PROFILE.md` is a **draft** built from the
firm's documents and confirmed by nobody, and parts of it are already superseded
by the measured record.

### Checking it still works

```bash
python system/scripts/check_screener.py                              # 68 assertions
python system/scripts/check_screener.py "system/data/drop/CostarExport (2).xlsx"   # against a thin export
```

The only automated safety net in the repo. Run it after any change to
`fit_screen.py`.

### Supplying a file

1. Drop it into `system/data/drop/` (ask Claude to `open_costar_folder`)
2. Attach or paste it into the Claude conversation — it's passed as `file_content_b64`
3. If it's already filed in the document library, it'll be found by name

**API keys: none, anywhere.** Ranking is arithmetic, ground truth is federal
open data, proximity runs on OpenStreetMap, and the qualitative read happens in
the Claude conversation that asked for the screen.

## How the Team Uses It

Each person sets up their own local instance once (see Setup below), connected
to their own Claude Desktop app:

1. Open **Claude Desktop**
2. Ask questions in plain English — Claude automatically calls the right tools:
   - *"What do we have on file for <property>?"*
   - *"Find the closing memo for <property> and summarise it"*
   - *"List all properties in Final Engineering"*
   - *"Screen this CoStar export"* → then *"open the report"*
   - *"Check flood and road access on the top six"*
   - *"What's within 5 miles of rank 3, compared to <property>?"*

No separate app beyond Claude Desktop itself, no browser tab, no login step
each time.

---

## Tech Stack

- **Document reading** — pdfplumber, Tesseract OCR (scanned pages), pdf2image,
  mammoth (Word), openpyxl / xlrd (Excel), pandas (CSV)
- **Document index** — SQLite (standard library)
- **Geodata** — OpenStreetMap/Overpass (proximity), FEMA National Flood Hazard
  Layer, Census TIGERweb (roads, places, county/city outlines), OpenTopoData/USGS
  NED elevation, NAIP aerial imagery, Nominatim — all keyless
- **MCP Server** — FastMCP (stdio, launched by each person's own Claude Desktop)

## Project Structure

```
Vaulter_AI/
├── main.py                    # Entry point — all commands run from here
├── config.py                  # All settings and paths in one place
├── portfolio.py               # Reads the Smartsheet Project Master export
├── mcp_server.py              # MCP server — no background threads
├── requirements.txt
│
├── system/corpus/                    # The firm's document library (read-only)
│   ├── index.py               # SQLite name index, search, and the scope guard
│   └── extract.py             # PDF/Word/Excel/CSV/text -> plain text
│
├── system/analysis/screening/        # CoStar Listing Screener
│   ├── fit_screen.py              # THE LIVE SCREENER — portfolio-fit ranking
│   ├── geo_federal.py             # Ground truth (FEMA flood over the parcel area, Census roads)
│   ├── geo_providers.py           # Keyless geodata + the Overpass mirror/cache layer
│   ├── report.py                  # Builds the self-contained HTML report
│   └── report_template.html
│
├── system/pipeline/
│   ├── proximity_tool.py       # Proximity export — one OpenStreetMap query, all categories
│   └── property_coordinates.py # Hand-verified coordinates per property, read off deeds
│
├── system/core/safe_io.py            # Atomic writes, file locking, conflict merging
├── system/scripts/                   # release, apply_update, push_org_setting, setup_wizard,
│                              #   check_screener (the screener's test harness)
├── quick_start/               # Double-clickable setup launchers
├── system/confidentials/             # Secrets — never committed to git
│
├── docs/
│   ├── PORTFOLIO_STANDARD.md  # The measured evidence base — every figure, with its source
│   ├── COMPANY_PROFILE.md     # Draft screening standard, ratified by nobody
│   ├── REBUILD_PLAN.md        # What was removed and why; what's built vs planned
│   ├── MULTI_USER_TRANSITION.md  # Historical — why the old design had its problems
│   └── jurisdictions/         # Per-city dossiers (comp plans, CIPs, water/sewer)
│
└── system/data/
    ├── drop/                  # Drop CoStar exports here (nothing watches it)
    ├── project_master/        # Smartsheet Project Master export
    ├── pending_update/        # A staged code update, waiting for you to say yes
    ├── pending_settings/      # A staged org-wide setting, same
    ├── corpus_index.db        # Local index of library filenames (no contents)
    └── logs/
```

**No documents are parsed by any of this.** `PORTFOLIO_STANDARD.md` and the
jurisdiction dossiers are written for people to read. The screener's numbers live
in code, in `ASSUMPTIONS`, each one citing the document it came from — so the
record and the running configuration can be checked against each other but can't
silently drift apart.

Screening output does **not** live under `system/data/` — it goes to the shared team
OneDrive so one person's run is visible to everyone. See Security Notes.

---

## Setup

For a non-technical staff member, this is the whole process. No terminal, no
typed commands — everything is either downloading, double-clicking, or
signing in on a normal web page. Everything else below this section is what
makes that possible, not something you need to read.

### 1. Install Python (one time, no admin rights needed)
- **Windows**: https://www.python.org/downloads/ → run the installer → tick
  **"Install for me only"** (this is the option that needs no admin rights;
  its "for all users" option is the one that does).
- **Mac**: download the official installer from python.org and run it like
  any other app installer. Recommended version: **3.11 or 3.12** — a much
  newer Python may not yet have ready-to-use installs for some of this
  project's dependencies, which the setup wizard below will warn you about
  if it applies to you.

### 2. Download the code
Go to https://github.com/YashuLanki/Vaulter_AI → click the green **Code**
button → **Download ZIP** → unzip it into a folder (e.g. your Documents
folder). This is a normal file download, no `git` needed.

### 3. Double-click "Setup Vaulter AI"
Inside the unzipped folder, open the **`quick_start/`** folder and double-click
**`Setup Vaulter AI.command`** (Mac) or **`Setup Vaulter AI.bat`** (Windows). A
plain window opens showing its progress — it installs Python dependencies,
checks for the OCR tools (Tesseract + Poppler) and tells you exactly how to
install them if they're missing (also no admin rights needed), sets up
credentials from the shared team template, connects Claude Desktop to your own
local instance — merging in its own entry without touching any other MCP server
or setting Claude Desktop already has configured — and finally builds the index
of the firm's document library.

That last step takes a couple of minutes and reads **filenames only** — no
documents are downloaded. Leave the window open until it finishes.

There is nothing in this process specific to you personally: no account to sign
into, no key to paste. The library reaches your machine through OneDrive, which
you're already signed into. The wizard reports each step in plain English rather
than assuming success, and it's safe to double-click more than once.

*Mac only, first time:* macOS may say the file "cannot be opened because it
is from an unidentified developer." Right-click the file → **Open** → confirm
— this is a normal one-time step for any downloaded script, not specific to
this project.

### 4. Drop the Project Master into place
Export the Vaulter Project Master from Smartsheet (**CSV or Excel** — PDF
exports are no longer supported) and drag it into the `system/data/project_master/`
folder. Export **.xlsx** if you need sold deals separated from active ones: a
CSV can't carry the strikethrough formatting that marks a deal sold, so every
row comes through as active.

### 5. Restart Claude Desktop
Fully quit and reopen it, then start a new conversation — it connects to
your own local Vaulter AI instance automatically. This only works with
Claude Desktop (or Claude Code), not the claude.ai website, since a web app
can't launch a process on your own computer.

*Later on,* when documents have been added to the library, ask Claude to rebuild
the index (or run `python system/main.py index-corpus`). Newly filed documents are
invisible to search until then; the health check warns you once the index is
more than 30 days old.

<details>
<summary><strong>Manual / advanced setup</strong> (troubleshooting, or if you'd rather not run the wizard)</summary>

#### Create a virtual environment
```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Mac/Linux
```

#### Install Python dependencies
```bash
pip install -r requirements.txt
```

#### Install external tools
**Windows:**
- **Tesseract OCR**: https://github.com/UB-Mannheim/tesseract/wiki (per-user install option)
- **Poppler**: https://github.com/oschwartz10612/poppler-windows/releases (just unzip it anywhere)

**Mac:**
```bash
brew install tesseract poppler
```
`system/config.py` auto-detects both by searching your PATH and a few common
install locations — there's no path to hand-edit anymore. If it can't find
either, it prints a plain-English warning at startup explaining what's
missing.

#### Set up credentials
Copy `system/confidentials/.env.template` to `system/confidentials/.env` and fill in:
```
(nothing required)
```
There are no API keys. A blank `.env` is a working setup — the file only exists
for machines where OneDrive put a folder somewhere unexpected. No Outlook app
registration, no Anthropic key, no Google key, no per-person sign-in.

`system/confidentials/` is always relative to the project folder, on every OS.

If OneDrive put the document library somewhere unexpected, set
`VAULTER_CORPUS_DIR` (and `VAULTER_SHARED_DIR` for the team output folder).
Both auto-detect `OneDrive - Vaulter LLC` otherwise.

#### Build the document index
```bash
python system/main.py index-corpus
```
Reads filenames only. Takes a couple of minutes over ~493,000 files.

#### Connect to Claude Desktop
1. Open Claude Desktop → Settings → Developer → Edit Config
2. Add an entry to `mcpServers`, preserving every other key already there:
   ```json
   {
     "mcpServers": {
       "vaulter_ai": {
         "command": "python",
         "args": ["/absolute/path/to/main.py", "mcp"]
       }
     }
   }
   ```
3. Restart Claude Desktop. No ngrok, no API key, no network exposure needed —
   each instance is local-only by design.

</details>

---

## Usage

Day to day, nobody runs commands — everything happens by asking Claude Desktop.
These are for setup and troubleshooting:

```bash
python system/main.py mcp                    # start the MCP server (Claude Desktop runs this)
python system/main.py index-corpus           # (re)build the document-library index
python system/main.py search "closing memo"    # search the library by filename/path
python system/main.py screen CostarExport.xlsx      # rank an export by portfolio fit
python system/main.py screen CostarExport.xlsx 2.5  # ...at a 2.5x MOIC target instead of 3x
python system/main.py properties             # list the portfolio from the Project Master
python system/main.py stats                  # what this instance has available
```

---

## Staying up to date

Nobody pulls code. When a reviewed fix is published, each instance notices it on
its own — the once-a-day check runs inside the health check Claude already
performs at the start of a conversation, so there is no background process.

If an update is waiting, Claude mentions it and asks. Say yes and it applies:
files sync into place, dependencies reinstall with the same Python already
running the project, and the staging area clears. **The whole thing happens in
the conversation — no terminal.**

The one manual step: fully quit and reopen Claude Desktop afterward. An MCP
server can't restart the app that launched it.

Updates ship to a `canary` channel first and reach everyone else only once
they've been confirmed healthy, so a bad fix can't break every instance at once.
Staff machines are on `general` and need no configuration for this.

---

## Security Notes

- Each instance runs locally only — stdio transport, launched directly by that
  person's own Claude Desktop app. Nothing is exposed over the network, so
  there's no port to open, no ngrok, and no shared API key to manage
- **The document library is read-only and scoped.** `CORPUS_DIR` points at the
  `Vaulter LLC - shaw` SharePoint library specifically, never the OneDrive
  account root above it — that root also holds the individual's own Desktop,
  Documents, and Teams chat files. Every path is resolved and re-checked against
  that boundary (`corpus.resolve_in_corpus`), so `../Documents` and absolute
  paths elsewhere on disk both fail. The system never writes to the library
- The local index (`system/data/corpus_index.db`) holds **filenames, sizes, and dates
  only** — never file contents
- Because the library is a shared SharePoint site, everyone with access sees the
  same documents. Unlike the old email pipeline, there is no per-person private
  data in this system at all
- The **only** things written outside your machine are screening and proximity
  outputs — the ranked workbook, the HTML report, the proximity CSV/XLSX, and
  two lookup caches (ground-truth results by coordinate, and basemap outlines).
  They save to the shared team OneDrive (`OneDrive - Vaulter LLC`, auto-detected
  — override with `VAULTER_SHARED_DIR` in `.env` if needed) on purpose, so one
  person's run is visible to the whole team. Nothing there is private to a
  person, and nothing is read back as shared *state* — each file has a single
  writer
- The `system/confidentials/` folder is gitignored — never commit it
- Anthropic's Team plan does not train on your content by default

---

*Built by Yashu Lanki — Data Analyst Intern, Vaulter*
