# Vaulter AI Property Intelligence System

An end-to-end AI system built for a real estate investment company to automate
market intelligence, document analysis, and broker email processing.

Built as a data analyst intern project using Python, Claude AI, and a modern
RAG (Retrieval-Augmented Generation) architecture — each team member runs
their own fully-local instance, accessible directly through their own Claude
Desktop app via an MCP server (see Security Notes below for why).

---

## System Overview

| Stage | Name | Description | Status |
|-------|------|-------------|--------|
| 1 | PDF Ingestion | Watches a folder, extracts text from PDFs (including scanned documents via OCR), and stores chunks in a vector database | ✅ Complete |
| 2 | Web & Email Pipeline | Scrapes public market data, pulls broker emails, and searches for property-specific intelligence tied to the Vaulter Project Master | ✅ Complete |
| 3 | MCP Server | Exposes each person's own local database as tools their own Claude Desktop can call — no separate UI needed | ✅ Complete |
| 4 | Speech-to-Knowledge | Records Monday meetings, transcribes audio, and extracts structured property updates | 🔜 Planned |

---

## CoStar Listing Screener (`analysis/screening/`)

A 4-phase pipeline for screening inbound CoStar exports and broker spreadsheets,
exposed through the MCP server so anyone on the team can screen a new listing
sheet straight from their own Claude Desktop:

| Phase | Description |
|-------|-------------|
| 1 — Hard Rules | Applies deterministic rules (minimum acreage, flood risk, target land-use categories, existing structures, stale listings, etc.) and eliminates or flags each listing. |
| 2 — Ranking | Scores every Phase 1 survivor across 5 weighted dimensions (days on market, price, land-use fit, developed-environment penalty, flood risk) into one Composite_Score, sorted descending. |
| 3 — Deep Analysis | Sends each of the top-ranked listings to Claude for a qualitative writeup — strengths/risks, entitlement risk, MOIC fit, red flags, and a pursue/conditional/pass recommendation. |
| 4 — Final Verification | Selects finalists from Phase 3's recommendation tiering, then (if configured) runs real-world Google Maps Platform ground-truth checks — elevation, nearby places, road access, satellite/street view imagery, distance to market — and a final multimodal Claude verdict per finalist. |

**Tools:**
- `screen_listings` — runs all 4 phases and returns a summary (market, screened/survived/finalist counts, top candidates with scores, and the path to the combined results workbook).
- `open_screening_dashboard` — opens a local, interactive dashboard (Pursue/Scrutinize/Pass tabs, per-listing analyst notes, and a direct Excel download) in a browser.

**Three ways to supply a CoStar file to `screen_listings`:**
1. It's already in the system — ingested via a broker email or dropped into the watched folder. Pass `property_name` if it was matched to a specific property, or leave it blank to search everywhere (including the `general/` folder for unmatched attachments).
2. Attach or paste the file directly into the Claude conversation — it gets base64-encoded and passed as `file_content_b64`.
3. If neither applies, `screen_listings` explains how to supply one.

**Required environment variable** (add to `confidentials/.env`):
```
GOOGLE_MAPS_API_KEY=AIzaSy...
```
Needs Elevation, Places, Roads, Geocoding, Distance Matrix, Static Maps, Street
View Static, Solar, Address Validation, and/or Air Quality enabled — Phase 4
auto-detects whichever subset is enabled for the key. If this key is not set,
Phases 1-3 and finalist selection still run; only the Google ground-truth
enrichment step is skipped.

---

## How the Team Uses It

Each person sets up their own local instance once (see Setup below), connected
to their own Claude Desktop app:

1. Open **Claude Desktop**
2. Ask questions in plain English — Claude automatically calls the right tools:
   - *"What's the latest on Mesa Del Sol?"*
   - *"Any new broker emails this week?"*
   - *"Run a risk scan on our Arizona portfolio"*
   - *"List all properties in Final Engineering"*

No separate app beyond Claude Desktop itself, no browser tab, no login step
each time — and because it's local, each person's own email stays private to
them (see Security Notes).

---

## Tech Stack

