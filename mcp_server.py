"""
mcp_server.py
--------------
Vaulter AI — MCP Server

Single entry point that runs everything:
  - PDF watcher      (background thread)
  - Scheduler        (background thread — email every 30min, web scrapes per
                       source, property intel daily)
  - MCP server       (main thread — serves Claude Desktop requests)

Deployment model: each staff member runs their OWN full local instance of
this project on their own computer (own ChromaDB, own Outlook auth, own
copy of this server). Transport is stdio, launched directly by that
person's own Claude Desktop app — nothing is exposed over the network,
and nothing is shared between instances except whatever documents live in
the shared OneDrive folder each person's ingestion pipeline also watches.
This is a deliberate privacy boundary: a staff member's own email is only
ever ingested into their own local database, never anyone else's.

Because of this, there is no server-side auth to configure (no
MCP_API_KEY, no ngrok) — the real access boundary is simply "is this your
own computer, logged in as you, with Claude Desktop configured to launch
your own copy of this process." claude.ai (the web app) CANNOT be used
with this server — it runs in the cloud and can only reach a network
address, never a process on your own machine. Claude Desktop or Claude
Code (this can launch local subprocesses directly) are required.

Start with:
  python main.py mcp

Connect in Claude Desktop:
  Settings → Developer → Edit Config → add an entry like:

    {
      "mcpServers": {
        "vaulter-ai": {
          "command": "python",
          "args": ["/absolute/path/to/main.py", "mcp"]
        }
      }
    }

  Restart Claude Desktop after saving. See Claude Desktop's own docs for
  the exact config file location on your OS (it differs Mac vs Windows).
"""

import logging
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

log = logging.getLogger("vaulter.mcp")


# ══════════════════════════════════════════════════════════════════
# Screening Source Resolver
# ══════════════════════════════════════════════════════════════════

def _resolve_costar_source(source_file: str, property_name: str = "", file_content_b64: str = "") -> "Path | None":
    """
    Resolves a CoStar export / broker spreadsheet to an on-disk path, in
    priority order:

      (a) file_content_b64 non-empty -- the user pasted/uploaded the file
          directly into the Claude conversation. Base64-decode it and
          write to data/watched_folder/<source_file>, returning that path.
          The ingestion watcher will also pick this up independently and
          chunk/embed it, moving it into PROCESSED_DIR/unknown (or a
          matched property folder) once processed.

      (b) else search data/watched_folder/ first (freshly dropped, not
          yet processed), then PROCESSED_DIR (same folder watcher.py and
          email_reader.py move files into once ingested) recursively for
          a filename matching source_file (case-insensitive) — this
          naturally covers PROCESSED_DIR/unknown, since it's just a
          subfolder under PROCESSED_DIR. If property_name is given,
          narrow to a matching property subfolder first; otherwise search
          everywhere, including "general/" and "unknown/".

      (c) else return None.
    """
    import base64
    from config import PROCESSED_DIR, WATCH_DIR

    if file_content_b64:
        try:
            # Must land at least 3 levels under WATCH_DIR (State/Property/file)
            # -- the watcher (ingestion/watcher.py) silently ignores anything
            # shallower than that, so a flat WATCH_DIR/source_file was never
            # actually picked up despite this docstring's own claim that it
            # would be. A pasted CoStar export spans many properties, so
            # there's no single real state/property to file it under -- using
            # "Unknown/General" lands it somewhere the watcher WILL ingest
            # (it already has a documented unrecognised-state fallback that
            # tags and moves it to processed/unknown/, exactly as this
            # docstring describes).
            dest = WATCH_DIR / "Unknown" / "General" / source_file
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(base64.b64decode(file_content_b64))
            return dest
        except Exception as e:
            log.warning(f"[MCP] Could not decode/write uploaded file content: {e}")
            return None

    target_lower = source_file.lower()

    # Check the drop zone first — a file that hasn't been picked up by
    # the watcher yet will still be sitting here.
    if WATCH_DIR.exists():
        for candidate in WATCH_DIR.rglob("*"):
            if candidate.is_file() and candidate.name.lower() == target_lower:
                return candidate

    if PROCESSED_DIR.exists():
        search_roots = []

        if property_name:
            for state_dir in PROCESSED_DIR.iterdir():
                if not state_dir.is_dir():
                    continue
                for prop_dir in state_dir.iterdir():
                    if prop_dir.is_dir() and property_name.lower() in prop_dir.name.lower():
                        search_roots.append(prop_dir)

        if not search_roots:
            search_roots = [PROCESSED_DIR]

        for root in search_roots:
            for candidate in root.rglob("*"):
                if candidate.is_file() and candidate.name.lower() == target_lower:
                    return candidate

    return None


# ══════════════════════════════════════════════════════════════════
# Background Services
# ══════════════════════════════════════════════════════════════════

