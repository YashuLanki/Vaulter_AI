"""
config.py
---------
Central configuration for the Vaulter AI Property Intelligence System.
All paths, settings, and constants live here.

Cross-platform: automatically detects Windows or Mac and sets the correct paths.
To adapt this project to a new machine, only this file needs to be updated.

Secrets (.env) live in confidentials/, relative to the project folder on every OS.
outlook_token.json no longer applies -- email ingestion was removed in the 2026-07
rebuild; delete that file if it's still sitting in confidentials/ on this machine.

NEVER put real credentials directly in this file.
"""

import os
import shutil
import sys
from pathlib import Path
from dotenv import load_dotenv

# ─── Project Root ─────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent

# ─── Secrets Folder ───────────────────────────────────────────────
# Canonical location is the project folder itself (same on every OS, and
# what the installer/setup wizard writes to) -- this used to be a
# DIFFERENT hardcoded path on Windows only, which was a real landmine:
# setup instructions told people to create confidentials/ inside the
# project, but the code only ever read from the hardcoded path, so
# secrets could look "set up" and silently never load, with no error.
#
# Windows still checks that old hardcoded path as a fallback -- ONLY if
# it already has a real .env in it and the project folder doesn't --
# so the one existing setup that predates this fix keeps working
# unchanged, without switching it out from under that machine.
_PROJECT_SECRETS_DIR = BASE_DIR / "confidentials"

if sys.platform == "win32":
    _LEGACY_WIN_SECRETS_DIR = Path(r"C:\Users") / os.environ.get("USERNAME", "YourName") / "Vaulter AI" / "confidentials"
    if not (_PROJECT_SECRETS_DIR / ".env").exists() and (_LEGACY_WIN_SECRETS_DIR / ".env").exists():
        SECRETS_DIR = _LEGACY_WIN_SECRETS_DIR
    else:
        SECRETS_DIR = _PROJECT_SECRETS_DIR
else:
    SECRETS_DIR = _PROJECT_SECRETS_DIR

SECRETS_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(SECRETS_DIR / ".env", override=True)

# ─── OneDrive: shared folder + document corpus ────────────────────
# Two different things live under the same synced OneDrive-for-Business
# account root, and they are NOT interchangeable:
#
#   SHARED_DIR ("Vaulter AI Shared")  -- this system's own OUTPUT. Screening
#       workbooks, proximity exports, update packages. Shared team-wide so one
#       person's screening run is visible to everyone instead of sitting only
#       on their own machine. This system writes here.
#
#   CORPUS_DIR ("Vaulter LLC - shaw") -- the firm's actual SharePoint document
#       library: !PROPERTIES/<STATE>/<Property>/, CLOSING MEMOS, entity files.
#       This system only ever READS here, never writes.
#
# CORPUS_DIR is deliberately the shaw library and NOT the OneDrive account
# root. The root also contains the individual's own Desktop, Documents,
# Pictures, and "Microsoft Teams Chat Files" -- personal content that this
# system must never read or index. Scoping to the shaw subfolder is the
# privacy boundary, and corpus/index.py enforces it on every path it touches.
#
# Auto-detects "OneDrive - Vaulter LLC" (standard OneDrive-for-Business
# naming -- same folder name for everyone, different C:\Users\<name>\ per
# person). Override either with VAULTER_SHARED_DIR / VAULTER_CORPUS_DIR in
# confidentials/.env if your OneDrive is named or located differently.

_LOCAL_FALLBACK_DIR = (BASE_DIR / "data" / "shared_fallback_not_synced").resolve()

ONEDRIVE_FOLDER_NAME = "OneDrive - Vaulter LLC"
SHARED_SUBFOLDER     = "Vaulter AI Shared"
CORPUS_SUBFOLDER     = "Vaulter LLC - shaw"