- **PDF Extraction** — pdfplumber, Tesseract OCR, pdf2image
- **Vector Database** — ChromaDB
- **AI Analysis** — Anthropic Claude API
- **Web Scraping** — BeautifulSoup, Requests
- **Email Integration** — Microsoft Graph API (Outlook), MSAL
- **OCR** — Tesseract (PDFs, image-based Project Master, image email attachments)
- **Document Parsing** — mammoth (Word), openpyxl (Excel), python-pptx (PowerPoint)
- **Scheduling** — APScheduler
- **MCP Server** — FastMCP (stdio, launched by each person's own Claude Desktop)
- **Transcription** — OpenAI Whisper (Stage 4)

---

## Project Structure

```
vaulter-ai/
├── main.py                    # Entry point — all commands run from here
├── config.py                  # All settings and paths in one place
├── requirements.txt           # All dependencies
├── README.md                  # This file
│
├── ingestion/                 # Stage 1 — PDF Ingestion
│   ├── extractor.py           # PDF text extraction + OCR fallback
│   ├── chunker.py             # Splits text into overlapping chunks
│   ├── embedder.py            # ChromaDB vector storage and retrieval
│   ├── watcher.py             # Folder monitoring and ingestion pipeline
│   └── registry.py            # Duplicate detection via file hashing
│
├── pipeline/                  # Stage 2 — Web & Email Data Pipeline
│   ├── web_scraper.py         # Public web scraping (reads from sources.csv)
│   ├── property_scraper.py    # Property-specific news & market data for all properties
│   ├── property_matcher.py    # Matches web/email content to Project Master properties
│   ├── email_reader.py        # Outlook email reader — handles all attachment types
│   ├── outlook_auth.py        # Microsoft OAuth2 authentication
│   └── scheduler.py           # Background scheduler for all automated jobs
│
├── analysis/                  # Stage 3 — RAG Engine
│   ├── __init__.py
│   ├── rag_engine.py          # ChromaDB retrieval and context assembly — no Claude
│   │                          # calls here; most MCP tools return this raw context
│   │                          # directly and let the requesting Claude Desktop session
│   │                          # do the reasoning (covered by its own Pro/Team plan)
│   └── screening/             # CoStar Listing Screener (4-phase pipeline) — the ONLY
│       │                      # part of this project that makes its own direct Claude
│       │                      # API calls, so the only part needing Console credits
│       ├── config.py                  # Hard rules + output columns
│       ├── scoring_config.py          # Approved scoring map (land use / flood / etc.)
│       ├── phase1_rules.py            # Phase 1 — hard rule engine
│       ├── phase2_ranking.py          # Phase 2 — composite scoring/ranking
│       ├── phase3_deep_analysis.py    # Phase 3 — Claude qualitative analysis
│       ├── phase4_verification.py     # Phase 4 — Google Maps ground-truth + final verdict
│       ├── market_utils.py            # Market detection/slugify helpers
│       ├── workbook_builder.py        # Builds the combined 4-sheet results workbook
│       ├── pipeline.py                # run_full_screening() — single entry point
│       ├── dashboard_server.py        # Local dashboard web server
│       └── dashboard/
│           └── vaulter_dashboard.html # Interactive Pursue/Scrutinize/Pass dashboard
│
├── mcp_server.py              # Stage 3 — MCP server (each user's own local Claude Desktop)
│
├── speech/                    # Stage 4 — Speech-to-Knowledge (planned)
│   └── __init__.py
│
├── confidentials/             # Secrets — never committed to git
│   ├── .env                   # All API keys and credentials
│   └── outlook_token.json     # Auto-generated after Outlook auth
│
└── data/
    ├── watched_folder/        # Drop PDFs here — State/Property/file.pdf
    │   ├── Arizona/
    │   ├── California/
    │   ├── Colorado/
    │   ├── New Mexico/
    │   └── Texas/
    ├── processed/             # PDFs move here after ingestion
    ├── chroma_db/             # Vector database (all stages write here)
    ├── logs/                  # System logs
    ├── raw_web/               # Raw scraped text (audit trail)
    ├── raw_email/             # Raw email/attachment dumps (audit trail)
    ├── project_master/        # Drop Vaulter Project Master export here
    ├── screening_uploads/     # CoStar files pasted/attached directly into a conversation
    ├── screening_output/      # Combined screening workbooks + manifest.json
    └── web_sources/
        └── sources.csv        # Add/remove web scraping sources here
```

---

## Setup

For a non-technical staff member, this is the whole process — everything else
below this is what makes it possible, not something you need to read.

### 1. Install Python (one time, no admin rights needed)
- **Windows**: https://www.python.org/downloads/ → run the installer → tick
  **"Install for me only"** (this is the option that needs no admin rights;
  its "for all users" option is the one that does).
- **Mac**: `brew install python` (or use the official installer from
  python.org). Recommended version: **3.11 or 3.12** — a much newer Python
  may not yet have ready-to-use installs for some of this project's
  dependencies, which the setup wizard below will warn you about if it
  applies to you.

### 2. Get the code
```bash
git clone https://github.com/YashuLanki/vaulter-ai.git
cd vaulter-ai
```

### 3. Run the setup wizard
```bash
python setup_wizard.py
```
This installs Python dependencies, checks for the OCR tools (Tesseract +
Poppler) and tells you exactly how to install them if they're missing (also
no admin rights needed), sets up `confidentials/.env` from the shared team
template, and connects Claude Desktop to your own local instance — merging
in its own entry without touching any other MCP server or setting Claude
Desktop already has configured. It checks and reports each step in plain
English rather than assuming success; it's also safe to run more than once.

