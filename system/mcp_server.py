"""
mcp_server.py
--------------
Vaulter AI — MCP Server

Serves MCP tools on the main thread over stdio. That is now the whole of it:
there are no background threads. The PDF watcher and the APScheduler thread
(email every 30min, web scrapes, property intel) were both deleted in the
2026-07 rebuild, along with the "the scheduler thread must never die"
constraint that existed to keep them from taking the server down.

What the tools sit on top of:

  - corpus/      Read-only access to the firm's SharePoint document library,
                 synced locally by OneDrive. Replaces the ingest → chunk →
                 embed → ChromaDB pipeline, which was a local copy of
                 documents the filesystem already had.
  - portfolio.py The property list, read from the Smartsheet Project Master.
  - analysis/screening/  fit_screen ranks a CoStar export against the portfolio;
                 report builds the HTML report; geo_federal does ground truth.

Deployment model: each staff member runs their own local copy of this server,
launched directly by their own Claude Desktop over stdio. Nothing is exposed
over the network, so there is no server-side auth to configure (no
MCP_API_KEY, no ngrok) — the access boundary is "is this your own computer,
logged in as you." claude.ai (the web app) CANNOT be used with this server:
it runs in the cloud and can only reach a network address, never a process on
your own machine. Claude Desktop or Claude Code are required.

The privacy boundary is now enforced by scope rather than by isolation. The
old design kept each person's email in their own local database. There is no
email ingestion any more; instead, `config.CORPUS_DIR` points at the
"Vaulter LLC - shaw" library specifically and never at the OneDrive account
root above it, which holds that individual's own Desktop, Documents, and
Teams chat files. `corpus.resolve_in_corpus` enforces this on every path.

Start with:
  python main.py mcp

Connect in Claude Desktop:
  Settings → Developer → Edit Config → add an entry like:

    {
      "mcpServers": {
        "vaulter_ai": {
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

log = logging.getLogger("vaulter.mcp")


# ══════════════════════════════════════════════════════════════════
# File Explorer / Finder Helper
# ══════════════════════════════════════════════════════════════════

def _open_in_file_manager(path: "Path", select: bool = False) -> None:
    """
    Opens `path` in the OS's file manager -- Explorer on Windows, Finder
    on Mac, the default file manager on Linux (via xdg-open). Each staff
    member's own instance could be running on any of these, per
    mcp_server.py's own header (each person runs this locally on their
    own machine). Previously this only ever ran `explorer`, which is
    Windows-only -- silently failing (and returning a raw error instead
    of the useful file list) for anyone on Mac or Linux.

    If select=True and path is a file, opens the containing folder with
    that file highlighted where the platform supports it (Windows/Mac);
    Linux has no widely-supported equivalent, so it just opens the
    containing folder unselected. If select=False, path is opened
    directly (expected to be a folder).
    """
    import subprocess

    # stdout/stderr are pinned to DEVNULL rather than inherited. Under MCP the
    # parent's stdout IS the transport, and a launcher that writes so much as a
    # deprecation notice onto it corrupts the connection to Claude Desktop.
    # xdg-open in particular is a shell script that talks on both streams.
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}

    if sys.platform == "win32":
        if select:
            subprocess.Popen(f'explorer /select,"{path}"', **quiet)
        else:
            subprocess.Popen(f'explorer "{path}"', **quiet)
    elif sys.platform == "darwin":
        if select:
            subprocess.Popen(["open", "-R", str(path)], **quiet)
        else:
            subprocess.Popen(["open", str(path)], **quiet)
    else:
        target = path.parent if select else path
        subprocess.Popen(["xdg-open", str(target)], **quiet)


def _find_property_folders(property_name: str) -> list:
    """
    Find property folders under !PROPERTIES/<STATE>/ whose name contains
    property_name (case-insensitive substring). Shared by open_property_files
    and open_property_document so both use the identical matching rule.

    Returns folders sorted by name (deterministic, not iterdir()'s arbitrary
    order). Caller decides what "exact match wins, else first, note others"
    means for its own situation -- this just finds candidates.
    """
    from config import CORPUS_DIR
    matches = []
    properties_root = CORPUS_DIR / "!PROPERTIES"
    if properties_root.is_dir():
        for state_dir in properties_root.iterdir():
            if not state_dir.is_dir():
                continue
            for prop_dir in state_dir.iterdir():
                if prop_dir.is_dir() and property_name.lower() in prop_dir.name.lower():
                    matches.append(prop_dir)
    matches.sort(key=lambda p: p.name)
    return matches


# ══════════════════════════════════════════════════════════════════
# Code Version
# ══════════════════════════════════════════════════════════════════

def _get_code_version() -> str:
    """
    Best-effort version of the running code, for check_system_health,
    support/debugging, and comparing against a published release. Must
    never raise -- this is read at the start of every conversation, and a
    machine without git on PATH (or a non-git deployment) is a real
    possibility, not an error condition.

    Checks the VERSION file release.py ships inside every update package
    FIRST, falling back to the local git HEAD only if that file doesn't
    exist yet (a fresh clone that's never had an update applied). This
    order matters: apply_update.py deliberately never touches .git (see
    its own PRESERVED_DIR_NAMES), so an instance's git HEAD stays frozen
    at whatever commit it was originally cloned from forever, even once an
    update genuinely changes every file in it. Reading git first would
    make every post-update instance permanently report its pre-update
    commit -- confirmed 2026-07-29 to make _check_and_stage_update() see
    remote != current on every check and re-stage the SAME already-applied
    version in an endless loop, since nothing else ever moves that
    comparison. VERSION is a plain file, so applying an update updates it
    exactly like any other shipped file, no git operation required.
    """
    version_file = Path(__file__).parent / "VERSION"
    try:
        content = version_file.read_text(encoding="utf-8").strip()
        if content:
            return content
    except OSError:
        pass

    # Run in a background thread rather than trusting subprocess.run's own
    # timeout: measured 2026-07-30 that a stuck git subprocess can make
    # communicate()'s internal reader-thread .join() hang for 60-240+s even
    # with timeout=5 set, blocking check_system_health -- the tool Claude
    # calls first in every conversation -- and surfacing as a dead server.
    # Waiting on a queue with our own timeout means we give up in 5s no
    # matter what the subprocess plumbing does; the abandoned thread is
    # harmless since it's a daemon and touches nothing but its own pipes.
    import subprocess, threading, queue
    result_q = queue.Queue(maxsize=1)

    def _run_git():
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(Path(__file__).parent),
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                result_q.put(result.stdout.strip())
        except Exception:
            pass

    threading.Thread(target=_run_git, daemon=True).start()
    try:
        return result_q.get(timeout=5)
    except queue.Empty:
        return "unknown"


def _check_and_stage_update() -> None:
    """
    Priority 4 (docs/MULTI_USER_TRANSITION.md): checks this instance's
    release channel for a newer version than what's currently running,
    and if found, downloads (copies) it into the local PENDING_UPDATE_DIR
    staging area. Deliberately does NOT extract or apply anything -- this
    first version of the mechanism is notify-and-stage only; a human
    confirms in a Claude conversation once check_system_health surfaces
    it, and Claude calls the apply_pending_update tool on their behalf
    (no terminal needed).

    Safe to call repeatedly: if the already-staged version matches the
    current marker, does nothing.
    """
    import datetime as _dt
    import json
    import shutil as _shutil
    from config import UPDATES_DIR, PENDING_UPDATE_DIR, VAULTER_UPDATE_CHANNEL

    marker_path = UPDATES_DIR / f"latest_version_{VAULTER_UPDATE_CHANNEL}.json"
    if not marker_path.exists():
        return  # nothing published to this channel yet

    remote = json.loads(marker_path.read_text())
    remote_version = remote.get("version")
    current_version = _get_code_version()
    if not remote_version or remote_version == current_version:
        return  # already on the latest version for this channel

    ready_path = PENDING_UPDATE_DIR / "ready.json"
    if ready_path.exists():
        already_staged = json.loads(ready_path.read_text()).get("version")
        if already_staged == remote_version:
            return  # already downloaded, just waiting for a human to apply it

    zip_filename = remote.get("zip_filename")
    remote_zip = UPDATES_DIR / (zip_filename or "")
    if not zip_filename or not remote_zip.exists():
        log.warning(f"[UPDATE] {marker_path.name} points to version {remote_version}, but its "
                    f"package file is missing -- skipping until it's available.")
        return

    local_zip = PENDING_UPDATE_DIR / zip_filename
    _shutil.copy2(remote_zip, local_zip)
    ready_path.write_text(json.dumps({
        "version": remote_version,
        "zip_filename": zip_filename,
        "downloaded_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "notes": remote.get("notes", ""),
        "current_version_at_download": current_version,
    }, indent=2))
    log.info(f"[UPDATE] Staged version {remote_version} (currently running {current_version}) -- "
             f"apply it with the apply_pending_update tool, or as a fallback "
             f"`python system/scripts/apply_update.py`.")


def _check_and_stage_org_settings() -> None:
    """
    Priority 4 extension: checks the shared org_settings folder for any
    org-wide value (e.g. a new feature's API key, pushed via
    scripts/push_org_setting.py) that this instance doesn't have filled
    in locally yet, and stages it -- never writes into confidentials/.env
    directly. A human confirms in a Claude conversation once
    check_system_health surfaces it, and apply_pending_settings does the
    actual write (see that tool's docstring for why this stays
    deliberately generic in the conversation -- no key names or values
    ever appear in anything sent back to Claude).

    Only fills in a key that's currently blank/missing locally -- if
    some value is already set (even an old one), this instance is
    treated as already configured and left untouched. Safe to call
    repeatedly. If a key is already staged locally but the maintainer
    has since republished a DIFFERENT value for it (e.g. fixing a typo
    before anyone applied the first one), the staged entry is updated
    to match -- otherwise a staff member who hasn't yet said "yes"
    would end up applying a stale, superseded value.
    """
    import datetime as _dt
    import json
    from dotenv import dotenv_values
    from config import ORG_SETTINGS_DIR, PENDING_SETTINGS_DIR, SECRETS_DIR

    if not ORG_SETTINGS_DIR.exists():
        return

    env_path = SECRETS_DIR / ".env"
    local_values = dotenv_values(env_path) if env_path.exists() else {}

    staged_path = PENDING_SETTINGS_DIR / "staged.json"
    staged_entries = []
    if staged_path.exists():
        try:
            staged_entries = json.loads(staged_path.read_text())
        except Exception:
            staged_entries = []
    staged_by_key = {e["key"]: e for e in staged_entries if e.get("key")}

    changed = False
    for setting_file in ORG_SETTINGS_DIR.glob("*.json"):
        try:
            remote = json.loads(setting_file.read_text())
        except Exception:
            continue
        key = remote.get("key", "").strip()
        value = remote.get("value", "")
        if not key or not value:
            continue
        if local_values.get(key):
            continue  # already configured locally -- nothing to do
        existing = staged_by_key.get(key)
        if existing is not None and existing.get("value") == value:
            continue  # already staged with this exact value -- nothing to do
        staged_by_key[key] = {
            "key": key,
            "value": value,
            "label": remote.get("label", key),
            "staged_at": _dt.datetime.now().isoformat(timespec="seconds"),
        }
        changed = True

    if changed:
        staged_path.write_text(json.dumps(list(staged_by_key.values()), indent=2))
        log.info(f"[ORG_SETTINGS] Staged {len(staged_by_key)} pending setting(s) -- "
                 f"a human will be asked to confirm before anything is written.")


# ══════════════════════════════════════════════════════════════════
# Cell Formatting
# ══════════════════════════════════════════════════════════════════

def _numeric(v):
    """
    A real number, or None. `v == v` alone is NOT enough: it catches
    float('nan') but None equals itself, so a blank cell sailed through
    and f"{None:,.0f}" raised TypeError. That crashed the whole tool on
    the first export containing a row with no acreage -- and the failure
    surfaced to Claude as an unresponsive server, not as a bad cell.

    Module level, not nested inside one tool: the same TypeError was
    still live in verify_listings and run_proximity_for_listing, which
    could not reach the copy that lived inside screen_listings.
    """
    if v is None or isinstance(v, str) or v != v:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _screen_error(source_name: str, e: Exception) -> str:
    """
    A failure to read an export, in words a non-technical reader can act
    on. Pasting a truncated attachment used to come back as pandas'
    "Excel file format cannot be determined, you must specify an engine
    manually", which names no file and suggests nothing anyone here can
    do. The technical detail still goes to the log, every time.
    """
    text = str(e)
    if "format cannot be determined" in text or "Excel file" in text or "codec" in text:
        return (f"'{source_name}' is not a spreadsheet I can read. The file is there, but its "
                f"contents are not a valid Excel or CSV export — if it was attached to this "
                f"conversation the upload may have been cut short. Try attaching it again, or "
                f"put the original file in the CoStar folder (call open_costar_folder) and give "
                f"me its name.")
    return (f"Could not screen '{source_name}': {text}\n\n"
            f"Nothing was changed. The full technical detail is in this instance's log file.")


def _acres_str(v) -> str:
    """
    "73ac" or an explicit statement of absence. Never "?ac" and never
    "nanac": on the 50-row Tucson export the acreage column exists but is
    blank on 45 of 50 rows, so this is the common case, not the edge.
    """
    acres = _numeric(v)
    return f"{acres:,.0f}ac" if acres else "size not stated"


# ══════════════════════════════════════════════════════════════════
# Property summary staleness
# ══════════════════════════════════════════════════════════════════

def _summary_staleness(property_name: str, summary_text: str) -> str:
    """
    Compare a summary's own "Source files as of:" stamp against the corpus
    index's mtimes, and say what has been filed since.

    The failure this exists to prevent is the worst shape a wrong answer can
    take here: not "I don't know" but a confident, well-cited answer that is
    simply months out of date. Every summary already records how current it
    was; the index already knows every file's mtime. Nothing was comparing
    them, so a summary could silently age forever.

    Returns a line to prepend to the summary, or "" when it can't tell --
    never a guess. Reports the newest filenames rather than only a count,
    because "6 newer files" prompts a re-read of everything while
    "Closing Memo.docx, Final ESA.pdf" tells the reader whether the change
    even matters to the question they asked.
    """
    import re
    import sqlite3
    import datetime as _dt

    m = re.search(r"Source files as of:\*{0,2}\s*(\d{4})-(\d{2})-(\d{2})", summary_text)
    if not m:
        return ""
    try:
        stamped = _dt.datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=_dt.timezone.utc)
    except ValueError:
        return ""

    try:
        from config import CORPUS_INDEX_FILE
        if not Path(CORPUS_INDEX_FILE).exists():
            return ""
        con = sqlite3.connect(f"file:{CORPUS_INDEX_FILE}?mode=ro", uri=True)
        try:
            # LIKE, so escape its wildcards -- '_' matches any single
            # character, which silently over-matches property names.
            needle = property_name.strip().replace("!", "!!").replace("%", "!%").replace("_", "!_")
            # Count ONLY what read_document can actually open. This started as
            # a blocklist (.eml/.msg) and that was not enough: on one real
            # property the four "newest" files were drone .MP4s, so the
            # warning told the reader to go read a video. A whitelist keeps it honest by
            # construction -- if the tool can't open it, naming it is worse than
            # silence, because it converts "here is what to check" into a dead
            # end. Same lesson the .eml exclusion taught: an alarm pointing at
            # something unreadable trains people to ignore alarms.
            exts = ("pdf", "doc", "docx", "xls", "xlsx", "csv", "txt", "md")
            ext_clause = " OR ".join("lower(name) LIKE ?" for _ in exts)
            where = (f"path LIKE ? ESCAPE '!' AND mtime > ? AND ({ext_clause})")
            args = (f"%{needle}%", int(stamped.timestamp()), *[f"%.{e}" for e in exts])
            rows = con.execute(
                f"SELECT name, mtime FROM files WHERE {where} ORDER BY mtime DESC LIMIT 6", args
            ).fetchall()
            total = con.execute(f"SELECT COUNT(*) FROM files WHERE {where}", args).fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error as e:
        log.warning(f"[MCP] Staleness check failed for {property_name}: {e}")
        return ""

    if not total:
        return ("\nCURRENCY: no readable documents newer than this summary's sources have "
                "appeared for this property, so it is current as far as the index knows.\n")

    names = ", ".join(n for n, _ in rows[:4])
    more = f" (and {total - 4} more)" if total > 4 else ""
    return (
        f"\n*** POSSIBLY OUT OF DATE: {total} document(s) for this property have appeared "
        f"or changed since this summary's sources, which stop at {stamped:%Y-%m-%d}. "
        f"Newest: {names}{more}.\n"
        f"Durable facts here -- what was bought, when, the legal description, the flood zone "
        f"-- are still good. Treat STATUS answers (is it recorded / sold / paid / approved) "
        f"as possibly superseded: read the newer files named above before stating status as "
        f"fact, and tell the user the summary may be behind rather than answering as though "
        f"it were current.\n"
        f"Caveat worth stating if you rely on this: the date reflects when a file last "
        f"changed on disk, which OneDrive also updates on sync -- so this can overstate how "
        f"much is genuinely new. Judge from the filenames, not the count alone.\n"
        f"WANT TO FIX IT FOR EVERYONE? After answering, offer to bring the summary up to "
        f"date: read the most relevant newer files with read_document, then save what "
        f"changed with update_property_summary. Whoever accepts pays a few minutes of "
        f"reading once, and every teammate after them gets the current answer. ***\n"
    )


# ══════════════════════════════════════════════════════════════════
# Screening Source Resolver
# ══════════════════════════════════════════════════════════════════

def _resolve_costar_source(source_file: str, property_name: str = "", file_content_b64: str = "") -> "Path | None":
    """
    Resolves a CoStar export / broker spreadsheet to an on-disk path, in
    priority order:

      (a) file_content_b64 non-empty -- the user pasted/uploaded the file
          directly into the Claude conversation. Base64-decode it and write
          it into DROP_DIR, returning that path. Nothing watches DROP_DIR;
          it is just where CoStar exports live so they can be found by name.

      (b) else look for a filename matching source_file (case-insensitive)
          in the team's shared COSTAR_DROP_DIR first, then the local
          DROP_DIR, then the firm's document library, then the pre-rebuild
          watched_folder/processed trees if they still exist on this machine.
          Shared beats local so a stale local copy cannot shadow a newer
          export the team just published. If property_name is given, library
          results are narrowed to it first.

      (c) else return None.
    """
    import base64
    from config import (DROP_DIR, COSTAR_DROP_DIR,
                        LEGACY_WATCH_DIR, LEGACY_PROCESSED_DIR)

    if file_content_b64:
        try:
            # Pasted content lands in the LOCAL folder deliberately: one
            # person's paste shouldn't appear in the team's shared folder.
            DROP_DIR.mkdir(parents=True, exist_ok=True)
            dest = DROP_DIR / source_file
            dest.write_bytes(base64.b64decode(file_content_b64))
            return dest
        except Exception as e:
            log.warning(f"[MCP] Could not decode/write uploaded file content: {e}")
            return None

    target_lower = source_file.lower()

    # The team's shared "CoStar Drop" FIRST -- it is the folder people can
    # actually find, the one open_costar_folder opens, and the one a colleague
    # publishes a fresh export to. Local data/drop is the fallback, and exists
    # mainly because pasted content lands there.
    #
    # This order matters. Searching local first meant a stale copy left over on
    # one machine silently shadowed a newer file the team had just published --
    # same filename, older data, no warning, and every downstream number wrong.
    # Preferring the shared copy makes the team folder the source of truth.
    for drop in (COSTAR_DROP_DIR, DROP_DIR):
        try:
            if not drop.exists():
                continue
            for candidate in drop.rglob("*"):
                if candidate.is_file() and candidate.name.lower() == target_lower:
                    where = "shared CoStar Drop" if drop == COSTAR_DROP_DIR else "local data/drop"
                    log.info(f"[MCP] CoStar source resolved from the {where}: {candidate}")
                    return candidate
        except OSError as e:
            # An unreachable shared folder is a reason to try the next
            # location, not to fail the whole lookup.
            log.warning(f"[MCP] Could not read {drop}: {e}")

    # The document library. Uses the index rather than walking: the library
    # holds ~500k files across a OneDrive placeholder filesystem, so an rglob
    # over it would take minutes.
    try:
        from corpus import resolve_in_corpus, search
        for hit in search(source_file, limit=25, subtree=""):
            if hit["name"].lower() == target_lower:
                if property_name and property_name.lower() not in hit["path"].lower():
                    continue
                return resolve_in_corpus(hit["path"])
    except Exception as e:
        log.warning(f"[MCP] Could not search the document library for {source_file}: {e}")

    # Pre-rebuild locations, in case this machine still has exports filed
    # there from before the watcher was removed.
    for legacy_root in (LEGACY_WATCH_DIR, LEGACY_PROCESSED_DIR):
        if legacy_root.exists():
            for candidate in legacy_root.rglob("*"):
                if candidate.is_file() and candidate.name.lower() == target_lower:
                    return candidate

    return None


# ══════════════════════════════════════════════════════════════════
# MCP Tools
# ══════════════════════════════════════════════════════════════════

def create_mcp_server():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        name="Vaulter AI Property Intelligence",
        instructions="""Before doing anything else in a brand-new conversation, call
check_system_health() once to check whether this local instance is actually working
(document library synced and indexed, shared folder connectivity, active portfolio
file, code version). Do not call it again later in the same conversation.
If it comes back healthy, say nothing about it at all and just proceed with whatever the
user asked. Only mention it if it reports an actual problem -- then state the problem in
plain English and continue with the user's request anyway; never withhold help or delay a
task because of what this check finds.

If ANY vaulter_ai tool call -- including check_system_health itself -- errors, times out,
or hangs, that's a different situation from check_system_health reporting a data problem:
it means the connector itself may be broken, not just the data behind it. Don't just
surface the raw error and move on.

If a vaulter-connection-doctor subagent is available to you, invoke it automatically, without
waiting to be asked -- it's authorized to investigate, fix a real code bug it finds, and
re-verify. If you have no such subagent (Claude Desktop does not load a project's subagents
from disk -- only Claude Code does), do NOT pretend to call it. Instead say plainly what
failed and offer the two things that resolve most connector problems: fully quit and reopen
Claude Desktop, and if that doesn't help, double-click "Setup Vaulter AI" in the quick_start
folder again. Still don't let this block the user for long: once you know the outcome
(fixed / still broken / an environment issue like OneDrive not being signed in, which no
subagent can fix), state that plainly in one or two sentences and continue with whatever
the user actually asked.

WHAT THE TEAM CALLS THIS. People refer to the document library by many names:
"the shaw drive", "the shaw folder", "shaw", "the OneDrive", "the SharePoint",
"the firm's files", "the drive", or just "our documents". These all mean the same
thing — the library you already reach through search_documents, browse_documents
and read_document. Treat any of them as referring to these tools.

So if the user asks whether you are connected to the shaw drive (or any of those
names), the answer is YES and you should say so directly. Do NOT search the
connector directory for it — this system is a local MCP server on the user's own
computer, not an installable connector, so a connector search will always come back
empty and reads as "not connected" when in fact it is working. Answer from the tools
you already hold; if you want to confirm the library is synced and indexed, that is
what check_system_health already reported at the start of this conversation.

You have access to Vaulter's own document library — the firm's SharePoint files,
synced locally. This includes:
- The active property portfolio across Arizona, California, New Mexico, Colorado, and Texas
- Per-property files under !PROPERTIES/<STATE>/<Property>/ — due diligence, surveys,
  ALTA, title reports, correspondence
- Closing memos, entity records, and firm-wide templates
- Inbound CoStar exports and broker listing spreadsheets

ASKED ABOUT A SPECIFIC PROPERTY? CALL get_property_summary FIRST.
The team keeps one shared, cited summary per property. It costs a few hundred
tokens and usually answers the question outright, with file and page citations.
Going to the source documents instead costs tens of thousands of tokens to reach
the same answer, and every teammate would pay it again. These summaries do NOT
appear in search_documents (the shared folder is deliberately excluded from the
document index), so the tool is the only way to reach them -- do not conclude a
property has no summary because a search didn't surface one. If the summary
doesn't cover what was asked, its Gaps section names what was not read, which
tells you which original document to open.

HOW DOCUMENT SEARCH WORKS — this matters, and it is not a vector database.
search_documents matches file and folder NAMES, not the text inside documents.
The library holds roughly half a million files stored as download-on-demand
placeholders, so searching their contents would mean downloading all of them.
Work in two steps:
  1. search_documents (or browse_documents to explore the folder tree) to find
     candidate files. Names are informative — they usually carry a date, the
     counterparty, and the document kind.
  2. read_document on the specific files that look right, to get their text.
If a search comes back empty, that means no file NAME matched — not that the
firm has nothing on the subject. Try broader terms or browse the property folder.
Never tell the user the firm has no records on something based on an empty search.

For screening inbound listings from a CoStar export or broker spreadsheet,
use screen_listings. It RANKS every listing by fit against the firm's existing
portfolio and eliminates nothing — there is no pass/fail, and a weak listing
sinks to the bottom with a stated reason rather than disappearing. It makes no
API calls and costs nothing. There are three ways to give it a CoStar file,
and they are NOT equally cheap — prefer them in this order:

(1) BY FILENAME, strongly preferred. The file is already in the CoStar drop
folder or the document library; just pass source_file (plus property_name to
narrow a library search). Costs nothing — the file never travels through the
conversation. If the user has the file but hasn't put it anywhere, call
open_costar_folder, which opens the drop folder for them, and ask them to
drop it in and tell you the name. That short exchange is far cheaper than (2).

(2) BY PASTED CONTENT, only when (1) genuinely isn't available — e.g. the user
attached the file here and has no easy way to save it. Base64 content passed
as file_content_b64 is enormous: a real 216-row export measured ~43,000 tokens
JUST to hand the file over, before any analysis. Do not reach for this by
default merely because a file was attached, and never loop it over several
files without saying what it will cost and offering the drop folder instead.

(3) Neither — screen_listings explains how to supply the file.

Do the qualitative read yourself, here in the conversation, on the top few it
returns — that is what used to be a paid API call and is now just you.
verify_listings adds free federal ground truth (flood over the parcel area,
road access, incorporated status, terrain) on the top-ranked few.
open_screening_dashboard opens the single self-contained HTML report written
alongside the workbook: the shortlist, a map, and a click-through detail card
for every listing."""
    )

    @mcp.tool()
    def check_system_health() -> str:
        """
        Check whether this local Vaulter AI instance is actually working:
        whether the firm's document library is synced and indexed, whether
        the shared team folder is really connected (vs silently fallen back
        to local-only), which portfolio file is active, and the running code
        version. Never changes any of this instance's data or settings.

        This also does the once-a-day check for a published code update or
        org-wide setting, and stages anything it finds without applying it.
        That used to be a 5am scheduled job; there is no scheduler any more,
        and running it here means it happens roughly once per Claude Desktop
        session, which is the same cadence in practice.

        Call this once, automatically, at the very start of every new
        conversation, before anything else -- not on every message, and
        not again later in the same conversation. If everything comes
        back healthy, say NOTHING about it and just proceed with the
        user's actual request; a clean bill of health is not worth
        reporting. Only surface this if it finds an actual problem, and
        even then keep it brief and keep going -- state the issue in
        plain English, then continue with whatever the user asked. Never
        refuse or delay a task because of what this check finds; it is
        informational only.
        """
        import datetime as _dt
        import json as _json
        import time as _t

        # Timed for the same reason as screen_listings: this is the tool Claude
        # calls first in every conversation, so if it stalls, everything after
        # it looks like a dead server. It reaches the corpus index and the
        # shared OneDrive folder, either of which can block.
        _t0 = _t.perf_counter()
        log.info("[MCP] check_system_health: entered")

        issues = []
        lines = []

        # ── Document library ─────────────────────────────────────
        from config import CORPUS_DIR, CORPUS_AVAILABLE
        if not CORPUS_AVAILABLE:
            lines.append(f"Document library: NOT available (expected {CORPUS_DIR})")
            issues.append(
                "The firm's document library isn't synced on this machine, so document "
                "search and property lookups won't return anything. Check that OneDrive "
                "is signed in and syncing the 'Vaulter LLC - shaw' library."
            )
        else:
            lines.append(f"Document library: connected ({CORPUS_DIR.name})")
            try:
                from corpus import index_age
                age = index_age()
                if age is None:
                    lines.append("  Index: not built yet")
                    issues.append(
                        "The document index hasn't been built, so search can't run. "
                        "It takes a few minutes and only reads filenames, never file "
                        "contents. Easiest fix: double-click \"Setup Vaulter AI\" in "
                        "the quick_start folder. From a terminal: "
                        "python system/main.py index-corpus"
                    )
                else:
                    count, built = age
                    days = (_dt.datetime.now(_dt.timezone.utc) - built).days
                    lines.append(f"  Index: {count:,} files, built {days}d ago")
                    if days > 30:
                        issues.append(
                            f"The document index is {days} days old, so anything filed since "
                            "then won't turn up in search. Rebuild by double-clicking "
                            "\"Setup Vaulter AI\" in the quick_start folder, or from a "
                            "terminal: python system/main.py index-corpus"
                        )
            except Exception as e:
                lines.append(f"  Index: could not check ({e})")

        # ── Staged update / settings check ───────────────────────
        # Was a 5am scheduled job; see this tool's docstring. Both of these
        # only ever download and stage -- neither applies anything.
        for _stage_check in (_check_and_stage_update, _check_and_stage_org_settings):
            _ts = _t.perf_counter()
            try:
                _stage_check()
            except Exception as e:
                log.warning(f"[HEALTH] Staging check failed (continuing): {e}")
            took = _t.perf_counter() - _ts
            # These read the shared OneDrive folder. A cloud-only file can block
            # for minutes while OneDrive fetches it, and that delay lands on the
            # first tool call of the conversation.
            if took > 5:
                log.warning(f"[HEALTH] {_stage_check.__name__} took {took:.0f}s "
                            f"— shared OneDrive folder is slow to read")

        # ── Shared folder ────────────────────────────────────────
        from config import SHARED_DIR, SHARED_DIR_IS_FALLBACK
        if SHARED_DIR_IS_FALLBACK or not SHARED_DIR.exists():
            lines.append(f"Shared folder: NOT connected -- using local fallback ({SHARED_DIR})")
            issues.append(
                "The shared OneDrive folder isn't connected, so screening results and other "
                "team-shared data are only being saved locally, not shared with the team. "
                "Check that OneDrive is signed in and syncing."
            )
        else:
            # "The folder exists" is NOT the same as "you can see the team's
            # copy of it." Vaulter AI Shared is an ordinary folder inside one
            # person's OneDrive, not a synced SharePoint library like the
            # document library is -- so on a machine it hasn't been shared
            # with, config.py's mkdir simply CREATES a private empty one and
            # everything downstream looks connected while being completely
            # isolated. That is exactly the silent-empty-answer failure this
            # project distrusts everywhere else, so name it rather than let a
            # teammate discover it by wondering where the portfolio went.
            try:
                has_anything = any(SHARED_DIR.rglob("*.*"))
            except OSError:
                has_anything = True  # unreadable is a different problem; don't guess
            if has_anything:
                lines.append(f"Shared folder: connected ({SHARED_DIR})")
            else:
                lines.append(f"Shared folder: present but EMPTY ({SHARED_DIR})")
                issues.append(
                    "The shared folder exists on this machine but has nothing in it. If "
                    "teammates have data there, this is probably a private empty folder "
                    "that got created automatically, not the team's shared one — which "
                    "would explain missing portfolio data and no shared CoStar exports. "
                    "Ask whoever set up Vaulter AI to share the 'Vaulter AI Shared' "
                    "folder with you, then use OneDrive's \"Add shortcut to My files\" so "
                    "it appears in the same place. (If you're the first person setting "
                    "this up, an empty folder is expected and this will resolve itself.)"
                )

        # ── Portfolio file ───────────────────────────────────────
        try:
            from portfolio import find_project_file
            pfile = find_project_file()
            if pfile:
                mtime = _dt.datetime.fromtimestamp(pfile.stat().st_mtime)
                # Name which copy won. With two possible locations a stale local
                # file silently beating a fresh team one is exactly the confusion
                # worth spending a few words to prevent.
                from config import SMARTSHEET_PORTFOLIO_DIR
                origin = ("shared with the team"
                          if pfile.parent == SMARTSHEET_PORTFOLIO_DIR
                          else "this machine only")
                lines.append(f"Portfolio file: {pfile.name} (dated {mtime:%Y-%m-%d}, {origin})")
            else:
                lines.append("Portfolio file: none found")
                issues.append(
                    "No Project Master file has been published to the team's shared "
                    "'Smartsheet Portfolio' folder, and this machine has no local copy -- "
                    "so property lookups are using only the built-in fallback list. "
                    "Whoever maintains the Smartsheet export should drop it in that "
                    "shared folder once, which fixes it for everyone."
                )
        except Exception as e:
            lines.append(f"Portfolio file: could not check ({e})")

        # ── Property summaries ───────────────────────────────────
        # A newly-acquired property has no summary until someone asks about
        # it and Claude builds one -- deliberately lazy, see
        # PROPERTY_SUMMARIES_DIR's own design note. But nothing previously
        # NOTICED a gap existed at all, so a new acquisition could sit
        # invisible indefinitely if nobody happened to ask. This only
        # detects and names the gap; it never writes a summary itself --
        # that stays a human-in-the-loop, reviewed-in-conversation action,
        # same as it's always been.
        try:
            from portfolio import load_properties
            from config import PROPERTY_SUMMARIES_DIR
            import re as _re

            def _norm(s):
                return _re.sub(r"[^a-z0-9]", "", s.lower())

            active, _sold = load_properties()
            existing = [p.stem for p in Path(PROPERTY_SUMMARIES_DIR).glob("*.md")]
            existing_norm = [_norm(e) for e in existing]
            # Substring match, not exact -- Project Master names often carry
            # a parenthetical alias or slash-suffix the summary's own
            # filename dropped (e.g. a property name with a parenthetical
            # alias vs. a summary filename that dropped it), so exact match
            # alone flagged real, already-summarized properties as missing.
            # Verified against the live 49-property Project Master: zero
            # false positives.
            # Known tradeoff, deliberately accepted: two properties sharing
            # a name stem (e.g. a property name and a longer-named later-
            # phase sibling sharing the same stem) can mask each other here
            # if only the shorter-named one has been summarized -- a false
            # negative, not a false
            # positive. Chosen on purpose: a wrong "you're missing this"
            # claim damages trust in a tool built to stay silent unless
            # something is actually wrong; an occasional missed detection
            # in this one narrow case is the safer failure direction, and
            # asking about that property directly still works exactly as
            # it always has.
            no_summary = [
                p["name"] for p in active
                if not any(en in _norm(p["name"]) or _norm(p["name"]) in en
                            for en in existing_norm)
            ]
            if no_summary:
                names = ", ".join(no_summary[:5])
                more = f" (+{len(no_summary) - 5} more)" if len(no_summary) > 5 else ""
                lines.append(f"Property summaries: {len(no_summary)} propert"
                              f"{'y' if len(no_summary) == 1 else 'ies'} with none yet: "
                              f"{names}{more}")
                issues.append(
                    f"{len(no_summary)} propert{'y' if len(no_summary) == 1 else 'ies'} in the "
                    f"Project Master ha{'s' if len(no_summary) == 1 else 've'} no shared summary "
                    f"yet: {names}{more}. This is expected for a brand-new acquisition -- summaries "
                    f"are only built the first time someone asks about a property. If this looks "
                    f"like a real gap, offer to build one now (the same way a summary always gets "
                    f"created): read that property's documents and save a summary."
                )
        except Exception as e:
            lines.append(f"Property summaries: could not check ({e})")

        # ── Version ──────────────────────────────────────────────
        _v = _get_code_version()
        lines.append(f"Code version: {_v}")
        log.info(f"[MCP] check_system_health: done in {_t.perf_counter()-_t0:.1f}s")

        try:
            from config import PENDING_UPDATE_DIR
            ready_path = PENDING_UPDATE_DIR / "ready.json"
            if ready_path.exists():
                staged = _json.loads(ready_path.read_text())
                notes = f" — {staged['notes']}" if staged.get("notes") else ""
                lines.append(f"  A new version ({staged.get('version')}) is downloaded and "
                              f"ready{notes}. Ask the user if they'd like it installed now; if "
                              f"so, call apply_pending_update (this is never done "
                              f"automatically without asking first).")
                issues.append(
                    f"A new version ({staged.get('version')}) is ready — ask the user if "
                    f"they'd like it applied now, and if so, call apply_pending_update."
                )
        except Exception:
            pass  # update-check reporting is a nicety, never worth failing the whole check over

        # ── Pending org-wide settings ─────────────────────────────
        # Deliberately generic -- never names the specific key or
        # mentions any value here. This text is what gets sent back
        # into the conversation, so it must never contain anything
        # that would reveal what the setting is, let alone its value.
        try:
            from config import PENDING_SETTINGS_DIR
            staged_path = PENDING_SETTINGS_DIR / "staged.json"
            if staged_path.exists():
                pending = _json.loads(staged_path.read_text())
                if pending:
                    count = len(pending)
                    phrase = "A new feature is" if count == 1 else f"{count} new features are"
                    lines.append(f"  {phrase} ready to set up. Ask the user if they'd like you "
                                  f"to set it up for them; if so, call apply_pending_settings "
                                  f"(this is never done automatically without asking first).")
                    issues.append(
                        f"{phrase} ready to set up — ask the user if they'd like you to set it "
                        f"up for them, and if so, call apply_pending_settings."
                    )
        except Exception:
            pass  # same nicety as above -- never worth failing the whole check over

        summary = "HEALTHY" if not issues else f"{len(issues)} ISSUE(S) FOUND"
        body = "\n".join(lines)
        if issues:
            issue_block = "\n".join(f"  - {i}" for i in issues)
            return f"Vaulter AI health check — {summary}\n\n{body}\n\nISSUES:\n{issue_block}"
        return f"Vaulter AI health check — {summary}\n\n{body}"

    @mcp.tool()
    def apply_pending_update() -> str:
        """
        Applies a Vaulter AI code update that check_system_health has
        reported as staged and ready: syncs the new version's files into
        place, re-installs any new/changed Python dependencies, and
        clears the staged update. Never touches confidentials/ or any
        local data (screening results, ingested documents, etc.).

        IMPORTANT -- only call this after the user has explicitly agreed
        to apply the update IN THIS CONVERSATION (e.g. you mentioned one
        is ready and they said something like "yes, go ahead" or "install
        it"). Never call this proactively, speculatively, or without an
        explicit go-ahead just given -- applying a code update is exactly
        the kind of action that needs a deliberate human decision, not an
        assumption, even though it's entirely safe to run.

        After this succeeds, tell the user to fully quit and reopen
        Claude Desktop -- the new code only takes effect on the next
        launch, never while this server is still running.
        """
        try:
            from scripts import apply_update
            result = apply_update.apply_pending_update()
            if not result["applied"]:
                return f"Nothing to apply: {result['reason']}"

            lines = [
                f"Applied version {result['version']}: {result['files_updated']} file(s) "
                f"updated, {result['files_deleted']} removed.",
            ]
            if not result["dependencies_ok"]:
                lines.append(f"Note: refreshing Python dependencies hit a problem: "
                              f"{result['dependencies_message']}")
            lines.append("")
            lines.append("Tell the user to fully quit and reopen Claude Desktop now — the new "
                          "code only takes effect on the next launch.")
            return "\n".join(lines)
        except Exception as e:
            log.error(f"[MCP] apply_pending_update failed: {e}", exc_info=True)
            return f"Could not apply the update: {e}"

    @mcp.tool()
    def get_pending_setup_details() -> str:
        """
        Get plain-English descriptions of any pending features that are
        ready to set up. Use this when the user asks "what feature is
        that?" or "what will this set up?" -- call this tool, then
        explain the result to the user in simple language.

        Never mention the specific setting name or any value; just
        describe what the feature does, in non-technical terms that a
        non-developer would understand.
        """
        try:
            import json
            from config import PENDING_SETTINGS_DIR

            staged_path = PENDING_SETTINGS_DIR / "staged.json"
            if not staged_path.exists():
                return "No new features are currently waiting to be set up."
            pending = json.loads(staged_path.read_text())
            if not pending:
                return "No new features are currently waiting to be set up."

            descriptions = []
            for i, entry in enumerate(pending, 1):
                label = entry.get("label", "a new feature")
                descriptions.append(label)

            if len(descriptions) == 1:
                return descriptions[0]
            else:
                return "New features: " + ", ".join(descriptions)
        except Exception as e:
            log.error(f"[MCP] get_pending_setup_details failed: {e}", exc_info=True)
            return "Could not retrieve feature details."

    @mcp.tool()
    def apply_pending_settings() -> str:
        """
        Sets up a new feature that check_system_health has reported as
        ready: writes a pending org-wide value (e.g. a new feature's API
        key, distributed by the maintainer) directly into this
        machine's own confidentials/.env. Never touches anything else.

        IMPORTANT -- only call this after the user has explicitly agreed
        IN THIS CONVERSATION (e.g. they said "yes, set it up" or "go
        ahead"). Never call this proactively or speculatively.

        IMPORTANT -- this tool's result, and anything you say about it,
        must stay completely generic. Never mention the specific setting
        name, or echo any value, in your reply to the user -- just
        confirm it's done and that they should restart Claude Desktop.
        The whole point of this tool is that the actual value is written
        directly to disk in Python, without you ever needing to see or
        repeat it.
        """
        try:
            import json
            from dotenv import set_key
            from config import PENDING_SETTINGS_DIR, SECRETS_DIR

            staged_path = PENDING_SETTINGS_DIR / "staged.json"
            if not staged_path.exists():
                return "Nothing to set up right now."
            pending = json.loads(staged_path.read_text())
            if not pending:
                return "Nothing to set up right now."

            env_path = SECRETS_DIR / ".env"
            for entry in pending:
                set_key(str(env_path), entry["key"], entry["value"])

            count = len(pending)
            staged_path.unlink(missing_ok=True)
            log.info(f"[ORG_SETTINGS] Configured {count} pending setting(s).")

            noun = "feature" if count == 1 else "features"
            return (f"Done — {count} new {noun} set up. Tell the user to fully quit and reopen "
                    f"Claude Desktop now for it to take effect. Do not mention any setting name "
                    f"or value in your reply — just confirm it's set up.")
        except Exception as e:
            log.error(f"[MCP] apply_pending_settings failed: {e}", exc_info=True)
            return "Could not finish setting this up — check the local logs for details."

    def _format_hits(hits: list, header: str) -> str:
        """Render search hits as a compact table Claude can pick from."""
        import datetime as _dt
        lines = [header, ""]
        for hit in hits:
            when = _dt.datetime.fromtimestamp(hit["mtime"]).strftime("%Y-%m-%d")
            size = hit["size"]
            size_str = f"{size / 1_048_576:.1f}MB" if size >= 1_048_576 else f"{size // 1024}KB"
            lines.append(f"{hit['path']}")
            lines.append(f"    {when} · {size_str}")
        lines.append("")
        lines.append(
            "To read one, call read_document with its path exactly as shown above."
        )
        return "\n".join(lines)

    @mcp.tool()
    def search_documents(query: str, n_results: int = 25, folder: str = "") -> str:
        """
        Find documents in the firm's document library by name and folder path.

        IMPORTANT — this searches file names and folder paths, NOT the text
        inside documents. The library is synced from SharePoint as
        download-on-demand placeholders, so reading every file to search its
        contents would download the entire library. Names carry a lot here:
        files are named like "220419 Neighboring Hotel Public Hearing
        Notice.pdf" inside "!PROPERTIES/ARIZONA/<Property>/".

        So: search broadly by property, counterparty, date, or document kind,
        then call read_document on the specific results that look right.

        Args:
            query: space-separated terms; ALL must appear in the path or name
            n_results: maximum results (default 25)
            folder: optional folder to restrict to, e.g. "!PROPERTIES/ARIZONA"
        """
        try:
            from corpus import search
            hits = search(query, limit=min(max(1, n_results), 100), subtree=folder)
            if not hits:
                where = f" under {folder}" if folder else ""
                return (
                    f"No documents matched '{query}'{where}.\n\n"
                    "Remember this matches file and folder NAMES, not document text. "
                    "Try fewer or broader terms, or use browse_documents to look "
                    "around the folder structure."
                )
            return _format_hits(hits, f"{len(hits)} document(s) matching '{query}':")
        except Exception as e:
            return f"Search failed: {e}"

    @mcp.tool()
    def read_document(path: str, max_chars: int = 200000) -> str:
        """
        Read one document out of the firm's document library and return its text.

        Handles PDF (with OCR for scanned pages), Word, Excel, CSV, and text.
        Takes a path exactly as returned by search_documents or browse_documents.

        Note this may take a few seconds: most files are stored in the cloud
        and get downloaded on first read.

        Args:
            path: document path relative to the library root
            max_chars: truncate beyond this (default 200,000)
        """
        try:
            from corpus import read_document as _read
            text, meta = _read(path, max_chars=max_chars)
            header = [f"=== {meta['filename']} ==="]
            header.append(f"Path: {meta['path']}")
            header.append(f"Modified: {meta['modified']}")
            if meta.get("page_count"):
                header.append(f"Pages/sheets: {meta['page_count']}")
            if meta.get("ocr_used"):
                header.append("Note: some pages were scanned images and were read via OCR, "
                              "so the text may contain recognition errors.")
            if meta.get("truncated"):
                header.append(f"Note: truncated — this document is {meta['full_length']:,} characters.")
            return "\n".join(header) + "\n\n" + text
        except Exception as e:
            return f"Could not read '{path}': {e}"

    @mcp.tool()
    def browse_documents(folder: str = "") -> str:
        """
        List the contents of one folder in the firm's document library.

        Use this to orient yourself when a search comes back empty, or to see
        what exists for a property. Pass "" for the top level.

        Args:
            folder: folder path relative to the library root,
                    e.g. "!PROPERTIES/ARIZONA"
        """
        try:
            from corpus import list_dir
            listing = list_dir(folder)
            where = listing["path"] or "(library root)"
            lines = [f"=== {where} ==="]
            if listing["folders"]:
                lines.append("")
                lines.append("Folders:")
                lines += [f"  {name}/" for name in listing["folders"]]
            if listing["files"]:
                lines.append("")
                lines.append("Files:")
                lines += [f"  {f['name']}" for f in listing["files"]]
            if not listing["folders"] and not listing["files"]:
                lines.append("(empty)")
            return "\n".join(lines)
        except Exception as e:
            return f"Could not list '{folder}': {e}"

    @mcp.tool()
    def get_property_info(property_name: str) -> str:
        """
        Find the documents held for a specific property.

        Returns the property's Project Master record plus the documents filed
        under it. Read the ones that look relevant with read_document.

        Args:
            property_name: Property name, as it appears in the Project Master
        """
        if not property_name.strip():
            return "Which property? Please give me a property name."
        try:
            from corpus import search

            details = ""
            try:
                from portfolio import load_properties
                props, _ = load_properties()
                match = next(
                    (p for p in props if property_name.lower() in p["name"].lower()), None
                )
                if match:
                    details = (
                        f"{match['name']} — {match['category']}, "
                        f"{match.get('city', '')}, {match['state']}\n\n"
                    )
            except Exception as e:
                log.warning(f"[MCP] Portfolio lookup failed for {property_name}: {e}")

            hits = search(property_name, limit=40)
            if not hits:
                return (
                    f"{details}No documents found for '{property_name}'.\n\n"
                    "The name may be filed differently in the library — try "
                    "browse_documents on the state folder under !PROPERTIES."
                )
            return details + _format_hits(hits, f"{len(hits)} document(s) for '{property_name}':")
        except Exception as e:
            return f"Property lookup failed: {e}"

    @mcp.tool()
    def get_property_summary(property_name: str) -> str:
        """
        Read the team's existing cited summary for a property, if one exists.

        ALWAYS TRY THIS FIRST when asked anything about a specific property.
        It is a few hundred tokens and answers most questions outright, with
        file+page citations. Reading the underlying documents instead costs
        tens of thousands of tokens for the same answer.

        These summaries are shared with the whole team, so one person's
        reading has already been paid for on everyone's behalf. They are not
        reachable through search_documents (the shared folder is deliberately
        excluded from the document index so this system's own output can never
        surface as a firm document), which is exactly why this tool exists.

        Each summary carries its own Gaps section naming what was NOT read. If
        the answer isn't in the summary, use that section to pick which
        original document to open with read_document -- it tells you where to
        look instead of searching blind.

        Args:
            property_name: Property name, as it appears in the Project Master
        """
        if not property_name.strip():
            return "Which property? Please give me a property name."
        try:
            import re
            from config import PROPERTY_SUMMARIES_DIR

            summaries_dir = Path(PROPERTY_SUMMARIES_DIR)
            if not summaries_dir.is_dir():
                return ("No shared summaries folder found on this machine. Check that "
                        "OneDrive is syncing, then read documents directly instead.")

            def _slug(name: str) -> str:
                return re.sub(r"[^a-z0-9]+", "-", name.lower().strip()).strip("-")

            wanted = _slug(property_name)
            available = sorted(summaries_dir.glob("*.md"))

            match = next((p for p in available if p.stem == wanted), None)
            if match is None:
                # Fall back to a containment match so a short property name
                # still finds a file slugged from a longer folder-style
                # name, and a partial name the user typed still lands.
                candidates = [p for p in available
                              if wanted in p.stem or p.stem in wanted]
                if len(candidates) == 1:
                    match = candidates[0]
                elif len(candidates) > 1:
                    names = ", ".join(p.stem for p in candidates)
                    return (f"Several summaries could match '{property_name}': {names}. "
                            f"Ask again with the exact name.")

            if match is None:
                have = ", ".join(p.stem for p in available) or "none yet"
                return (
                    f"No summary has been written for '{property_name}' yet.\n\n"
                    f"Read its documents directly (get_property_info then read_document) "
                    f"to answer the question. Summaries that DO exist: {have}"
                )

            text = match.read_text(encoding="utf-8", errors="replace")
            staleness = _summary_staleness(property_name, text)
            return (
                f"Shared summary for '{property_name}' ({match.name}).\n"
                f"Every finding below is cited to a source document and page. If what you "
                f"need isn't here, check the Gaps section at the end -- it names what was "
                f"not read, so you know which original document to open.\n"
                f"{staleness}\n{text}"
            )
        except Exception as e:
            return f"Could not read the property summary: {e}"

    @mcp.tool()
    def update_property_summary(property_name: str, update_text: str,
                                sources_as_of: str = "") -> str:
        """
        Add a dated update to a property's shared summary, after reading
        newer documents that appeared since it was written.

        Use this to close the loop when get_property_summary warned the
        summary was possibly out of date and you then read the newer files:
        write what CHANGED as a short, cited update. Whoever does this pays
        the reading cost once; every teammate after them gets the current
        picture.

        Rules for the update text, matching how the summaries are written:
        - Only what changed or is new -- do not restate the original.
        - Cite every finding: "-- <filename>, p.<page>". No citation, no claim.
        - If a newer document supersedes something (a plat recorded, a sale
          closed), say so explicitly rather than leaving the two to conflict.
        - Say what you did NOT read, if you skipped some of the newer files.

        This APPENDS a dated section to the end of the file. It never edits
        or deletes the original text, so a bad update can't destroy the
        summary -- the worst case is an extra section a human can review.

        Args:
            property_name: Property name, as in the Project Master
            update_text:   The cited findings, markdown, no heading needed
            sources_as_of: YYYY-MM-DD of the newest document you read. Sets
                           the summary's freshness stamp so the out-of-date
                           warning stops firing for files you already covered.
        """
        import re
        import datetime as _dt
        try:
            from config import PROPERTY_SUMMARIES_DIR

            if not property_name.strip():
                return "Which property? Please give me a property name."
            if not update_text.strip():
                return "The update text is empty -- nothing was saved."

            def _slug(name: str) -> str:
                return re.sub(r"[^a-z0-9]+", "-", name.lower().strip()).strip("-")

            summaries_dir = Path(PROPERTY_SUMMARIES_DIR)
            wanted = _slug(property_name)
            available = sorted(summaries_dir.glob("*.md")) if summaries_dir.is_dir() else []
            match = next((p for p in available if p.stem == wanted), None)
            if match is None:
                candidates = [p for p in available if wanted in p.stem or p.stem in wanted]
                if len(candidates) == 1:
                    match = candidates[0]
                elif len(candidates) > 1:
                    return (f"Several summaries could match '{property_name}': "
                            f"{', '.join(p.stem for p in candidates)}. Use the exact name.")
            if match is None:
                return (f"No summary exists for '{property_name}' yet, so there is nothing "
                        f"to update. A first summary needs the full treatment -- reading the "
                        f"key documents, not just the newest ones -- so say so to the user "
                        f"rather than writing a partial one here.")

            stamp_new = ""
            if sources_as_of.strip():
                try:
                    _dt.date.fromisoformat(sources_as_of.strip())
                    stamp_new = sources_as_of.strip()
                except ValueError:
                    return (f"'{sources_as_of}' isn't a date in YYYY-MM-DD form -- nothing "
                            f"was saved. Pass the newest document date you actually read.")

            text = match.read_text(encoding="utf-8", errors="replace")
            today = _dt.date.today().isoformat()
            section = (f"\n\n## Update {today} — from documents filed since the last read\n\n"
                       f"{update_text.strip()}\n")

            # Append first, bump the freshness stamp second -- the stamp is
            # what silences the out-of-date warning, so it must only ever
            # advance once the update is actually in the file.
            new_text = text.rstrip("\n") + "\n" + section
            if stamp_new:
                new_text = re.sub(
                    r"(Source files as of:\*{0,2}\s*)\d{4}-\d{2}-\d{2}",
                    rf"\g<1>{stamp_new}",
                    new_text, count=1,
                )
            match.write_text(new_text, encoding="utf-8")

            log.info(f"[MCP] Summary updated: {match.name} "
                     f"(+{len(update_text):,} chars, sources_as_of={stamp_new or 'unchanged'})")
            return (f"Saved to {match.name} as a dated update section"
                    + (f", and its freshness stamp now reads {stamp_new}" if stamp_new else "")
                    + ". The whole team sees this the next time anyone asks about "
                      "this property.")
        except Exception as e:
            return f"Could not update the summary: {e}"

    @mcp.tool()
    def get_portfolio_list(group_by: str = "state") -> str:
        """
        Get the complete list of all active Vaulter AI properties.
        Args:
            group_by: "state" or "stage" (default: "state")
        """
        try:
            from portfolio import load_properties
            props, _ = load_properties()
            # Anything other than "stage" used to fall through to grouping by
            # state while the heading still echoed whatever was asked for, so
            # a typo produced a list headed "by adress" that was not by
            # address at all. Say what actually happened instead.
            if group_by not in ("state", "stage"):
                group_by = "state"
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
            from portfolio import load_properties
            props, _ = load_properties()
            filtered = [p for p in props if p.get("category", "").lower() == stage.lower()]
            if not filtered:
                # Name the stages that do exist. The stage labels come from the
                # Project Master and are not fixed, so a caller guessing from
                # the tool description can otherwise get nothing with no way of
                # telling whether the stage is empty or simply spelled
                # differently in this export.
                have = sorted({p.get("category", "") for p in props if p.get("category")})
                return (f"No active properties are in a stage called '{stage}'.\n\n"
                        f"The stages in the current Project Master are: {', '.join(have)}.")
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
            property_name: Property name, as it appears in the Project Master
        """
        from config import CORPUS_DIR, CORPUS_AVAILABLE
        try:
            if not property_name.strip():
                # An empty string is a substring of every folder name, so
                # without this check every property folder would "match"
                # and the code below would silently open an arbitrary one
                # (whichever iterdir() happened to list first).
                return "Which property? Please tell me the property name to open its files."

            if not CORPUS_AVAILABLE:
                return ("The firm's document library isn't synced on this machine, so there's "
                        "no folder to open. Check that OneDrive is syncing 'Vaulter LLC - shaw'.")

            matches = _find_property_folders(property_name)

            if matches:
                exact = [m for m in matches if m.name.lower() == property_name.lower()]
                folder = exact[0] if exact else matches[0]
            else:
                folder = None

            if folder:
                _open_in_file_manager(folder)
                extra = ""
                if len(matches) > 1:
                    others = ", ".join(m.name for m in matches if m is not folder)
                    extra = f"\n\nOther folders also matched: {others}"
                return f"Opened File Explorer to {folder.name}.{extra}"

            properties_root = CORPUS_DIR / "!PROPERTIES"
            _open_in_file_manager(properties_root if properties_root.is_dir() else CORPUS_DIR)
            return (f"No folder found for '{property_name}'. Opened the properties folder "
                    f"instead — the name may be filed differently.")

        except Exception as e:
            return f"Could not open folder: {e}"

    @mcp.tool()
    def open_property_document(property_name: str, filename: str) -> str:
        """
        Open File Explorer directly to ONE specific document for a property,
        with that exact file highlighted -- not just the property's folder.

        Use this when the user asks for a SPECIFIC document by name or by what
        a property summary cited it as -- "open that escrow agreement", "show
        me the deed", "pull up the Phase I ESA" -- rather than a general "show
        me the files for X" (use open_property_files for that instead).

        Property folders can hold thousands of files, and the same or a
        near-identical filename often appears more than once in different
        subfolders (seen repeatedly in this library: duplicate copies, the
        same memo filed under both Acquisition and Disposition, etc.) --
        finding one match is the common case, not a rare one. So this tool
        NEVER guesses between multiple matches: if more than one file in the
        property's folder matches, it lists every one (with its folder, so
        they can be told apart) and asks which was meant, rather than
        opening one and risking it being the wrong copy.

        Args:
            property_name: Property name, as it appears in the Project Master
            filename: The filename, or a distinctive part of it, as cited in
                      a property summary (e.g. from a "-- filename, p.N" citation)
        """
        from config import CORPUS_DIR, CORPUS_AVAILABLE
        try:
            if not property_name.strip():
                return "Which property? Please tell me the property name."
            if not filename.strip():
                return "Which file? Please tell me the filename, or the name cited in the summary."

            if not CORPUS_AVAILABLE:
                return ("The firm's document library isn't synced on this machine, so there's "
                        "no file to open. Check that OneDrive is syncing 'Vaulter LLC - shaw'.")

            folders = _find_property_folders(property_name)
            if not folders:
                return (f"No folder found for '{property_name}', so there's nothing to search "
                        f"for '{filename}' in. The property name may be filed differently -- "
                        f"try open_property_files first to see what folders exist.")

            exact = [f for f in folders if f.name.lower() == property_name.lower()]
            folder = exact[0] if exact else folders[0]
            folder_note = ""
            if len(folders) > 1:
                others = ", ".join(f.name for f in folders if f is not folder)
                folder_note = f" (other property folders also matched '{property_name}': {others})"

            # Search the chosen property's own folder only -- never the wider
            # corpus. Exact filename match first (the normal case: a citation
            # is usually the real filename verbatim); fall back to a substring
            # match so a shortened or reworded reference still finds it.
            wanted = filename.strip().lower()
            all_files = [p for p in folder.rglob("*") if p.is_file()]
            exact_hits = [p for p in all_files if p.name.lower() == wanted]
            hits = exact_hits or [p for p in all_files if wanted in p.name.lower()]

            if not hits:
                return (f"No file matching '{filename}' found anywhere in {folder.name}'s "
                        f"folder{folder_note}. It may be named differently than cited, or in "
                        f"a format this search didn't catch -- try open_property_files to "
                        f"browse the folder directly.")

            if len(hits) == 1:
                _open_in_file_manager(hits[0], select=True)
                rel = hits[0].relative_to(folder)
                return f"Opened {hits[0].name} in {folder.name}'s folder ({rel}).{folder_note}"

            # More than one match: list every one rather than guessing. Cap
            # the list so a too-broad fragment (e.g. a single common word)
            # doesn't dump an unreadable wall of paths.
            hits.sort(key=lambda p: str(p.relative_to(folder)))
            shown = hits[:15]
            lines = [f"{len(hits)} files in {folder.name}'s folder match '{filename}'{folder_note} "
                     f"-- which one did you mean?\n"]
            for p in shown:
                lines.append(f"  - {p.relative_to(folder)}")
            if len(hits) > len(shown):
                lines.append(f"  ...and {len(hits) - len(shown)} more.")
            return "\n".join(lines)

        except Exception as e:
            return f"Could not open document: {e}"

    @mcp.tool()
    def open_costar_folder() -> str:
        """
        Open File Explorer to the folder where CoStar exports and broker
        spreadsheets get dropped for screening.

        Use this when the user wants to add a listing export to screen, asks
        where to put one, or has a file they'd otherwise attach to the
        conversation -- opening this folder and screening by filename costs
        nothing, where pasting the file's contents costs tens of thousands of
        tokens on a real export.

        Opens the TEAM's shared "CoStar Drop" folder in OneDrive: it's easy to
        find beside the other Vaulter AI folders, and anything dropped there is
        screenable by every teammate without re-sending it.
        """
        from config import COSTAR_DROP_DIR, DROP_DIR
        try:
            COSTAR_DROP_DIR.mkdir(parents=True, exist_ok=True)
            _open_in_file_manager(COSTAR_DROP_DIR)

            # Both are searched when screening, so list both -- otherwise a file
            # sitting in the old local folder looks like it isn't there.
            shared = sorted(f for f in COSTAR_DROP_DIR.iterdir() if f.is_file())
            local = []
            if DROP_DIR.exists() and DROP_DIR.resolve() != COSTAR_DROP_DIR.resolve():
                local = sorted(f for f in DROP_DIR.iterdir() if f.is_file())

            if not shared and not local:
                return ("Opened the team's CoStar Drop folder in OneDrive — it's empty.\n\n"
                        "Drop a CoStar export or broker spreadsheet in, then tell me its "
                        "filename and I'll screen it. (Dropping the file here is free; "
                        "attaching it to this conversation instead costs a lot, since the "
                        "whole file has to be sent through the chat.)")

            out = ["Opened the team's CoStar Drop folder in OneDrive."]
            if shared:
                out.append("\nReady to screen (shared with the team):")
                out += [f"  - {f.name}" for f in shared]
            if local:
                out.append("\nAlso screenable, on this machine only:")
                out += [f"  - {f.name}" for f in local]
            out.append("\nTell me a filename and I'll screen it.")
            return "\n".join(out)
        except Exception as e:
            return f"Could not open folder: {e}"

    @mcp.tool()
    def open_proximity_files(property_name: str = "") -> str:
        """
        Open File Explorer to the proximity output folder, with the most recent
        file for the property selected so the user lands right on their export.
        Use this when the user says ANYTHING like:
        - "pull it up", "show me", "open it", "can you open that" — after a proximity export was run
        - "open the proximity files", "show me the CSV", "pull up the spreadsheet"
        - "open the export", "where is the proximity output", "show me the results"
        - any casual request to view or open proximity export files
        When a proximity export was recently run and the user wants to see/open the output, use this tool.
        Args:
            property_name: Property name to find matching files, as in the Project Master
        """
        from config import PROXIMITY_OUTPUT_DIR
        try:
            proximity_dir = PROXIMITY_OUTPUT_DIR
            proximity_dir.mkdir(parents=True, exist_ok=True)
            all_files = sorted([f for f in proximity_dir.iterdir() if f.is_file()], reverse=True)
            if not all_files:
                _open_in_file_manager(proximity_dir)
                return ("Opened proximity output folder — no exports yet. Run "
                        "run_proximity_for_property or run_proximity_for_listing first.")

            # Find files matching this property
            if property_name:
                words = [w for w in property_name.lower().split() if len(w) > 3]
                matching = [f for f in all_files if any(w in f.name.lower() for w in words)]
            else:
                matching = all_files

            target = matching[0] if matching else all_files[0]
            _open_in_file_manager(target, select=True)

            display = matching if matching else all_files
            file_list = "\n".join(f"  - {f.name}" for f in display[:10])
            # Asking for a property with no export used to open, and announce,
            # the newest file belonging to some OTHER property, with nothing to
            # say the request had not been met. Falling back is still the right
            # behaviour -- there is something to look at -- but it has to be
            # named as a fallback.
            if property_name and not matching:
                return (f"No proximity export found for '{property_name}'. Opened the folder "
                        f"showing the most recent exports instead — {target.name} is selected, "
                        f"and it is NOT for that property.\n\nMost recent exports:\n{file_list}"
                        f"\n\nTo produce one for '{property_name}', call "
                        f"run_proximity_for_property.")
            return f"Opened File Explorer to proximity output — {target.name} selected.\n\nFiles:\n{file_list}"
        except Exception as e:
            return f"Could not open proximity folder: {e}"

    # ── LISTING SCREENER (fit ranking; the 4-phase pipeline is gone) ──

    @mcp.tool()
    def get_screening_rules(source_file: str = "CostarExport.xlsx", property_name: str = "") -> str:
        """
        Explain how listings are ranked, and check a CoStar file has the columns
        the ranking needs.

        Use when asked what rules the screener applies, what the criteria are,
        whether the rules make sense, or to sanity-check a file before screening.

        The most important thing to convey: **nothing is eliminated.** Listings
        are ranked and every one keeps a stated reason. There is no pass/fail.
        """
        try:
            from analysis.screening.fit_screen import ASSUMPTIONS, WEIGHTS
            import pandas as pd

            lines = [
                "HOW LISTINGS ARE RANKED",
                "=" * 60,
                "",
                "Nothing is eliminated. Every listing is scored and ordered, and each keeps",
                "a reason. Tiers are assigned by rank within the file, so a market the firm",
                "has not entered yet still produces a usable shortlist.",
                "",
                "FOUR FACTORS:",
                f"  Proximity {WEIGHTS['proximity']:>3}  distance to land the firm already owns. Heaviest weight —",
                "               clustering is the most consistent pattern in the portfolio. Where",
                "               nothing is owned nearby this goes neutral rather than counting against.",
                f"  Pricing   {WEIGHTS['pricing']:>3}  what the entitled position must sell for to return the target",
                "               multiple, against what the market pays for the parcel size this",
                "               would be subdivided into. Not a comparison to recent sales —",
                "               Vaulter is not a builder.",
                f"  Distress  {WEIGHTS['distress']:>3}  long time on market, lender or REO owner, asking at or below",
                "               what the seller paid. Counted as favourable.",
                f"  Size      {WEIGHTS['size_fit']:>3}  judged in context. Small is normal inside a cluster, large is",
                "               normal as an assemblage. Only large AND isolated scores badly.",
                "",
                "ASSUMPTIONS (none ratified by the firm, but most are now measured):",
            ]
            for k, v in ASSUMPTIONS.items():
                lines.append(f"  {k:30} {v}")
            lines += [
                "",
                "WHERE THESE COME FROM (measured 2026-07-28 from the firm's own documents;",
                "full record and source paths in docs/PORTFOLIO_STANDARD.md):",
                "  Entitlement cost  three Arizona budget workbooks. Priced PER LOT and falls",
                "                    with scale; the largest project's figure is an invoiced actual.",
                "  Lot yield         four dated deals, 2.5-4.2/acre. The previous 8.0 was",
                "                    roughly double anything in the record.",
                "  Carry             property tax measured on two deals. A floor: insurance,",
                "                    management and maintenance have no figure on record.",
                "  Horizontal cost   four engineer's estimates, ALL Pinal County. Kept out of",
                "                    the arithmetic — the firm sells entitled, not built-out,",
                "                    land, and one county cannot price another state.",
                "  Hold period       six completed sales (6-15 years) against five models",
                "                    (30-60 months). Schedules in the record slip 2.5-4x.",
                "",
                "THE WEAKEST INPUT IS NOW THE FOUR WEIGHTS ABOVE. Two independent document",
                "searches found nothing in the corpus that ranks or weights selection factors.",
                "The closest thing is a senior partner's unordered rationale list on one deal —",
                "distressed basis, vested entitlements, low off-site cost, prepaid utility",
                "credits. No document contradicts the weights either. This one needs a person,",
                "not a search.",
            ]

            source_path = _resolve_costar_source(source_file=source_file,
                                                 property_name=property_name)
            if source_path is None:
                lines += ["", f"No file matching '{source_file}' to check columns against."]
                return "\n".join(lines)

            df = (pd.read_excel(source_path) if source_path.suffix.lower() in (".xlsx", ".xls")
                  else pd.read_csv(source_path))
            needed = {
                "Land Area (AC)": "size, and the per-acre maths",
                "For Sale Price": "everything in the pricing factor",
                "Secondary Type": "which exit product it is compared against",
                "Latitude": "distance to owned land", "Longitude": "distance to owned land",
                "Days On Market": "the distress signal",
                "County Name": "the peer group", "Submarket Name": "the peer group",
            }
            lines += ["", f"COLUMN CHECK — {source_path.name} ({len(df)} rows):"]
            for col, why in needed.items():
                have = col in df.columns
                filled = int(df[col].notna().sum()) if have else 0
                mark = "ok " if have and filled else "MISSING"
                lines.append(f"  {mark:8} {col:20} {filled:>4}/{len(df)}   {why}")
            return "\n".join(lines)

        except Exception as e:
            return f"Could not read the rules: {e}"

    @mcp.tool()
    def test_screener(source_file: str = "CostarExport.xlsx", property_name: str = "",
                      num_listings: int = 10) -> str:
        """
        Show WHY listings score as they do — a diagnostic, not a decision.

        Samples across the whole range (strongest, middle, weakest) and breaks
        each one into its four component scores, so you can see whether the
        ranking is behaving sensibly on this file. Use screen_listings when the
        question is which properties to pursue.

        Use when asked to check the screener is working, to see why something
        ranked where it did, or to sanity-check a new market's file.

        Args:
            source_file:  CoStar export filename
            property_name: Optional name to narrow the file search
            num_listings: How many to show (default 10, spread across the range)
        """
        try:
            from analysis.screening.fit_screen import screen

            source_path = _resolve_costar_source(source_file=source_file,
                                                 property_name=property_name)
            if source_path is None:
                return (f"Could not find a CoStar file matching '{source_file}'. "
                        f"Call open_costar_folder to see the drop folder.")

            r = screen(source_path, write_workbook=False)
            df = r["dataframe"]
            n = max(3, min(num_listings, len(df)))

            # spread the sample rather than showing only the winners -- a
            # ranking is easiest to sanity-check at its edges
            step = max(1, len(df) // n)
            picks = list(range(0, len(df), step))[:n]

            lines = [f"SCREENER DIAGNOSTIC — {source_path.name}",
                     f"{len(df)} listings, sampled every {step} by rank", "=" * 62, ""]
            for i in picks:
                x = df.iloc[i]
                lines.append(f"#{int(x['Rank'])}  {str(x.get('Property Address'))[:44]}")
                lines.append(f"     {x['Fit_Tier']}   overall {x['Fit_Score']:.0f}")
                lines.append(f"     proximity {x.get('Score_Proximity', 0):>3.0f} · "
                             f"pricing {x.get('Score_Pricing', 0):>3.0f} · "
                             f"distress {x.get('Score_Distress', 0):>3.0f} · "
                             f"size {x.get('Score_Size', 0):>3.0f}")
                lines.append(f"     {x['Why']}")
                lines.append("")

            conf = df["Pricing_Confidence"].value_counts().to_dict()
            lines += [
                f"Pricing confidence across the file: {conf}",
                f"Peer groups used: {df['Exit_Comp_Basis'].nunique()}",
                "",
                "Low confidence means too few comparable parcels to price against — "
                "expected on a small file or a thin submarket, not a fault.",
            ]
            return "\n".join(lines)

        except Exception as e:
            log.error(f"[MCP] test_screener failed: {e}", exc_info=True)
            return _screen_error(source_file, e)

    @mcp.tool()
    def screen_listings(
        source_file: str = "CostarExport.xlsx",
        property_name: str = "",
        file_content_b64: str = "",
        moic_target: float = 3.0,
        show_top: int = 15,
    ) -> str:
        """
        Screen a CoStar export or broker spreadsheet by FIT against Vaulter's
        existing portfolio, and return a ranked shortlist.

        Free and instant — no API calls, no per-listing cost, works on any
        market (AZ, TX, CO, UT, ...). Every market-relative number is computed
        from peers inside the export itself, so it self-calibrates.

        IMPORTANT — nothing is eliminated. Every listing is ranked and
        explained. Vaulter's documented rejection history is thin, so hard
        filters would silently destroy deal flow. Low-fit listings sink to the
        bottom with a stated reason rather than disappearing.

        What it scores, all derived from docs/COMPANY_PROFILE.md:
          - Proximity to existing holdings (heaviest weight — clustering is the
            firm's strongest revealed preference and is exactly checkable)
          - Size judged IN CONTEXT (large + standalone in an unfamiliar market
            is the documented failure mode; large near holdings is the
            assemblage pattern; small inside a cluster is normal)
          - Pricing from a predevelopment value-add perspective — NOT user or
            spec-developer comps. Reports the exit each listing must achieve to
            return `moic_target` on invested capital, expressed as a multiple
            of same-type peers in the same submarket.
          - Distress as a POSITIVE (long days on market, lender/REO owner,
            asking at or below prior basis) — a distressed basis was the
            stated #1 rationale on one of the firm's best acquisitions.
          - Cautions surfaced, never eliminating: a high flood-risk flag as a
            question about NET developable acreage (never a dealbreaker — the
            firm has bought through it), structures as possible income rather
            than demolition cost, and an ask far above the firm's average.

        After this returns, do the qualitative work yourself in this
        conversation on the top candidates — read them, weigh entitlement
        risk, and give a view. Do NOT call a separate Claude API for that;
        this tool deliberately costs nothing.

        Args:
            source_file:      Filename of the CoStar export (default: CostarExport.xlsx)
            property_name:    Optional name to narrow the file search
            file_content_b64: Base64 file content — LAST RESORT, not the default for an
                               attached file. Measured ~43,000 tokens to pass one real
                               216-row export this way, versus zero by filename. Put the
                               file in the drop folder (open_costar_folder) and pass
                               source_file instead whenever that's at all possible.
            moic_target:      Target multiple on invested capital (default 3.0; the
                               firm targets 2.5-3x on predevelopment value-add)
            show_top:         How many ranked listings to list back (default 15)
        """
        # Entry stamp. A Desktop call once showed a four-minute gap between the
        # MCP layer accepting the request and this tool's first log line, and
        # with nothing logged in between there was no way to tell whether the
        # time went into the import, the file lookup, or the client. Both are
        # timed now so the next occurrence names itself.
        import time as _t
        _t0 = _t.perf_counter()
        log.info("[MCP] screen_listings: entered")
        try:
            from analysis.screening.fit_screen import (screen, ASSUMPTIONS, MISSION,
                                                       PRE_PURSUIT_CHECKS)
            log.info(f"[MCP] screen_listings: imports ready in {_t.perf_counter()-_t0:.1f}s")

            _t1 = _t.perf_counter()
            source_path = _resolve_costar_source(
                source_file=source_file,
                property_name=property_name,
                file_content_b64=file_content_b64,
            )
            if source_path is None:
                property_clause = f' for property "{property_name}"' if property_name else ""
                return (
                    f"Could not find a CoStar file matching '{source_file}'{property_clause}.\n\n"
                    f"Two ways to give me one:\n"
                    f"  1. Drop it into the CoStar folder (call open_costar_folder to open it), "
                    f"then tell me the filename.\n"
                    f"  2. Attach or paste the export directly into this conversation."
                )

            log.info(f"[MCP] screen_listings: resolved in "
                     f"{_t.perf_counter()-_t1:.1f}s -> {source_path}")
            r = screen(source_path, moic=moic_target)
            df = r["dataframe"]

            # A visual report alongside the workbook. Failing to build it must
            # never lose the screening result itself, which is the expensive part.
            report_path = None
            try:
                from analysis.screening.report import build_report
                report_path = build_report(r)
            except Exception as e:
                log.warning(f"[MCP] Report build failed (screening still succeeded): {e}")

            def _cell(row, name, default=""):
                v = row.get(name, default)
                return default if v != v else v  # NaN check without importing pandas

            # No two CoStar exports carry the same columns, so the screener
            # resolves each concept from whatever this file provides. Report the
            # result both ways: what had to be found elsewhere or derived (so a
            # thin number is read as thin), and what could not be found at all.
            # Silence here is what made a 50-row Tucson export read as a flat,
            # dull market rather than as a file with almost nothing in it.
            _WHY_IT_MATTERS = {
                "Land Area (AC)": "every per-acre figure, and the exit comparison",
                "Secondary Type": "which exit product this is measured against",
                "For Sale Price": "all pricing",
                "Latitude":       "distance to land the firm owns",
                "Longitude":      "distance to land the firm owns",
                "Days On Market": "the distress signal",
            }
            total = r["total_screened"] or 1
            srcs = {c["field"]: c for c in r.get("column_sources", [])}

            recovered, missing, partial = [], [], []
            for field, why in _WHY_IT_MATTERS.items():
                got = srcs.get(field, {"rows": 0, "note": "", "source": ""})
                if not got["rows"]:
                    missing.append(f"    · {field} — no column for it, so {why} is unavailable")
                else:
                    if got["note"]:
                        recovered.append(f"    · {field} — {got['note']} ({got['rows']} listings)")
                    if got["rows"] < total * 0.8:
                        partial.append(f"    · {field} — only {got['rows']} of {total} listings")

            warn = []
            if recovered or missing or partial:
                warn = ["", "HOW COMPLETE THIS EXPORT IS"]
                if recovered:
                    warn += ["  Found under other names, or worked out:"] + recovered
                if partial:
                    warn += ["  Present but sparse:"] + partial
                if missing:
                    warn += ["  Not in this export at all:"] + missing
                if missing or partial:
                    warn.append(
                        "  Every listing is still here and nothing was dropped, but the ordering "
                        "rests on less than usual. Treat the ranking as a starting point rather "
                        "than a verdict, and if a size or price column exists in CoStar for these "
                        "properties, re-exporting with it will sharpen this considerably."
                    )

            # Whether this file can be MAPPED is a property of the file, known
            # now -- but it used to be discoverable only one refusal at a time,
            # rank by rank, from run_proximity_for_listing. Say it once here.
            # Empty string when every row has a coordinate, so a complete export
            # prints nothing new. Never worth losing a screen over.
            try:
                from pipeline.proximity_tool import coordinate_coverage
                _cov = coordinate_coverage(df)
                if _cov["message"]:
                    if not warn:
                        warn = ["", "HOW COMPLETE THIS EXPORT IS"]
                    warn += ["  " + _cov["message"]]
            except Exception as e:
                log.warning(f"[MCP] coordinate_coverage failed (screening unaffected): {e}")

            lines = [
                f"SCREENED {r['total_screened']} LISTINGS — {source_path.name}",
                "=" * 64,
                *warn,
                f"Markets in file : {', '.join(r['markets'][:6]) or 'unspecified'}",
                f"Compared against: {r['holdings_used']} geocoded Vaulter holdings",
                f"Pricing lens    : {r['moic_target']:g}x MOIC on purchase + entitlement "
                f"(measured per lot) + property-tax carry over "
                f"{ASSUMPTIONS['hold_years_actual']}yr",
                "",
                "Fit tiers (nothing eliminated):",
            ]
            for tier, n in sorted(r["tier_counts"].items()):
                lines.append(f"  {tier:20} {n:4d}")

            # What the firm is, so the ranking is read as a predevelopment
            # investor would read it rather than as a generic land screen. Each
            # listing then carries its own Vaulter_Read line below.
            lines += ["", f"READ AS: {MISSION}"]

            lines += ["", f"TOP {min(show_top, len(df))} BY FIT:"]
            for _, row in df.head(show_top).iterrows():
                price = _numeric(row.get("For Sale Price"))
                price_s = f"${price/1e6:.1f}M" if price else "no price"
                acres_s = _acres_str(row.get("Land Area (AC)"))
                lines.append(
                    f"  {int(row['Rank']):3d}. [{row['Fit_Score']:.0f}] "
                    f"{str(row.get('Property Address'))[:38]} — {acres_s}, "
                    f"{str(row.get('Secondary Type'))}, {price_s}"
                )
                lines.append(f"        {row['Why']}")
                if row.get("Vaulter_Read"):
                    lines.append(f"        {row['Vaulter_Read']}")
                if row.get("Cautions"):
                    lines.append(f"        CAUTION: {row['Cautions']}")

            lines += ["", PRE_PURSUIT_CHECKS]

            irr_cols = [c for c in df.columns if c.startswith("IRR_at_")]
            if irr_cols:
                lines += ["", "REALITY CHECK ON THE MULTIPLE:"]
                for c in irr_cols:
                    lines.append(f"  {c.replace('_', ' ')}: {df[c].iloc[0]}% IRR")
                lines.append(
                    "  The firm's own published multiples (vaulterup.com) are 2.40x at 5yr, "
                    "1.71x at 10yr, 1.61x at 15yr. Measured against settlement statements, six "
                    "completed deals ran 6-15 years and returned anywhere from 0.72x (a real "
                    "loss) to 18.8x (a bank-disposal purchase) — and 21 properties bought "
                    "2011-15 are still held, so the completed deals are the ones that could "
                    "exit. Treat a 3x pro forma accordingly."
                )

            lines += [
                "",
                f"Full ranked workbook: {r.get('workbook_path')}",
                (f"Visual report: {report_path}"
                 if report_path else
                 "Visual report: could not be built this run; the workbook still has everything."),
                ("  Offer to open it with open_screening_dashboard — it has the map, aerial "
                 "views and a click-through detail card for every listing."
                 if report_path else ""),
                "",
                "Assumptions are in the workbook's 'Assumptions' tab and are NOT ratified — "
                "the cost, timing and return figures are measured from the firm's own "
                "documents (docs/PORTFOLIO_STANDARD.md records every source), but nobody "
                "has signed off on them. The four scoring weights have no evidence at all.",
            ]
            return "\n".join(lines)

        except Exception as e:
            log.error(f"[MCP] screen_listings failed: {e}", exc_info=True)
            return _screen_error(source_file, e)

    @mcp.tool()
    def compare_to_portfolio_history(
        state: str = "",
        county: str = "",
        land_type: str = "",
        plan_type: str = "",
        acres: float = None,
        top_n: int = 5,
    ) -> str:
        """
        Find the firm's own past deals that most resemble a listing or an
        off-market property, and how the firm approached them.

        Use this when someone asks "have we done anything like this before,"
        "what's similar in our history," or wants a new deal compared against
        the firm's track record — for a listing already screened by
        screen_listings, or for a single off-market property an analyst is
        looking at directly.

        Compares CHARACTERISTICS only — location, land type, the kind of plan
        (rezone/subdivide/etc.), and size. Does NOT compare price or tell you
        whether to pursue the deal — that decision needs a person, and pricing
        comparison for a standalone property isn't wired up yet. What this
        gives you: the most similar past deals, why they matched, what
        actually happened with each one (still held / sold / pending), and
        the market conditions each was bought into, so a person can judge
        whether that playbook still applies today.

        If nothing in the portfolio meaningfully resembles the input, this
        says so plainly rather than forcing a weak match — that's a genuine
        finding (this may be a new kind of deal for the firm), not a failure.

        Args:
            state: two-letter state code (e.g. "AZ"), if known
            county: county name, if known
            land_type: one of residential, commercial, industrial, mixed-use,
                       agricultural — leave blank if unclear
            plan_type: one of rezone, subdivide, entitle-only, annex,
                       hold-only, assemble-resell, recapitalization — leave
                       blank if unclear
            acres: parcel size, if known
            top_n: how many matches to return (default 5)
        """
        try:
            from analysis.screening.portfolio_comparison import find_similar_deals

            facts = {"state": state, "county": county, "land_type": land_type,
                      "plan_type": plan_type, "acres": acres}
            result = find_similar_deals(facts, top_n=top_n)

            lines = [result["coverage_note"], ""]
            if not result["matches"]:
                lines.append("Nothing else to report — no forced or approximate match follows.")
                return "\n".join(lines)

            for i, m in enumerate(result["matches"], 1):
                lines.append(f"{i}. {m['property_name']}")
                lines.append(f"   Why it matched: {', '.join(m['reasons'])}")
                lines.append(f"   What happened: {m['outcome_status'].replace('-', ' ')} — {m['notes']}")
                if m["era_note"]:
                    lines.append(f"   {m['era_note']}")
                lines.append("")

            lines.append(
                "This is a comparison, not a recommendation — read the full story for any of "
                "these with get_property_summary before drawing a conclusion."
            )
            return "\n".join(lines)

        except Exception as e:
            log.error(f"[MCP] compare_to_portfolio_history failed: {e}", exc_info=True)
            return f"Could not compare to portfolio history: {e}"

    @mcp.tool()
    def verify_listings(
        source_file: str = "CostarExport.xlsx",
        property_name: str = "",
        top_n: int = 6,
    ) -> str:
        """
        Run authoritative ground-truth checks on the top-ranked listings from a
        CoStar export: FEMA flood zones over the parcel footprint, Census TIGER
        road access, incorporated-place status, and terrain relief.

        Free, keyless, and works in any US market — every source is a federal
        ArcGIS service with uniform national coverage. Results are cached in the
        shared folder by coordinate, so a repeat run is instant and the whole
        team benefits from one person's lookups.

        Two things worth knowing about the output:

          * Flood is checked over the parcel's AREA, not its centre point. That
            matters: an 80-acre listing whose centroid reads "Zone X, minimal
            hazard" had an AE Special Flood Hazard Area across part of the
            parcel. A point check would have cleared it wrongly.
          * "UNAVAILABLE" means a service did not answer. It never means the
            hazard is absent. Do not report it as a clean result.

        Floodplain is NOT a dealbreaker for this firm — the deal record includes
        an acquisition with a meaningful share of its acreage in the 100-year
        floodplain. It matters because it reduces NET developable acreage,
        which is how the firm prices land.

        Args:
            source_file:   Filename of the CoStar export
            property_name: Optional name to narrow the file search
            top_n:         How many top-ranked listings to verify (default 6)
        """
        try:
            from analysis.screening.fit_screen import screen
            from analysis.screening import geo_federal as gf

            source_path = _resolve_costar_source(source_file=source_file,
                                                 property_name=property_name)
            if source_path is None:
                return (f"Could not find a CoStar file matching '{source_file}'. "
                        f"Call open_costar_folder to see the drop folder.")

            df = screen(source_path, write_workbook=False)["dataframe"].head(top_n)
            lines = [f"GROUND TRUTH — top {len(df)} of {source_path.name}",
                     "=" * 64, ""]

            for _, row in df.iterrows():
                lat, lng = row.get("Latitude"), row.get("Longitude")
                acres = row.get("Land Area (AC)")
                r = gf.verify_site(lat, lng, acres)
                lines.append(f"#{int(row['Rank'])} {row.get('Property Address')} "
                             f"— {_acres_str(acres)} {row.get('Secondary Type')}")

                if r.get("status") == "NO_COORDINATES":
                    lines.append("    no coordinates in the export — cannot verify")
                    lines.append("")
                    continue

                f, rd, pl, el = r["flood"], r["roads"], r["place"], r["elevation"]
                if f["status"] == "OK":
                    flag = "SFHA PRESENT" if f["sfha_present"] else "no SFHA"
                    lines.append(f"    flood    : {flag} — zones {', '.join(f['zones'])}")
                    if f["sfha_present"]:
                        lines.append("               confirm NET developable acreage before pricing")
                else:
                    lines.append(f"    flood    : {f['status']} — {f['note']}")

                lines.append(f"    access   : {rd['status']}"
                             + (f" — {', '.join(rd['named'][:4])}" if rd.get("named") else "")
                             + (f" ({rd['count']} segments)" if rd.get("count") else ""))
                lines.append(f"    place    : {pl.get('place') or pl['status']}"
                             + ("  (annexation likely needed for city utilities)"
                                if pl["status"] == "UNINCORPORATED" else ""))
                lines.append(f"    terrain  : {el.get('max_diff_m')}m relief across sample points"
                             if el["status"] == "OK" else "    terrain  : UNAVAILABLE")
                if r.get("from_cache"):
                    lines.append("    (cached)")
                lines.append("")

            lines.append("Sources: FEMA National Flood Hazard Layer, Census TIGER, USGS. "
                         "All free and keyless.")
            lines.append("Flood is checked over the parcel footprint approximated from stated "
                         "acreage — not a survey. An SFHA hit means get one.")
            return "\n".join(lines)

        except Exception as e:
            log.error(f"[MCP] verify_listings failed: {e}", exc_info=True)
            return _screen_error(source_file, e)

    @mcp.tool()
    def open_screening_dashboard() -> str:
        """
        Open the most recent visual screening report in a browser.

        Use after screen_listings, when the user wants to see the map, the
        aerial views, the shortlist, or the full ranked table rather than a
        summary in chat.

        The report is a single self-contained HTML file in the shared folder,
        so a colleague can open it straight from OneDrive with nothing running
        on their machine.
        """
        import glob
        import webbrowser
        try:
            from config import SCREENING_OUTPUT_DIR

            reports = sorted(glob.glob(str(Path(SCREENING_OUTPUT_DIR) / "screen_*.html")),
                             key=lambda f: Path(f).stat().st_mtime, reverse=True)
            if not reports:
                return ("No screening report has been generated yet. Run screen_listings "
                        "first and one will be written alongside the workbook.")

            newest = Path(reports[0])
            if webbrowser.open(newest.as_uri()):
                return f"Opened {newest.name} in your browser.\n\nFile: {newest}"
            return ("Could not confirm a browser opened (no default browser configured?). "
                    f"Open this file directly: {newest}")
        except Exception as e:
            return f"Could not open the screening report: {e}"

    @mcp.tool()
    def run_proximity_for_listing(
        rank: int,
        source_file: str = "CostarExport.xlsx",
        property_name: str = "",
        radius_miles: float = 5.0,
    ) -> str:
        """
        Map what is actually around a screened CoStar listing — every business,
        employer, school, utility and piece of infrastructure within a radius.

        The screener answers "how close is this to land we already own." This
        answers "what is actually there," which is a different question and the
        one that decides whether a site can support what you would entitle it
        for. Use it on a shortlist candidate, not on all 216.

        Free and keyless. Exports a CSV to the shared proximity folder, the
        same format as the portfolio version, so a listing and an owned
        property can be compared side by side.

        Args:
            rank:          Position in the ranked screen (1 is the strongest).
                           Run screen_listings first to see the ranking.
            source_file:   CoStar export the ranking came from
            property_name: Optional name to narrow the file search
            radius_miles:  Search radius (default 5)
        """
        try:
            from analysis.screening.fit_screen import screen
            from pipeline.proximity_tool import coordinate_coverage, run_proximity_search

            source_path = _resolve_costar_source(source_file=source_file,
                                                 property_name=property_name)
            if source_path is None:
                return (f"Could not find a CoStar file matching '{source_file}'. "
                        f"Call open_costar_folder to see the drop folder.")

            df = screen(source_path, write_workbook=False)["dataframe"]
            hit = df[df["Rank"] == rank]
            if hit.empty:
                return f"No listing ranked {rank} — this file has {len(df)}."

            row = hit.iloc[0]
            lat, lng = row.get("Latitude"), row.get("Longitude")
            if lat is None or lng is None or lat != lat or lng != lng:
                # Say whether this is one bad row or the whole file, so the user
                # stops trying ranks one at a time.
                _cov = coordinate_coverage(df)
                return (f"#{rank} {row.get('Property Address')} has no coordinates in the "
                        f"export, so there is nothing to search around."
                        + (f"\n\n{_cov['message']}" if _cov["message"] else ""))

            label = f"LISTING {rank} - {row.get('Property Address')}"
            log.info(f"[MCP] proximity for listing #{rank} at {lat},{lng}")
            body = run_proximity_search(
                property_name=label, radius_miles=radius_miles,
                vaulter_dir=Path(__file__).parent, lat=float(lat), lon=float(lng),
            )
            header = (f"#{rank} {row.get('Property Address')} — "
                      f"{_acres_str(row.get('Land Area (AC)'))} {row.get('Secondary Type')}, "
                      f"{row.get('City')}\n"
                      f"Ranked {rank} of {len(df)} · {row.get('Fit_Tier')}\n")
            return header + "\n" + body

        except Exception as e:
            log.error(f"[MCP] run_proximity_for_listing failed: {e}", exc_info=True)
            return f"run_proximity_for_listing failed: {e}"

    @mcp.tool()
    def run_proximity_for_property(property_name: str, radius_miles: float = 5.0) -> str:
        """
        Map everything around one of Vaulter's own properties — businesses,
        employers, schools, government, infrastructure and nuisance uses within
        a radius — and save it as a CSV.

        This is the ONLY way to produce that export. Do not attempt it with web
        search or a maps site. Call this directly whenever the user asks what is
        near a property, for a proximity report, or to export nearby businesses.

        Free and keyless, running on OpenStreetMap. For a CoStar listing rather
        than an owned property, use run_proximity_for_listing with its rank.

        The property must have a verified coordinate in property_coordinates.csv.
        If it does not, this refuses rather than guessing the location from the
        name — that guess was wrong for 5 of 8 properties tested, twice landing
        in the wrong country, and it fails silently.

        Args:
            property_name: Portfolio property, by Project Master name
            radius_miles:  Search radius (default 5)
        """
        try:
            from pipeline.proximity_tool import run_proximity_search
            from pathlib import Path

            return run_proximity_search(
                property_name=property_name,
                radius_miles=radius_miles,
                vaulter_dir=Path(__file__).parent,
            )
        except Exception as e:
            log.error(f"[MCP] run_proximity_for_property failed: {e}", exc_info=True)
            return f"Proximity export failed: {e}"

    @mcp.tool()
    def compare_proximity_to_portfolio(
        property_names: list[str],
        rank: int = 0,
        source_file: str = "CostarExport.xlsx",
        listing_property_name: str = "",
        radius_miles: float = 5.0,
    ) -> str:
        """
        Compare what is near a screened CoStar listing with what is near land
        the firm already owns — same radius, same categories, side by side.

        Answers "is this like the ground we already know?" without opening two
        spreadsheets. Give `rank` (from screen_listings) for the candidate and
        one to three portfolio names to compare against; or omit `rank` and
        pass two or more names to compare owned properties with each other.

        Each owned property must have a verified coordinate in
        property_coordinates.csv or this refuses — it never guesses a location
        from a name. Free and keyless, on OpenStreetMap. Every site is a
        separate query against a volunteer-run endpoint, so four places is the
        limit and this takes a little while.

        Args:
            property_names: portfolio properties, by Project Master name
            rank:           rank of the candidate listing (0 = no candidate)
            source_file:    CoStar export the ranking came from
            listing_property_name: optional name to narrow the file search
            radius_miles:   search radius (default 5)
        """
        try:
            from analysis.screening.fit_screen import screen
            from pipeline.proximity_tool import compare_proximity, coordinate_coverage

            lat = lng = None
            label = ""
            if rank:
                source_path = _resolve_costar_source(source_file=source_file,
                                                     property_name=listing_property_name)
                if source_path is None:
                    return (f"Could not find a CoStar file matching '{source_file}'. "
                            f"Call open_costar_folder to see the drop folder.")
                df = screen(source_path, write_workbook=False)["dataframe"]
                hit = df[df["Rank"] == rank]
                if hit.empty:
                    return f"No listing ranked {rank} — this file has {len(df)}."
                row = hit.iloc[0]
                lat, lng = row.get("Latitude"), row.get("Longitude")
                if lat is None or lng is None or lat != lat or lng != lng:
                    _cov = coordinate_coverage(df)
                    return (f"#{rank} {row.get('Property Address')} has no coordinates in "
                            f"the export, so it cannot be compared on the map."
                            + (f"\n\n{_cov['message']}" if _cov["message"] else ""))
                label = f"LISTING {rank} - {row.get('Property Address')}"
                lat, lng = float(lat), float(lng)

            return compare_proximity(
                property_names=list(property_names or []),
                radius_miles=radius_miles,
                vaulter_dir=Path(__file__).parent,
                candidate_label=label, candidate_lat=lat, candidate_lon=lng,
            )
        except Exception as e:
            log.error(f"[MCP] compare_proximity_to_portfolio failed: {e}", exc_info=True)
            return f"compare_proximity_to_portfolio failed: {e}"

    return mcp


# ══════════════════════════════════════════════════════════════════
# Server Entry Point
# ══════════════════════════════════════════════════════════════════

def run_mcp_server():
    """
    Launch the MCP server. Transport is stdio (see this file's header) --
    there is no port to configure; a `port` parameter existed here previously
    but did nothing, since stdio has no network listener at all.

    Single-threaded now. The PDF watcher and scheduler threads this used to
    start were both removed in the 2026-07 rebuild; the daily update check
    that the scheduler owned runs from check_system_health instead.
    """
    log.info("[MCP] Starting Vaulter AI MCP server...")

    # Load the heavy compiled libraries BEFORE the event loop starts.
    #
    # Every tool in this file imports lazily, which keeps CLI startup quick --
    # but under stdio it meant pandas/numpy were first imported *inside* a
    # running asyncio loop, on the first tool call. On Windows that stalls for
    # minutes loading numpy's C extension: a stack dump caught the main thread
    # parked in numpy/_core/multiarray.py at create_module, reached from
    # check_system_health. The same import takes 0.2s in a plain process.
    #
    # To the user this looked like a dead server -- Claude Desktop reported
    # "the MCP server isn't responding" and suggested a restart, which never
    # helped, because the next first-call paid the same cost again.
    #
    # Warming them here costs about a second of startup, once, off the loop.
    # Keep this ahead of mcp.run(); do not "tidy" it into a lazy import.
    import time as _t
    _t0 = _t.perf_counter()
    try:
        import pandas  # noqa: F401  (drags in numpy, the actual offender)
        import corpus  # noqa: F401  (what check_system_health reaches for first)
        log.info(f"[MCP] Preloaded pandas/corpus in {_t.perf_counter()-_t0:.1f}s")
    except Exception as e:
        # A failure here is not fatal -- the tools still import lazily and will
        # report their own errors. Better a slow server than none.
        log.warning(f"[MCP] Preload failed ({e}); tools will import on demand.")

    mcp = create_mcp_server()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    run_mcp_server()