def _detect_onedrive_root() -> Path | None:
    """The synced OneDrive-for-Business account root, or None if not found."""
    candidates = []
    if sys.platform == "win32":
        username = os.environ.get("USERNAME", "YourName")
        candidates.append(Path(r"C:\Users") / username / ONEDRIVE_FOLDER_NAME)
    else:
        home = Path.home()
        # Modern OneDrive for Mac syncs under ~/Library/CloudStorage/;
        # older versions/some configs use ~/<OneDrive folder name> directly.
        candidates.append(home / "Library" / "CloudStorage" / f"OneDrive-{ONEDRIVE_FOLDER_NAME.replace('OneDrive - ', '').replace(' ', '')}")
        candidates.append(home / ONEDRIVE_FOLDER_NAME)

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


ONEDRIVE_ROOT = _detect_onedrive_root()


def _detect_shared_dir() -> Path:
    override = os.getenv("VAULTER_SHARED_DIR", "").strip()
    if override:
        return Path(override)
    if ONEDRIVE_ROOT:
        return ONEDRIVE_ROOT / SHARED_SUBFOLDER

    # OneDrive not found on this machine -- fall back to a local folder so
    # nothing crashes, but this means screening results won't actually be
    # shared with the team until VAULTER_SHARED_DIR is set correctly.
    return _LOCAL_FALLBACK_DIR

SHARED_DIR = _detect_shared_dir()
try:
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
except OSError as e:
    # This runs at config.py's IMPORT time -- every entry point imports
    # this module first, so an unguarded failure here (a transient
    # OneDrive sync/permission hiccup, a network path briefly
    # unreachable, etc.) would crash the ENTIRE application before it
    # can even start. Fall back to a local folder instead -- screening
    # results just won't be shared with the team until this is resolved,
    # same degraded-but-working behavior as "OneDrive not found at all."
    # Deliberately sys.stderr, never stdout/logging: this runs before
    # main.py sets up file-only logging in MCP mode, and any stray stdout
    # write here would corrupt the MCP stdio connection to Claude Desktop.
    print(f"WARNING: could not create shared folder at {SHARED_DIR} ({e}) -- "
          f"falling back to a local-only folder. Screening results will not "
          f"be shared with the team until this is fixed.", file=sys.stderr)
    SHARED_DIR = _LOCAL_FALLBACK_DIR
    SHARED_DIR.mkdir(parents=True, exist_ok=True)

# Whether SHARED_DIR is actually the real OneDrive-backed folder, or the
# local fallback used when OneDrive wasn't found / couldn't be written to.
# Exposed so the health-check tool (mcp_server.py) can report "silently
# fell back to local" without reaching into this module's private
# _LOCAL_FALLBACK_DIR/_detect_shared_dir internals.
SHARED_DIR_IS_FALLBACK = (SHARED_DIR == _LOCAL_FALLBACK_DIR)

def _find_corpus_subfolder(onedrive_root: Path) -> Path | None:
    """
    CORPUS_SUBFOLDER's exact name isn't guaranteed to match across every
    teammate's synced copy -- confirmed 2026-07-29 that colleagues see
    different capitalization ("shaw" vs "Shaw"), and this machine's own
    exact "Vaulter LLC - shaw" match is not something to assume elsewhere.
    Same problem CoStar column resolution already solved: don't index one
    exact name, match the concept, and refuse rather than guess when it's
    genuinely ambiguous.
    """
    exact = onedrive_root / CORPUS_SUBFOLDER
    if exact.is_dir():
        return exact

    try:
        candidates = [d for d in onedrive_root.iterdir()
                      if d.is_dir() and "shaw" in d.name.lower()]
    except OSError:
        return None

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        print(f"WARNING: found {len(candidates)} folders under {onedrive_root} "
              f"matching 'shaw' ({[c.name for c in candidates]}) -- can't tell "
              f"which is the real document library. Set VAULTER_CORPUS_DIR in "
              f"confidentials/.env to the correct one.", file=sys.stderr)
    return None


# The firm's document library. Read-only, and deliberately NOT mkdir'd:
# if it isn't there, that means OneDrive isn't syncing the shaw library on
# this machine, which is a real condition for check_system_health to report
# -- creating an empty folder would just hide it and make every search
# silently return nothing.
_corpus_override = os.getenv("VAULTER_CORPUS_DIR", "").strip()
if _corpus_override:
    CORPUS_DIR = Path(_corpus_override)