### 4. Drop the Project Master into place
Export the Vaulter Project Master from Smartsheet (PDF, CSV, or Excel) and
drop it into `data/project_master/`.

### 5. Sign into Outlook — the one step that's genuinely yours
```bash
python main.py auth
```
Everything else (the Azure app registration, the Anthropic/Google API keys)
is a shared team value already baked in by the setup wizard — this is the
only thing that's actually specific to you, since it's you proving your own
identity, not "technical setup." This is what keeps your own email private
to your own instance — no one else's Claude session ever sees it.

### 6. Restart Claude Desktop
Fully quit and reopen it, then start a new conversation — it connects to
your own local Vaulter AI instance automatically. This only works with
Claude Desktop (or Claude Code), not the claude.ai website, since a web app
can't launch a process on your own computer.

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
`config.py` auto-detects both by searching your PATH and a few common
install locations — there's no path to hand-edit anymore. If it can't find
either, it prints a plain-English warning at startup explaining what's
missing.

#### Set up credentials
Copy `confidentials/.env.template` to `confidentials/.env` and fill in:
```
OUTLOOK_CLIENT_ID=your-application-id
OUTLOOK_TENANT_ID=your-directory-id
ANTHROPIC_API_KEY=sk-ant-your-key-here
GOOGLE_PLACES_API_KEY=your-google-places-key
GOOGLE_MAPS_API_KEY=your-google-maps-key
```
`confidentials/` is always relative to the project folder now, on every OS —
there's no longer a separate hardcoded Windows-only location to worry about.

`OUTLOOK_CLIENT_ID`/`OUTLOOK_TENANT_ID` are the **same one shared Azure app
registration for the whole team** — it just identifies "this is the Vaulter
AI Email Pipeline app," not any individual person. Each person still
authenticates with their own Microsoft account via `python main.py auth`.

To set up the shared Outlook app registration (do this once, for the whole team):
1. Go to portal.azure.com → App registrations → New registration
2. Name it "Vaulter Email Pipeline" → Single tenant → Register
3. API Permissions → Microsoft Graph → Delegated → Mail.Read
4. Authentication → Mobile/desktop → tick http://localhost → Allow public client flows: Yes
5. Copy Application ID and Directory ID from Overview into `confidentials/.env.template`
   (no client secret needed — this uses the device-code flow, which
   authenticates each person individually rather than the app itself)

#### Connect to Claude Desktop
1. Open Claude Desktop → Settings → Developer → Edit Config
2. Add an entry to `mcpServers`, preserving every other key already there:
   ```json
   {
     "mcpServers": {
       "vaulter-ai": {
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

### Stage 1 — PDF Ingestion
```bash
python main.py ingest                              # start the PDF watcher
python main.py stats                               # show full database statistics
python main.py query "flood zone Magic Ranch"      # search documents
```

Drop PDFs into `data/watched_folder/State/Property/` — ingestion is automatic.

### Stage 2 — Web & Email Pipeline
```bash
python main.py scrape                              # scrape all sources
python main.py email                               # pull new emails
python main.py email --days 30                     # pull last 30 days
python main.py property-scrape                     # scrape all active properties
python main.py properties                          # list all properties
python main.py schedule                            # run everything automatically
python main.py auth                                # authorize Outlook (once)
```

### Stage 3 — MCP Server
```bash
python main.py mcp                                 # stdio transport -- no port to configure
```

---

## Security Notes

- Each instance runs locally only — stdio transport, launched directly by that
  person's own Claude Desktop app. Nothing is exposed over the network, so
  there's no port to open, no ngrok, and no shared API key to manage
- Each staff member authenticates their OWN Outlook account into their OWN
  local database — this is what keeps one person's email private from
  everyone else's Claude session, not an access-control check
- The **one** exception to "everything is local" is CoStar screening results
  (workbooks + `manifest.json`), which save to the shared team OneDrive
  (`OneDrive - Vaulter LLC`, auto-detected — override with `VAULTER_SHARED_DIR`
  in `.env` if needed) on purpose, so one person's screening run benefits
  everyone instead of each person re-paying for the same file
- The `confidentials/` folder is gitignored — never commit it
- Anthropic's Team plan does not train on your content by default

---

*Built by Yashu Lanki — Data Analyst Intern, Vaulter*