def _start_watcher():
    """Start the PDF watcher in a background thread. supervise=True because
    nothing else here holds onto the returned Observer to monitor it — the
    watcher.py-internal supervisor thread is what notices and restarts it
    if watchdog's dispatch thread ever dies silently."""
    try:
        from ingestion.watcher import start_watcher_background
        log.info("[WATCHER] Starting PDF watcher...")
        start_watcher_background(supervise=True)
        log.info("[WATCHER] Running — watching data/watched_folder/")
    except Exception as e:
        log.warning(f"[WATCHER] Could not start: {e}")


def _start_scheduler():
    """
    Start the background scheduler in a background thread.

    This function MUST never return or raise — if the scheduler dies,
    the keepalive loop catches it and sleeps, keeping the thread alive
    so the MCP server process is never taken down with it.
    """
    import datetime as _datetime
    # Delay all first-run jobs by 5 minutes so the MCP server is fully
    # initialized before any job fires. The old 60-second delay caused
    # the first job to fire at the exact same moment as the keepalive
    # sleep(60), and a job error at that moment killed the thread.
    FIRST_RUN_DELAY = _datetime.timedelta(minutes=5)

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
        from config import SCHEDULER_TIMEZONE, RUN_SCHEDULED_SCRAPING

        scheduler = BackgroundScheduler(
            timezone=SCHEDULER_TIMEZONE,
            job_defaults={
                "coalesce":       True,   # skip missed runs instead of piling up
                "max_instances":  1,      # never run the same job twice at once
                "misfire_grace_time": 300,
            },
        )

        # ── Web scraping — each source on its own frequency ───────
        # Gated behind RUN_SCHEDULED_SCRAPING: web/property scraping hits
        # the same public pages regardless of who runs it, so only one
        # designated machine on the team needs this on (see config.py).
        # Email is NOT gated -- it's correctly per-person, never duplicated.
        if RUN_SCHEDULED_SCRAPING:
            # Sources are loaded fresh from pipeline.web_scraper.load_web_sources()
            # (not the raw config.WEB_SOURCES constant) so a CSV override in
            # data/web_sources/ is scheduled correctly -- previously the
            # scheduler always used config.WEB_SOURCES while scrape_all()
            # itself preferred a CSV if present, so any CSV-only source was
            # never scheduled, and any config-only source (when a CSV
            # existed) silently no-op'd every single time it fired.
            from pipeline.web_scraper import load_web_sources
            sources, source_label = load_web_sources()
            log.info(f"[SCHEDULER] Web sources: {source_label} ({len(sources)} sources)")

            for source in sources:
                def _scrape(name=source["name"]):
                    try:
                        from pipeline.web_scraper import scrape_all
                        scrape_all(target_name=name)
                    except Exception as ex:
                        log.warning(f"[SCHEDULER] Scrape failed ({name}): {ex}")
                scheduler.add_job(
                    _scrape,
                    trigger=IntervalTrigger(hours=source["frequency_hours"]),
                    id=f"scrape_{source['name'].replace(' ', '_')}",
                    next_run_time=_datetime.datetime.now() + FIRST_RUN_DELAY,
                    replace_existing=True,
                )

        # ── Email — every 30 minutes ───────────────────────────────
        def _email():
            try:
                from pipeline.email_reader import process_all_emails
                process_all_emails()
            except Exception as ex:
                log.warning(f"[SCHEDULER] Email check failed: {ex}")

        scheduler.add_job(
            _email,
            trigger=IntervalTrigger(minutes=30),
            id="check_email",
            next_run_time=_datetime.datetime.now() + FIRST_RUN_DELAY,
            replace_existing=True,
        )

        # ── Property intelligence — daily at 6 AM ─────────────────
        if RUN_SCHEDULED_SCRAPING:
            def _property_scrape():
                try:
                    from pipeline.property_scraper import scrape_all_properties
                    scrape_all_properties()
                except Exception as ex:
                    log.warning(f"[SCHEDULER] Property scrape failed: {ex}")

            scheduler.add_job(
                _property_scrape,
                trigger=CronTrigger(hour=6, minute=0),
                id="property_scrape",
                replace_existing=True,
            )

        scheduler.start()
        if RUN_SCHEDULED_SCRAPING:
            log.info("[SCHEDULER] Running — emails every 30min, web scrapes per source, property intel daily 6am")
        else:
            log.info("[SCHEDULER] Running — emails every 30min. Web/property scraping is OFF on this "
                      "machine (RUN_SCHEDULED_SCRAPING=false) -- another team machine handles that.")

    except Exception as e:
        log.warning(f"[SCHEDULER] Could not start scheduler: {e}")

    # Keepalive loop — runs whether or not the scheduler started.
    # Wrapped in its own try/except so any unexpected error just logs
    # and continues; the thread never exits and never kills the MCP process.
    while True:
        try:
            time.sleep(60)
        except Exception as e:
            log.warning(f"[SCHEDULER] Keepalive error (continuing): {e}")