elif ONEDRIVE_ROOT:
    CORPUS_DIR = _find_corpus_subfolder(ONEDRIVE_ROOT)
else:
    CORPUS_DIR = None

CORPUS_AVAILABLE = CORPUS_DIR is not None and CORPUS_DIR.is_dir()

# Local (per-machine) cache of the corpus's file/folder NAMES only -- never
# file contents. See corpus/index.py for why this exists: the library is
# synced as OneDrive Files On-Demand placeholders, so walking it live is
# slow, and reading contents downloads them.
#
# SQLite rather than JSON because the library turned out to hold ~400,000
# files -- a JSON index is ~60MB and would have to be parsed in full on
# every single search.
CORPUS_INDEX_FILE = (BASE_DIR / "data" / "corpus_index.db").resolve()

# ─── Data Folders ─────────────────────────────────────────────────

DATA_DIR       = (BASE_DIR / "data").resolve()
LOG_DIR        = DATA_DIR / "logs"

# Where a CoStar export or broker spreadsheet gets dropped so screen_listings
# can find it by filename. Just a folder -- nothing watches it. The old
# data/watched_folder/<State>/<Property>/ tree and its watcher thread are
# gone; documents live in CORPUS_DIR now and are read in place.
DROP_DIR = DATA_DIR / "drop"
DROP_DIR.mkdir(parents=True, exist_ok=True)

# Pre-rebuild locations, still searched when resolving a CoStar file by name
# so exports already sitting on an existing machine don't become invisible
# after an update. Nothing writes here any more.
LEGACY_WATCH_DIR     = DATA_DIR / "watched_folder"
LEGACY_PROCESSED_DIR = DATA_DIR / "processed"

# CoStar listing screener & proximity search — both produce outputs that are
# SHARED (under SHARED_DIR) on purpose so one person's analysis run is visible
# to the whole team instead of sitting only on their own machine, avoiding
# duplicate API calls and letting the team benefit from each run.
#
# There used to be a local OUTPUT_DIR (data/output) here too. Screening output
# moved to SHARED_DIR and nothing read the local one any more -- it just kept
# creating an empty folder. MEETINGS_DIR went the same way: it was for the
# meeting-transcript feature that was never built and has since been retired,
# and because it lived under SHARED_DIR it created an empty "meetings" folder
# in the team's OneDrive on every import, on every machine.
# ── Shape of the shared folder (2026-08-03) ───────────────────────────────
# It had grown to eight sibling folders at the top level, mixing three very
# different things: what a person DROPS IN, what they should GO LOOK AT, and
# machinery nobody should ever need to open. Now grouped so the top level
# answers "where do I put things / where do I find results" at a glance:
#
#   Vaulter AI Shared/
#     CoStar Drop/           <- inputs, deliberately kept at the top level so
#     Smartsheet Portfolio/     they stay easy to find and drop files into
#     output/                <- what people actually read
#       proximity/  screening/  property_summaries/
#     system/                <- machinery; nobody should need to open this
#       geo_cache/  org_settings/  updates/
#
# Inputs stay at the top on purpose: burying the drop folder is exactly the
# problem that made teammates paste files into the conversation instead (see
# COSTAR_DROP_DIR below for what that cost).
SHARED_OUTPUT_DIR = SHARED_DIR / "output"
SHARED_SYSTEM_DIR = SHARED_DIR / "system"

PROXIMITY_OUTPUT_DIR  = SHARED_OUTPUT_DIR / "proximity"
SCREENING_OUTPUT_DIR  = SHARED_OUTPUT_DIR / "screening"

PROXIMITY_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SCREENING_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Basemap tiles and Overpass/federal lookups, cached per rounded bounding box
# so two properties in the same area don't re-fetch the same data. Was
# hardcoded as `Path(SHARED_DIR) / "geo_cache"` in three separate modules,
# against this file's own "nothing else hardcodes a path" rule -- which is
# also why it was the one shared folder that couldn't be moved without
# hunting down every copy.
GEO_CACHE_DIR = SHARED_SYSTEM_DIR / "geo_cache"
GEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Per-property research summaries -- built 2026-07-30 as a byproduct of
# vaulter-document-reader's normal work, never a separate ingestion pass.
# The first real question about a property costs a full document read; that
# agent writes what it found here, cited, so every later question (from any
# user) reads a few hundred tokens instead of re-reading the same documents.
# Deliberately lazy and demand-driven -- properties nobody asks about never
# get a file here, so this never becomes a second full-corpus copy.
PROPERTY_SUMMARIES_DIR = SHARED_OUTPUT_DIR / "property_summaries"
PROPERTY_SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)