# ══════════════════════════════════════════════════════════════════
# MCP Tools
# ══════════════════════════════════════════════════════════════════

def create_mcp_server():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        name="Vaulter AI Property Intelligence",
        instructions="""You have access to Vaulter AI's complete property intelligence database.
This includes:
- 48 active properties across Arizona, California, New Mexico, Colorado, and Texas
- Due diligence PDFs (surveys, ALTA, title reports)
- Property intelligence scraped from Google News and City-Data for each property
- Market research from CBRE, Marcus & Millichap, JLL, and GlobeSt
- Broker emails and document attachments (Word, Excel, PowerPoint, PDF)
- Inbound CoStar exports and broker listing spreadsheets

Use these tools to answer questions about the portfolio, specific properties,
market conditions, risk flags, and broker communications.
Always use the most specific tool available for the question.

For screening inbound listings from a CoStar export or broker spreadsheet,
use screen_listings. It runs the full 4-phase screening pipeline (hard-rule
elimination, composite ranking, Claude deep analysis on the top candidates,
and Google Maps ground-truth verification on the finalists) and returns a
summary with the top candidates and where to find the full workbook. There
are three ways to give it a CoStar file: (1) it's already in the system —
pass property_name if the file was matched to a specific property, or
leave it blank to search everywhere including unmatched broker emails;
(2) attach/paste the file directly into the conversation and pass its
base64 content as file_content_b64; (3) if neither applies, screen_listings
will explain how to supply the file. After screening, call
open_screening_dashboard to view the full breakdown (Pursue/Scrutinize/Pass
tabs, per-listing analyst notes, and a direct Excel download) in a browser."""
    )

    @mcp.tool()
    def search_database(query: str, n_results: int = 15) -> str:
        """
        Search the Vaulter AI database for any topic.
        Use this for general questions about properties, markets, emails, or documents.
        Args:
            query: What to search for
            n_results: Number of results (default 15, max 20)
        """
        try:
            from analysis.rag_engine import free_search, format_context_for_claude
            chunks  = free_search(query, n=min(max(1, n_results), 20))
            context = format_context_for_claude(chunks)
            return context if context else "No relevant data found for this query."
        except Exception as e:
            return f"Search failed: {e}"

    @mcp.tool()
    def get_property_info(property_name: str) -> str:
        """
        Get all available intelligence for a specific property.
        Args:
            property_name: Property name (e.g. "Magic Ranch 10", "Mesa Del Sol", "Rita Ranch")
        """
        try:
            from analysis.rag_engine import get_property_context, format_context_for_claude
            chunks  = get_property_context(property_name, n=20)
            context = format_context_for_claude(chunks)
            return context if context else f"No data found for {property_name}."
        except Exception as e:
            return f"Property lookup failed: {e}"

    @mcp.tool()
    def get_portfolio_list(group_by: str = "state") -> str:
        """
        Get the complete list of all 48 active Vaulter AI properties.
        Args:
            group_by: "state" or "stage" (default: "state")
        """
        try:
            from pipeline.property_scraper import load_properties
            props, _ = load_properties()
            groups: dict = {}
            key_field = "category" if group_by == "stage" else "state"
            for p in props:
                k = p.get(key_field, "Unknown")
                groups.setdefault(k, []).append(p)
            lines = [f"VAULTER AI PORTFOLIO — {len(props)} active properties (by {group_by}):\n"]
            for k in sorted(groups):
                lines.append(f"{k} ({len(groups[k])}):")
                for p in groups[k]:
                    lines.append(f"  - {p['name']} | {p.get('category','')} | {p.get('city','')}")
                lines.append("")
            return "\n".join(lines)
        except Exception as e:
            return f"Failed to load portfolio: {e}"

    @mcp.tool()
    def get_properties_by_stage(stage: str) -> str:
        """
        Get all properties currently in a specific stage.
        Args:
            stage: Acquisition, Pre-Plat, Final Engineering, Disposition, Site Maintenance, Rezone, Development
        """
        try:
            from pipeline.property_scraper import load_properties
            props, _ = load_properties()
            filtered = [p for p in props if p.get("category", "").lower() == stage.lower()]
            if not filtered:
                return f"No active properties found in the '{stage}' stage."
            by_state: dict = {}
            for p in filtered:
                by_state.setdefault(p.get("state", "Unknown"), []).append(p)
            lines = [f"PROPERTIES IN {stage.upper()} — {len(filtered)} total:\n"]
            for state in sorted(by_state):
                lines.append(f"{state} ({len(by_state[state])}):")
                for p in by_state[state]:
                    lines.append(f"  - {p['name']} | {p.get('city', '')}")
                lines.append("")
            return "\n".join(lines)
        except Exception as e:
            return f"Stage filter failed: {e}"

    @mcp.tool()
    def check_inbox_now() -> str:
        """
        Pull new emails from Outlook right now and store them in the database.
        Use this when the user asks about new emails, anything in the inbox,
        or wants the latest broker communications.
        """
        try:
            from pipeline.email_reader import process_all_emails
            log.info("[MCP] Live email pull triggered by user")
            process_all_emails()
            from analysis.rag_engine import get_recent_emails, format_context_for_claude
            chunks  = get_recent_emails(n=10)
            context = format_context_for_claude(chunks)
            return context if context else "Inbox checked — no new emails found."
        except Exception as e:
            return f"Email check failed: {e}"

    @mcp.tool()
    def get_email_highlights(n_emails: int = 15) -> str:
        """
        Get recent broker email content from the database.
        Args:
            n_emails: Number of email chunks to retrieve (default 15)
        """
        try:
            from analysis.rag_engine import get_recent_emails, format_context_for_claude
            chunks  = get_recent_emails(n=n_emails)
            context = format_context_for_claude(chunks)
            return context if context else "No broker emails found in the database."
        except Exception as e:
            return f"Email retrieval failed: {e}"

    @mcp.tool()
    def get_risk_scan(state: str = None) -> str:
        """
        Search the database for risk-related content across the portfolio.
        Args:
            state: Optional state filter (e.g. "Arizona"). Leave empty for full portfolio.
        """
        try:
            from analysis.rag_engine import get_cross_property_context, format_context_for_claude
            query  = "zoning denial environmental flood easement title dispute permit delay market softening legal issue risk"
            chunks = get_cross_property_context(query, state=state, n=18)
            return format_context_for_claude(chunks) or "No risk-related data found."
        except Exception as e:
            return f"Risk scan failed: {e}"

    @mcp.tool()
    def get_market_intelligence(state: str = None) -> str:
        """
        Get market intelligence from web scrapes and property news.
        Args:
            state: Optional state filter (e.g. "California"). Leave empty for all markets.
        """
        try:
            from analysis.rag_engine import (
                get_cross_property_context,
                get_recent_web_intelligence,
                format_context_for_claude,
            )
            query      = "land market new homes permits builder activity pricing trends supply demand"
            chunks     = get_cross_property_context(query, state=state, n=12)
            web_chunks = get_recent_web_intelligence(n=6)
            seen, merged = set(), []
            for c in chunks + web_chunks:
                key = c["text"][:80]
                if key not in seen:
                    seen.add(key)
                    merged.append(c)
            return format_context_for_claude(merged[:18]) or "No market intelligence found."
        except Exception as e:
            return f"Market intelligence failed: {e}"

    @mcp.tool()
    def get_database_stats() -> str:
        """
        Get a summary of what is currently in the Vaulter AI database.
        Use this to show the user how much data has been ingested.
        """
        try:
            from ingestion.embedder import get_stats
            from ingestion.registry import load_registry
            from config import DATA_DIR
            import json

            stats    = get_stats()
            registry = load_registry()

            def _load_json(path):
                try:
                    return json.loads(path.read_text()) if path.exists() else {}
                except Exception:
                    return {}

            email_registry = _load_json(DATA_DIR / "email_registry.json")
            web_registry   = _load_json(DATA_DIR / "web_registry.json")

            lines = [
                f"Vaulter AI Database — {stats['total_chunks']:,} total chunks",
                f"  PDF documents ingested : {len(registry)}",
                f"  Web sources scraped    : {len(web_registry)}",
                f"  Emails processed       : {len(email_registry)}",
            ]
            return "\n".join(lines)
        except Exception as e:
            return f"Stats failed: {e}"

    @mcp.tool()
    def open_property_files(property_name: str) -> str:
        """
        Open File Explorer directly to the folder for a property, with the first
        file selected so the user lands right on their files.
        Use this when the user says ANYTHING like:
        - "pull it up", "show me", "open it", "where is it", "can you open that"
        - "open the files for X", "show me the files for X", "pull up X"
        - "I want to see the documents", "open the folder", "show me what we have"
        - any casual request to view, access, or open property documents or files
        When in doubt and a property name is mentioned alongside any open/show/view/pull intent, use this tool.
        Args:
            property_name: Property name (e.g. "Mesa Del Sol", "Magic Ranch 10", "Forney")
        """
        import subprocess
        from config import PROCESSED_DIR
        try:
            matches = []
            if PROCESSED_DIR.exists():
                for state_dir in PROCESSED_DIR.iterdir():
                    if not state_dir.is_dir():
                        continue
                    for prop_dir in state_dir.iterdir():
                        if not prop_dir.is_dir():
                            continue
                        if property_name.lower() in prop_dir.name.lower():
                            matches.append(prop_dir)

            if len(matches) > 1:
                exact = [m for m in matches if m.name.lower() == property_name.lower()]
                folder = exact[0] if exact else matches[0]
            elif len(matches) == 1:
                folder = matches[0]
            else:
                folder = None

            if folder and folder.exists():
                files = sorted([f for f in folder.iterdir() if f.is_file()])
                if files:
                    # /select highlights the first file so user lands right on their files
                    subprocess.Popen(f'explorer /select,"{files[0]}"')
                    file_list = "\n".join(f"  - {f.name}" for f in files)
                    return f"Opened File Explorer to {folder.name}.\n\nFiles:\n{file_list}"
                else:
                    subprocess.Popen(f'explorer "{folder}"')
                    return f"Opened {folder.name} — no files there yet."
            else:
                subprocess.Popen(f'explorer "{PROCESSED_DIR}"')
                return f"No folder found for '{property_name}'. Opened the processed documents folder instead."

        except Exception as e:
            return f"Could not open folder: {e}"

    @mcp.tool()
    def open_general_files() -> str:
        """
        Open File Explorer to the general documents folder.
        Use this when the user asks for files that are not tied to a specific property,
        such as market reports, CoStar exports, general spreadsheets, or any file
        that came from email but wasn't matched to a specific property.
        """
        import subprocess
        from config import PROCESSED_DIR
        try:
            general_dir = PROCESSED_DIR / "general"
            general_dir.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(f'explorer "{general_dir}"')
            files = [f for f in general_dir.iterdir() if f.is_file()]
            if files:
                file_list = "\n".join(f"  - {f.name}" for f in sorted(files))
                return f"Opened File Explorer to general documents folder.\n\nFiles available:\n{file_list}"
            else:
                return "Opened general documents folder — no files there yet."
        except Exception as e:
            return f"Could not open folder: {e}"

    @mcp.tool()
    def open_proximity_files(property_name: str = "") -> str:
        """
        Open File Explorer to the proximity output folder, with the most recent
        file for the property selected so the user lands right on their export.
        Use this when the user says ANYTHING like:
        - "pull it up", "show me", "open it", "can you open that" — after a proximity export was run
        - "open the proximity files", "show me the CSV", "pull up the GeoJSON"
        - "open the export", "where is the proximity output", "show me the results"
        - any casual request to view or open proximity/Google Places export files
        When a proximity export was recently run and the user wants to see/open the output, use this tool.
        Args:
            property_name: Property name to find matching files (e.g. "Mesa Del Sol")
        """
        import subprocess
        from config import PROXIMITY_OUTPUT_DIR
        try:
            proximity_dir = PROXIMITY_OUTPUT_DIR
            proximity_dir.mkdir(parents=True, exist_ok=True)
            all_files = sorted([f for f in proximity_dir.iterdir() if f.is_file()], reverse=True)
            if not all_files:
                subprocess.Popen(f'explorer "{proximity_dir}"')
                return "Opened proximity output folder — no exports yet. Run a Google Places export first."

            # Find files matching this property
            if property_name:
                words = [w for w in property_name.lower().split() if len(w) > 3]
                matching = [f for f in all_files if any(w in f.name.lower() for w in words)]
            else:
                matching = all_files

            target = matching[0] if matching else all_files[0]
            # /select opens the folder with that file highlighted
            subprocess.Popen(f'explorer /select,"{target}"')

            display = matching if matching else all_files
            file_list = "\n".join(f"  - {f.name}" for f in display[:10])
            return f"Opened File Explorer to proximity output — {target.name} selected.\n\nFiles:\n{file_list}"
        except Exception as e:
            return f"Could not open proximity folder: {e}"

    # ── FOUR-PHASE LISTING SCREENER ───────────────────────────────

    @mcp.tool()
    def get_screening_rules(source_file: str = "CostarExport.xlsx", property_name: str = "") -> str:
        """
        Preview the hard rules and scoring dimensions the screener will apply
        to a CoStar export, without running the full 4-phase screening pipeline.
        Use this to check the rulebook and confirm the file's columns line up
        before committing to a full screening run.

        Use when asked to:
        - Show me what rules the screener uses
        - What hard rules apply / check the screening criteria
        - Are the rules good / do the rules make sense for this file
        """
        try:
            from analysis.screening import config as screening_config
            import pandas as pd

            file_path = _resolve_costar_source(source_file, property_name=property_name)
            if not file_path:
                property_clause = f', filtered to a property matching "{property_name}"' if property_name else ""
                return (
                    f"File not found: {source_file}\n"
                    f"Searched data/watched_folder/ and data/processed/ "
                    f"(recursively{property_clause}).\n"
                    f"Drop the CoStar export into the watched folder, or attach it to this "
                    f"conversation and I can screen it directly."
                )

            df = pd.read_excel(str(file_path))
            headers = list(df.columns)
            missing_cols = sorted({
                r["column"] for r in screening_config.RULES
                if r["column"] not in headers
            })

            hard_rules = screening_config.RULES
            compound_rules = screening_config.COMPOUND_RULES

            lines = [
                f"Screening Rulebook — {source_file}",
                f"{len(df)} listings | {len(hard_rules)} hard rules | {len(compound_rules)} compound rule(s)",
                "",
            ]
            if missing_cols:
                lines += [
                    f"⚠️  This file is missing {len(missing_cols)} column(s) the rules depend on: "
                    f"{', '.join(missing_cols)}. Those rules will error when the full screener runs — "
                    f"check the export's column headers match CoStar's standard names.",
                    "",
                ]

            lines += ["═" * 55, f"  HARD RULES ({len(hard_rules)})", "═" * 55]
            for i, r in enumerate(hard_rules, 1):
                lines.append(f"\n{i}. [{r['id']}] {r['reason']}")
                lines.append(f"   Column: {r['column']}  |  Type: {r['type']}  |  Action: {r['action']}")

            lines += ["", "═" * 55, f"  COMPOUND RULES ({len(compound_rules)})", "═" * 55]
            for i, r in enumerate(compound_rules, 1):
                lines.append(f"\n{i}. [{r['id']}] {r['reason']} (triggers at {r['min_flags']}+ flags, action: {r['action']})")

            lines += [
                "",
                "═" * 55,
                "  SCORING DIMENSIONS (Phase 2, applied to Phase 1 survivors)",
                "═" * 55,
                "\n1. Days On Market — percentile rank, fewer days scores higher",
                "2. For Sale Price — percentile rank, lower price scores higher",
                "3. Land use category fit (Secondary Type) — see analysis/screening/scoring_config.py",
                "4. Developed-environment penalty — see analysis/screening/scoring_config.py",
                "5. Flood risk severity — see analysis/screening/scoring_config.py",
                "\nWeights are calculated dynamically per run, based on how complete each dimension's data is for this file.",
                "",
                "Use test_screener to see exactly which listings these rules eliminate or flag before running the full screen.",
            ]
            return "\n".join(lines)

        except Exception as e:
            log.error(f"[MCP] get_screening_rules error: {e}", exc_info=True)
            return f"Couldn't preview the screening rules: {e}"

    @mcp.tool()
    def test_screener(source_file: str = "CostarExport.xlsx", property_name: str = "", num_listings: int = 10) -> str:
        """
        Run the real Phase 1 hard-rule engine on a CoStar export and show the
        first few listings' results — exactly which rules each one hit and
        whether it passes, gets flagged, or gets eliminated. Use this to sanity
        check the rulebook against real data before running the full screener.

        Use when asked to:
        - Test the rules on a few listings
        - Show me why listings pass or fail
        - Check if the hard rules are working correctly
        - Test the screener on 10 listings

        Args:
            source_file:  CoStar export filename (default: CostarExport.xlsx)
            num_listings: How many listings to show (default: 10)
        """
        try:
            from analysis.screening import phase1_rules
            import pandas as pd

            file_path = _resolve_costar_source(source_file, property_name=property_name)
            if not file_path:
                return f"File not found: {source_file}"

            df = pd.read_excel(str(file_path))
            missing_cols = sorted({
                r["column"] for r in phase1_rules.config.RULES
                if r["column"] not in df.columns
            })
            if missing_cols:
                return (
                    f"Can't test the rules — this file is missing required column(s): "
                    f"{', '.join(missing_cols)}. Check the export's column headers."
                )

            scored = phase1_rules.run_screener(df)
            sample = scored.head(num_listings)

            lines = [
                f"Hard Rule Test — {source_file}",
                f"Showing first {len(sample)} of {len(scored)} listings",
                "",
            ]

            for i, row_d in enumerate(sample.to_dict("records"), 1):
                address = row_d.get("Property Address") or f"Listing {i}"
                price = row_d.get("For Sale Price")
                acres = row_d.get("Land Area (AC)")
                status = row_d["Screening_Status"]
                icon = {"ELIMINATED": "❌", "FLAGGED": "🚩", "PASS": "✅"}.get(status, "•")

                lines.append(f"{'─' * 50}")
                lines.append(f"#{i} {address}")
                if pd.notna(price):
                    lines.append(f"   Price: ${price:,.0f}" + (f" | {acres} AC" if pd.notna(acres) else ""))
                lines.append(f"   Result: {icon} {status} ({row_d['Flag_Count']} flag{'s' if row_d['Flag_Count'] != 1 else ''})")
                if row_d["Screening_Reasons"]:
                    lines.append(f"   Reasons: {row_d['Screening_Reasons']}")

            counts = scored["Screening_Status"].value_counts()
            lines += [
                "",
                f"Full file summary: {counts.get('ELIMINATED', 0)} eliminated, "
                f"{counts.get('FLAGGED', 0)} flagged, {counts.get('PASS', 0)} pass clean.",
                "",
                "If any listing was wrongly eliminated or wrongly passed, describe it and I can adjust the rules in analysis/screening/config.py.",
            ]
            return "\n".join(lines)

        except Exception as e:
            log.error(f"[MCP] test_screener error: {e}", exc_info=True)
            return f"Couldn't test the screener: {e}"

    @mcp.tool()
    def screen_listings(
        source_file: str = "CostarExport.xlsx",
        property_name: str = "",
        file_content_b64: str = "",
        top_n: int = 15,
        include_low_value_apis: bool = False,
    ) -> str:
        """
        Run the four-phase CoStar listing screening pipeline on a CoStar
        export or broker spreadsheet.

        Phase 1 (Python, instant) — applies deterministic hard rules (min
          acreage, flood risk, land use category, existing structures, etc.)
          and eliminates or flags listings accordingly.
        Phase 2 (Python, fast)    — scores every Phase 1 survivor across 5
          weighted dimensions (days on market, price, land use fit,
          developed-environment penalty, flood risk) into a Composite_Score,
          sorted descending.
        Phase 3 (Claude)          — deep qualitative analysis on the top_n
          ranked listings: strengths/risks, entitlement risk, MOIC fit, red
          flags, and a pursue/conditional/pass recommendation.
        Phase 4 (Claude + Google Maps) — selects finalists from the top_n
          using Phase 3's recommendation tiering, then (if a Google Maps API
          key is configured) runs real-world ground-truth verification
          (elevation, places, roads, satellite/street view imagery, distance
          to market, etc.) and a final multimodal Claude verdict per finalist.
          Air Quality and Solar checks are skipped by default (both are low
          value for raw vacant land) — pass include_low_value_apis=true to
          include them anyway.

        There are three ways to supply the CoStar file:
          1. It's already in the system (ingested via email or the folder
             watcher) — pass property_name if it was matched to a specific
             property, or leave property_name blank to search everywhere,
             including the general/ folder for unmatched attachments.
          2. Attach or paste the file directly into the conversation and
             pass its base64-encoded content as file_content_b64.
          3. If neither applies, this tool will explain how to supply one.

        All 4 phases are written to a single combined Excel workbook. Call
        open_screening_dashboard afterward to view the full breakdown in a
        browser (Pursue/Scrutinize/Pass tabs, per-listing analyst notes, and
        a direct Excel download).

        Use when asked to:
        - Screen, filter, or analyze listings from a CoStar export
        - Find which properties Vaulter should pursue or pass on
        - Run an investment filter on inbound broker properties

        Args:
            source_file:      Filename of the CoStar export (default: CostarExport.xlsx)
            property_name:    Optional property name to narrow the search for an
                               already-ingested file (e.g. "Mesa Del Sol")
            file_content_b64: Optional base64-encoded file content, if the user
                               pasted/attached the file directly into the conversation
            top_n:             Number of top-ranked listings to carry into Phase 3 (default: 15);
                               Phase 4 then selects its 10 strongest finalists from those
            include_low_value_apis: Set true to also run Air Quality and Solar checks in
                               Phase 4 (default: false — both add little value for vacant land)
        """
        try:
            from analysis.screening.pipeline import run_full_screening
            from config import ANTHROPIC_API_KEY, GOOGLE_MAPS_API_KEY

            source_path = _resolve_costar_source(
                source_file=source_file,
                property_name=property_name,
                file_content_b64=file_content_b64,
            )

            if source_path is None:
                property_clause = f' for property "{property_name}"' if property_name else ""
                return (
                    f"Could not find a CoStar file matching '{source_file}'{property_clause}.\n\n"
                    f"There are three ways to give me a file to screen:\n"
                    f"  1. If it's already in the system, tell me the property it's linked to "
                    f"(or say it's unmatched/general) and I'll search there.\n"
                    f"  2. Attach or paste the CoStar export directly into this conversation.\n"
                    f"  3. Run check_inbox_now first if the broker email hasn't been pulled yet, "
                    f"then try again."
                )

            log.info(f"[MCP] screen_listings: resolved source to {source_path}")

            result = run_full_screening(
                source_path=source_path,
                anthropic_api_key=ANTHROPIC_API_KEY,
                google_api_key=GOOGLE_MAPS_API_KEY or None,
                top_n=top_n,
                include_low_value_apis=include_low_value_apis,
            )

            if result.get("cached"):
                header = f"SCREENING COMPLETE — {result['market']} (reused a previous team screening result from {result.get('cached_from_timestamp', 'earlier')} — no new API calls made)"
            else:
                header = f"SCREENING COMPLETE — {result['market']}"

            lines = [
                header,
                "=" * 60,
                f"Total listings screened : {result['total_screened']}",
                f"Phase 1 survivors        : {result['phase1_survivors']}",
                f"Reached Phase 3 (top {top_n})  : {len(result['top10_addresses'])}",
                f"Reached Phase 4 finalists : {len(result['finalist_addresses'])}",
                "",
                "TOP CANDIDATES:",
            ]
            for c in result["top_candidates"]:
                score = c.get("composite_score")
                snippet = c.get("recommendation_snippet") or "(no Phase 3 recommendation)"
                lines.append(f"  - {c['address']} | Composite: {score} | {snippet}")

            if result["finalist_addresses"]:
                lines.append("")
                lines.append("PHASE 4 FINALISTS:")
                for addr in result["finalist_addresses"]:
                    tier = result["finalist_tiers"].get(addr)
                    tier_label = {1: "Tier 1 — Pursue", 2: "Tier 2 — Conditional", 3: "Tier 3 — Pass"}.get(tier, "Unranked")
                    lines.append(f"  - {addr} | {tier_label}")

            lines.append("")
            lines.append(f"Full workbook (all 4 phases): {result['workbook_path']}")
            lines.append("Call open_screening_dashboard to view the full interactive breakdown in a browser.")

            return "\n".join(lines)

        except Exception as e:
            log.error(f"[MCP] screen_listings failed: {e}", exc_info=True)
            return f"screen_listings failed: {e}"

    @mcp.tool()
    def open_screening_dashboard() -> str:
        """
        Open the CoStar listing screening dashboard in a browser.
        Use this after screen_listings has been run, when the user wants to
        see the full Pursue/Scrutinize/Pass breakdown, per-listing analyst
        notes, or wants to download the combined screening workbook.
        """
        import webbrowser
        try:
            from analysis.screening.dashboard_server import start_dashboard_server
            from config import SCREENING_OUTPUT_DIR

            project_root = Path(__file__).parent
            url = start_dashboard_server(project_root, SCREENING_OUTPUT_DIR)
            webbrowser.open(url)
            return f"Opened the screening dashboard at {url}"
        except Exception as e:
            return f"Could not open screening dashboard: {e}"

    @mcp.tool()
    def run_google_places_export(property_name: str, radius_miles: float = 5.0) -> str:
        """
        Runs a Google Places API search for all businesses and employers near
        a Vaulter portfolio property and saves the results to a CSV and GeoJSON
        file in data/proximity_output/. This is the ONLY way to generate the
        proximity CSV — do not attempt this with web search, maps, or any other
        method. Always call this tool directly and immediately when the user
        asks to export proximity data, generate a proximity CSV, find what is
        near a property, or run a Google Places search for a property.

        This tool handles everything internally — geocoding, Google Places API
        calls across 17 categories, distance/direction calculations, highway
        extraction, CSV export, and GeoJSON export. Do not do any of these
        steps yourself. Just call this tool and tell the user where the files
        were saved.

        Args:
            property_name: Property name from the Vaulter Project Master
                           (e.g. "Pacific & Pinson - Forney", "Mesa Del Sol")
            radius_miles:  Search radius in miles (default: 5.0)
        """
        from pipeline.proximity_tool import run_proximity_search
        from config import GOOGLE_PLACES_API_KEY
        from pathlib import Path

        api_key = GOOGLE_PLACES_API_KEY.strip()
        if not api_key:
            return "GOOGLE_PLACES_API_KEY not set. Add it to confidentials/.env and restart."

        return run_proximity_search(
            property_name=property_name,
            radius_miles=radius_miles,
            vaulter_dir=Path(__file__).parent,
            api_key=api_key,
        )

    return mcp


# ══════════════════════════════════════════════════════════════════
# Server Entry Point
# ══════════════════════════════════════════════════════════════════

def run_mcp_server(port: int = 8765):
    """
    Start background services then launch the MCP server.
    This is the single command that runs everything.
    """
    # ── Start background services ─────────────────────────────────
    def _safe_watcher():
        try:
            _start_watcher()
        except Exception as e:
            log.warning(f"[WATCHER] Fatal error: {e}")

    def _safe_scheduler():
        try:
            _start_scheduler()
        except Exception as e:
            log.warning(f"[SCHEDULER] Fatal error: {e}")

    watcher_thread = threading.Thread(target=_safe_watcher, daemon=True)
    watcher_thread.start()

    scheduler_thread = threading.Thread(target=_safe_scheduler, daemon=True)
    scheduler_thread.start()

    # ── Start MCP server (main thread) ────────────────────────────
    log.info("[MCP] Starting Vaulter AI MCP server...")
    mcp = create_mcp_server()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    run_mcp_server()