# The firm's own portfolio data: the Smartsheet Project Master export, the
# hand-verified coordinates table, and the property-details fallback list.
#
# SHARED as of 2026-08-03, and for a measured reason. The handoff package
# (scripts/build_handoff.py) deliberately ships no firm data, so a brand-new
# teammate's install had none of this -- `python main.py stats` on a fresh
# machine reported "Portfolio: unavailable", every property question came back
# empty, and proximity-by-name refused for every property, until someone
# hand-delivered them files. Dropping the Smartsheet export here once makes it
# work for the whole team, through the same OneDrive folder they already trust
# with screening and proximity output.
#
# Local copies under DATA_DIR/project_master still win (see portfolio.py's
# _portfolio_dirs) -- this is the fallback that makes a fresh install useful,
# not a replacement for a file someone deliberately put on their own machine.
SMARTSHEET_PORTFOLIO_DIR = SHARED_DIR / "Smartsheet Portfolio"
SMARTSHEET_PORTFOLIO_DIR.mkdir(parents=True, exist_ok=True)

# Where CoStar exports and broker spreadsheets get dropped for screening.
#
# SHARED as of 2026-08-03, and for a measured reason. The local drop folder
# (DROP_DIR below) sits at <install>/system/data/drop -- somewhere no
# non-technical person will ever navigate to. The observed consequence: a
# teammate attached exports to the Claude conversation instead, so Claude
# base64-encoded them and passed them through as file_content_b64, which cost
# ~43,000 tokens for one real 216-row export purely to hand the file over. A
# folder people can actually find is the fix for that.
#
# In OneDrive beside every other Vaulter AI folder, so "everything this system
# touches lives in Vaulter AI Shared" stays true and there is one place to
# look. Shared also means one person's export is screenable by the whole team
# without re-sending it. DROP_DIR still works and is still searched first --
# see _resolve_costar_source -- so nothing breaks on a machine that was
# already using it.
COSTAR_DROP_DIR = SHARED_DIR / "CoStar Drop"
COSTAR_DROP_DIR.mkdir(parents=True, exist_ok=True)

# Priority 4 (docs/MULTI_USER_TRANSITION.md) — auto-update. UPDATES_DIR is
# where release.py (run by whoever ships a reviewed fix) publishes a new
# version's code package + version marker; every instance's scheduler
# reads from there. Deliberately the same shared OneDrive location as
# everything else shared across the team, not a new channel.
# PENDING_UPDATE_DIR is LOCAL (per machine) -- where an update gets
# staged once downloaded, before a human decides to actually apply it.
UPDATES_DIR        = SHARED_SYSTEM_DIR / "updates"
PENDING_UPDATE_DIR = DATA_DIR / "pending_update"

UPDATES_DIR.mkdir(parents=True, exist_ok=True)
PENDING_UPDATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Org-wide settings (e.g. a new feature's API key) distribution -- a
# separate channel from the code-update one above, on purpose. A code
# update package deliberately never includes confidentials/ (see
# release.py's EXCLUDED_DIR_NAMES) specifically so one person's own
# filled-in .env can never accidentally ship to every other
# instance. Org-wide settings need
# the opposite property -- everyone SHOULD end up with the same
# value -- so this uses its own small, deliberate publish tool
# (scripts/push_org_setting.py, run by the maintainer only) rather
# than reusing release.py's blanket packaging.
# ORG_SETTINGS_DIR is SHARED (every instance reads it, from
# check_system_health -- there is no scheduler any more).
# PENDING_SETTINGS_DIR is LOCAL -- staged here once downloaded, and
# only written into confidentials/.env after a human says yes (see
# apply_pending_settings in mcp_server.py).
ORG_SETTINGS_DIR     = SHARED_SYSTEM_DIR / "org_settings"
PENDING_SETTINGS_DIR = DATA_DIR / "pending_settings"

ORG_SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
PENDING_SETTINGS_DIR.mkdir(parents=True, exist_ok=True)

# Which release channel this instance follows -- "general" (the default)
# only picks up versions that have been explicitly promoted after a
# canary check; "canary" picks up every new release immediately, before
# it's been confirmed healthy anywhere else. Set on a small number of
# designated machines only (e.g. the maintainer's own), via
# confidentials/.env -- most instances should stay on "general".
VAULTER_UPDATE_CHANNEL = os.getenv("VAULTER_UPDATE_CHANNEL", "general").strip().lower()
if VAULTER_UPDATE_CHANNEL not in ("general", "canary"):
    VAULTER_UPDATE_CHANNEL = "general"

# ─── OCR Settings ─────────────────────────────────────────────────
# Auto-detected rather than hardcoded to one exact install location --
# a hardcoded exact-version path (e.g. a specific "poppler-26.02.0")
# silently broke for anyone whose installer put a different version
# anywhere else, and a hardcoded Homebrew path broke on Intel Macs
# (Homebrew installs to /usr/local on Intel, /opt/homebrew on Apple
# Silicon -- the old hardcoded path only ever covered the latter).
#
# Searches PATH first (covers a standard install on either platform/
# architecture), then a few common install locations, including the
# OLD hardcoded paths so a setup that predates this fix keeps working
# unchanged. TESSERACT_PATH falls back to plain "tesseract" and
# POPPLER_PATH to None if genuinely not found anywhere -- extractor.py's
# pytesseract/pdf2image calls already treat both as "just search PATH at
# the moment OCR actually runs" in that case, so this degrades to
# exactly the same behavior a bare `tesseract`/`pdftoppm` on PATH would
# give, rather than crashing on an invalid hardcoded path. Only
# scanned-PDF OCR is affected if the tools truly aren't installed;
# digital-text PDFs never touch this at all.


def _find_executable(names: list, extra_dirs: list) -> str:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for d in extra_dirs:
        for name in names:
            candidate = d / name
            if candidate.exists():
                return str(candidate)
    return ""


def _find_poppler_bin_dir(extra_dirs: list) -> str:
    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm:
        return str(Path(pdftoppm).parent)
    for d in extra_dirs:
        if (d / "pdftoppm.exe").exists() or (d / "pdftoppm").exists():
            return str(d)
    return ""


if sys.platform == "win32":
    _username = os.environ.get("USERNAME", "YourName")
    _tesseract_dirs = [
        Path(r"C:\Program Files\Tesseract-OCR"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR"),
        Path(r"C:\Users") / _username / r"AppData\Local\Programs\Tesseract-OCR",
        Path(r"C:\Users") / _username / r"Packages\Tesseract-OCR",  # old hardcoded location
    ]
    TESSERACT_PATH = _find_executable(["tesseract.exe", "tesseract"], _tesseract_dirs) or "tesseract"

    # setup_wizard.py's auto-installer (2026-08-03) extracts the official
    # release zip to AppData\Local\Programs\poppler\poppler-<version>\Library\bin
    # -- verified against the real release's own internal folder layout, not
    # assumed. Checked first since it's where a fresh auto-install lands;
    # Packages\poppler* is kept for any machine set up before that existed.
    _poppler_dirs = []
    _programs_poppler_dir = Path(r"C:\Users") / _username / r"AppData\Local\Programs\poppler"
    if _programs_poppler_dir.exists():
        _poppler_dirs += [d / "Library" / "bin" for d in _programs_poppler_dir.glob("poppler*") if d.is_dir()]
    _packages_dir = Path(r"C:\Users") / _username / "Packages"
    if _packages_dir.exists():
        _poppler_dirs += [d / "Library" / "bin" for d in _packages_dir.glob("poppler*") if d.is_dir()]
    POPPLER_PATH = _find_poppler_bin_dir(_poppler_dirs) or None
else:
    _mac_dirs = [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    TESSERACT_PATH = _find_executable(["tesseract"], _mac_dirs) or "tesseract"
    POPPLER_PATH = _find_poppler_bin_dir(_mac_dirs) or None

if TESSERACT_PATH == "tesseract" and not shutil.which("tesseract"):
    print("WARNING: Tesseract OCR was not found anywhere -- scanned/image-only PDF pages "
          "will not be readable until it's installed. See README.md's Setup section.",
          file=sys.stderr)
if POPPLER_PATH is None:
    print("WARNING: Poppler was not found anywhere -- scanned/image-only PDF pages "
          "will not be readable until it's installed. See README.md's Setup section.",
          file=sys.stderr)

# ══════════════════════════════════════════════════════════════════
# API Keys — there are none
# ══════════════════════════════════════════════════════════════════
# This project calls no paid or keyed service. Document search is local,
# ranking is arithmetic, ground truth is federal open data (FEMA, Census,
# USGS), proximity is OpenStreetMap, and the qualitative read happens in the
# Claude conversation that asked for it. A blank confidentials/.env works.
#
# What used to be here, and why it went:
#   OUTLOOK_CLIENT_ID / TENANT_ID / CLIENT_SECRET -- email ingestion removed.
#   GOOGLE_MAPS_API_KEY   -- ground truth is federal open data now.
#   ANTHROPIC_API_KEY     -- the 4-phase pipeline made its own Claude API
#       calls; it is deleted, and nothing here calls an LLM.
#   GOOGLE_PLACES_API_KEY -- the proximity tool moved to keyless Overpass and
#       stopped reading it. The constant outlived its last reader.
#
# Adding a key back means adding a real dependency on someone's billing. Weigh
# that against a free equivalent first -- every one of the above had one.

# ══════════════════════════════════════════════════════════════════
# MCP Server
# ══════════════════════════════════════════════════════════════════

# No API key or port here on purpose: each staff member runs their own
# fully-local instance of this server, launched directly by their own
# Claude Desktop via stdio (see mcp_server.py's header). Nothing is
# exposed over a network, so there's no request to gate with a shared
# secret and no port to listen on -- the real access boundary is simply
# "is this your own computer, logged in as you."

# ─── Proximity Search ────────────────────────────────────────────
# Categories and settings for the proximity_search MCP tool.
# Edit PROXIMITY_CATEGORIES to add/remove/change search categories.
#
# `osm_tags` is what the tool actually queries — OpenStreetMap key=value
# pairs, fetched via Overpass, free and keyless. `google_types` is retained
# only as documentation of what each category originally meant under the
# Google Places API; nothing reads it any more.
# OSM tag reference: https://wiki.openstreetmap.org/wiki/Map_features
#
# Note the ordering matters: a feature is classified into the FIRST category
# whose tags it matches (see proximity_tool._classify), so narrower
# categories must come before broader ones that would also match them.

PROXIMITY_DEFAULT_RADIUS_MILES          = 5.0
PROXIMITY_SUMMARY_RESULTS_PER_CATEGORY  = 10
PROXIMITY_GEOCODING_TIMEOUT             = 10
PROXIMITY_PLACES_REQUEST_DELAY          = 0.15

PROXIMITY_CATEGORIES = [
    {"label": "Shopping Mall & Outlets",       "icon": "🏬", "color": "#C0392B",
     "google_types": ["shopping_mall"],
     "osm_tags": ["shop=mall"]},
    {"label": "Grocery & Specialty Food",      "icon": "🛍️", "color": "#27AE60",
     "google_types": ["grocery_or_supermarket", "supermarket"],
     "osm_tags": ["shop=supermarket", "shop=greengrocer", "shop=butcher"]},
    {"label": "Retail & Big Box",              "icon": "🛒", "color": "#E74C3C",
     "google_types": ["supermarket", "department_store", "shopping_mall",
                      "home_goods_store", "hardware_store", "warehouse_store"],
     "osm_tags": ["shop=department_store", "shop=doityourself", "shop=hardware",
                  "shop=wholesale", "shop=furniture", "shop=variety_store"]},
    {"label": "Hospitality",                   "icon": "🏨", "color": "#9B59B6",
     "google_types": ["lodging"],
     "osm_tags": ["tourism=hotel", "tourism=motel", "tourism=resort"]},
    {"label": "Industrial & Logistics",        "icon": "🏭", "color": "#F39C12",
     "google_types": ["storage", "moving_company"],
     "osm_tags": ["landuse=industrial", "building=warehouse",
                  "industrial=warehouse", "amenity=storage_rental"]},
    {"label": "Major Corporate HQ",            "icon": "🏢", "color": "#2C3E50",
     "google_types": ["corporate_office"],
     "osm_tags": ["office=company", "office=corporate"]},
    {"label": "Technology & Innovation",       "icon": "💻", "color": "#1A5276",
     "google_types": ["electronics_store"],
     "osm_tags": ["office=it", "shop=electronics", "office=research"]},
    {"label": "Healthcare",                    "icon": "🏥", "color": "#2ECC71",
     "google_types": ["hospital", "doctor", "pharmacy", "health"],
     "osm_tags": ["amenity=hospital", "amenity=clinic", "amenity=doctors",
                  "amenity=pharmacy"]},
    {"label": "School & University",           "icon": "🎓", "color": "#3498DB",
     "google_types": ["school", "university", "secondary_school"],
     "osm_tags": ["amenity=school", "amenity=university", "amenity=college"]},
    {"label": "Military Base",                 "icon": "🪖", "color": "#6D4C41",
     "google_types": ["local_government_office"],
     "osm_tags": ["landuse=military", "military=base"]},
    {"label": "Government & Civic",            "icon": "🏛️", "color": "#5D6D7E",
     "google_types": ["city_hall", "local_government_office",
                      "courthouse", "post_office", "fire_station"],
     "osm_tags": ["amenity=townhall", "amenity=courthouse", "amenity=post_office",
                  "amenity=fire_station", "amenity=police", "office=government"]},
    {"label": "Sports & Entertainment",        "icon": "🏟️", "color": "#E67E22",
     "google_types": ["stadium", "amusement_park", "movie_theater", "casino"],
     "osm_tags": ["leisure=stadium", "tourism=theme_park", "amenity=cinema",
                  "amenity=casino", "leisure=sports_centre"]},
    {"label": "Gas & Convenience",             "icon": "⛽", "color": "#F1C40F",
     "google_types": ["gas_station", "convenience_store"],
     "osm_tags": ["amenity=fuel", "shop=convenience"]},
    {"label": "Restaurant & QSR",              "icon": "🍔", "color": "#1ABC9C",
     "google_types": ["restaurant", "meal_takeaway", "cafe", "bakery"],
     "osm_tags": ["amenity=restaurant", "amenity=fast_food", "amenity=cafe",
                  "shop=bakery"]},
    {"label": "Financial Services",            "icon": "🏦", "color": "#2E86C1",
     "google_types": ["bank", "atm"],
     "osm_tags": ["amenity=bank", "amenity=atm"]},
    {"label": "Parks & Recreation",            "icon": "🌳", "color": "#229954",
     "google_types": ["park", "campground"],
     "osm_tags": ["leisure=park", "tourism=camp_site", "leisure=nature_reserve"]},
    {"label": "Transportation & Infrastructure","icon": "🛣️", "color": "#717D7E",
     "google_types": ["transit_station", "bus_station", "train_station",
                      "airport", "subway_station"],
     "osm_tags": ["amenity=bus_station", "railway=station", "aeroway=aerodrome",
                  "public_transport=station"]},
    # No Google equivalent -- OSM maps physical infrastructure that Google
    # Places largely does not, and for raw land these nuisance/utility
    # factors often matter more than nearby businesses.
    {"label": "Utilities & Nuisance",          "icon": "⚡", "color": "#8E44AD",
     "google_types": [],
     "osm_tags": ["power=substation", "power=plant", "man_made=wastewater_plant",
                  "amenity=waste_transfer_station", "landuse=landfill",
                  "landuse=quarry", "man_made=water_tower"]},
]

# Logging: there was a LOG_LEVEL constant here, read only by the scrapers and
# the scheduler. Those are gone; main.py sets its own level directly.
