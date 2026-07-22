"""
config.py
---------
Central configuration for the Vaulter AI Property Intelligence System.
All paths, settings, and constants live here.

Cross-platform: automatically detects Windows or Mac and sets the correct paths.
To adapt this project to a new machine, only this file needs to be updated.

Secrets (.env and outlook_token.json) are stored in:
  Windows : C:/Users/<YourName>/Vaulter AI/confidentials/
  Mac     : <project_root>/confidentials/

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

# ─── Shared Folder (OneDrive) ─────────────────────────────────────
# Each staff member runs their own fully-local Vaulter instance (see
# mcp_server.py header), but a few things are meant to be genuinely shared
# across the whole team via the company OneDrive -- e.g. CoStar screening
# results, so one person's screening run benefits everyone instead of each
# person re-paying for it. This does NOT include the private stuff
# (Outlook auth, someone's own email) -- those stay local on purpose.
#
# Auto-detects "OneDrive - Vaulter LLC" (the standard OneDrive-for-Business
# naming -- same folder name for everyone, different C:\Users\<name>\ per
# person). Override with VAULTER_SHARED_DIR in confidentials/.env if your
# OneDrive folder is named or located differently.

_LOCAL_FALLBACK_DIR = (BASE_DIR / "data" / "shared_fallback_not_synced").resolve()


def _detect_shared_dir() -> Path:
    override = os.getenv("VAULTER_SHARED_DIR", "").strip()
    if override:
        return Path(override)

    onedrive_folder_name = "OneDrive - Vaulter LLC"
    candidates = []
    if sys.platform == "win32":
        username = os.environ.get("USERNAME", "YourName")
        candidates.append(Path(r"C:\Users") / username / onedrive_folder_name)
    else:
        home = Path.home()
        # Modern OneDrive for Mac syncs under ~/Library/CloudStorage/;
        # older versions/some configs use ~/<OneDrive folder name> directly.
        candidates.append(home / "Library" / "CloudStorage" / f"OneDrive-{onedrive_folder_name.replace('OneDrive - ', '').replace(' ', '')}")
        candidates.append(home / onedrive_folder_name)

    for candidate in candidates:
        if candidate.exists():
            return candidate / "Vaulter AI Shared"

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

# ─── Data Folders ─────────────────────────────────────────────────

DATA_DIR       = (BASE_DIR / "data").resolve()
WATCH_DIR      = DATA_DIR / "watched_folder"
PROCESSED_DIR  = DATA_DIR / "processed"
CHROMA_DIR     = DATA_DIR / "chroma_db"
LOG_DIR        = DATA_DIR / "logs"
REGISTRY_DIR   = DATA_DIR / "registry"
REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
REGISTRY_FILE  = REGISTRY_DIR / "ingested_registry.json"

# CoStar listing screener (analysis/screening/) — uploaded/pasted source
# files land in SCREENING_UPLOADS_DIR (local, since an upload is specific to
# whoever pasted it into their own conversation). Combined workbooks +
# manifest.json land in SCREENING_OUTPUT_DIR, which is SHARED (under
# SHARED_DIR) on purpose -- so one person's screening run is visible to the
# whole team instead of sitting only on their own machine.
OUTPUT_DIR            = DATA_DIR / "output"
PROXIMITY_OUTPUT_DIR  = OUTPUT_DIR / "proximity"
SCREENING_OUTPUT_DIR  = SHARED_DIR / "screening_output"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
PROXIMITY_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SCREENING_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

CHROMA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ─── Chunking Settings ────────────────────────────────────────────
# Only CHUNK_TIERS is actually consulted (via get_chunk_settings) --
# there used to be flat CHUNK_SIZE/CHUNK_OVERLAP constants here too, but
# nothing read them; removed rather than leave a footgun for a future
# maintainer editing them and seeing no effect.

CHUNK_TIERS = [
    (10,   500,   50),
    (50,   800,  100),
    (100, 1200,  150),
    (999, 1500,  200),
]

# extractor.py sets page_count to EXACTLY this value (never a real page
# count) to mark Excel/structured data, so chunking keeps whole rows
# intact -- CoStar rows average 1,600 chars, max ~3,000 -- instead of
# hard-splitting pipe-separated data at a 500-1500 char boundary. This is
# checked as an exact match, deliberately NOT folded into CHUNK_TIERS as
# a "<=" upper tier -- a genuinely huge real PDF (1000-9998 actual pages)
# must fall through to the tiers above instead of incorrectly getting
# Excel-sized 8000-char chunks meant for something else entirely.
EXCEL_SENTINEL_PAGE_COUNT = 9999
EXCEL_CHUNK_SIZE, EXCEL_CHUNK_OVERLAP = 8000, 200

def get_chunk_settings(page_count: int) -> tuple[int, int]:
    if page_count == EXCEL_SENTINEL_PAGE_COUNT:
        return EXCEL_CHUNK_SIZE, EXCEL_CHUNK_OVERLAP
    for max_pages, chunk_size, overlap in CHUNK_TIERS:
        if page_count <= max_pages:
            return chunk_size, overlap
    return 1500, 200

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

    _poppler_dirs = []
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

# ─── ChromaDB ─────────────────────────────────────────────────────

CHROMA_COLLECTION_NAME = "vaulter_documents"

# ─── Embedding ────────────────────────────────────────────────────

EMBEDDING_DIM  = 384

# ══════════════════════════════════════════════════════════════════
# Stage 2 — Web & Email Pipeline
# ══════════════════════════════════════════════════════════════════

RAW_WEB_DIR   = DATA_DIR / "raw_web"
RAW_EMAIL_DIR = DATA_DIR / "raw_email"

WEB_SOURCES = [
    {
        "name": "CBRE US Market Outlook 2026",
        "url": "https://www.cbre.com/insights/books/us-real-estate-market-outlook-2026",
        "frequency_hours": 24,
        "tags": ["p", "h2", "h3"],
    },
    {
        "name": "CBRE Capital Markets 2026",
        "url": "https://www.cbre.com/insights/books/us-real-estate-market-outlook-2026/capital-markets",
        "frequency_hours": 24,
        "tags": ["p", "h2", "h3"],
    },
    {
        "name": "Marcus & Millichap Research",
        "url": "https://www.marcusmillichap.com/research",
        "frequency_hours": 24,
        "tags": ["p", "h3"],
    },
    {
        "name": "GlobeSt CRE News",
        "url": "https://www.globest.com/sectors/",
        "frequency_hours": 12,
        "tags": ["article", "p", "h2", "h3"],
    },
    {
        "name": "GlobeSt Homepage",
        "url": "https://www.globest.com/",
        "frequency_hours": 12,
        "tags": ["article", "p", "h2", "h3"],
    },
]

SCHEDULER_TIMEZONE = "America/Phoenix"

# Web/property-intelligence scraping hits the same small set of public
# pages regardless of which staff member's instance runs it -- since
# everyone runs their own full local instance (see mcp_server.py header),
# leaving this on everywhere means the same handful of pages get scraped
# once per PERSON, every cycle, for no benefit (the content is identical).
# Set to false in confidentials/.env on every machine except the one
# designated to do this team's scraping; email stays per-person always
# (it's correctly scoped to each person's own mailbox, never duplicated).
RUN_SCHEDULED_SCRAPING = os.getenv("RUN_SCHEDULED_SCRAPING", "true").strip().lower() != "false"

# ─── Outlook / Microsoft Graph ────────────────────────────────────
# Add to confidentials/.env:
#   OUTLOOK_CLIENT_ID=your-application-id
#   OUTLOOK_TENANT_ID=your-directory-id
#   OUTLOOK_CLIENT_SECRET=your-client-secret

OUTLOOK_CLIENT_ID     = os.getenv("OUTLOOK_CLIENT_ID", "")
OUTLOOK_TENANT_ID     = os.getenv("OUTLOOK_TENANT_ID", "")
OUTLOOK_CLIENT_SECRET = os.getenv("OUTLOOK_CLIENT_SECRET", "")
OUTLOOK_TOKEN_FILE    = SECRETS_DIR / "outlook_token.json"
OUTLOOK_FOLDERS       = ["Inbox"]
OUTLOOK_SENDER_WHITELIST = []
OUTLOOK_LOOKBACK_DAYS = 30

# ─── Anthropic / Claude API ───────────────────────────────────────
# Add to confidentials/.env:
#   ANTHROPIC_API_KEY=sk-ant-...

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ─── Google Places API ────────────────────────────────────────────
# Add to confidentials/.env:
#   GOOGLE_PLACES_API_KEY=AIzaSy...

GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")

# ─── Google Maps Platform (CoStar Screener — Phase 4 verification) ────
# Add to confidentials/.env:
#   GOOGLE_MAPS_API_KEY=AIzaSy...
# Needs Elevation, Places, Roads, Geocoding, Distance Matrix, Static Maps,
# Street View Static, Solar, Address Validation, and Air Quality enabled
# (whichever subset is enabled, Phase 4 auto-detects and uses only those).
# If unset, screen_listings still runs Phases 1-3 and Phase 4's finalist
# selection, just skips the Google ground-truth enrichment step.

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")

# ══════════════════════════════════════════════════════════════════
# Stage 3 — MCP Server
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
# google_types reference: https://developers.google.com/maps/documentation/places/web-service/supported_types

PROXIMITY_DEFAULT_RADIUS_MILES          = 5.0
PROXIMITY_SUMMARY_RESULTS_PER_CATEGORY  = 10
PROXIMITY_GEOCODING_TIMEOUT             = 10
PROXIMITY_PLACES_REQUEST_DELAY          = 0.15

PROXIMITY_CATEGORIES = [
    {"label": "Retail & Big Box",              "icon": "🛒", "color": "#E74C3C",
     "google_types": ["supermarket", "department_store", "shopping_mall",
                      "home_goods_store", "hardware_store", "warehouse_store"]},
    {"label": "Shopping Mall & Outlets",       "icon": "🏬", "color": "#C0392B",
     "google_types": ["shopping_mall"]},
    {"label": "Hospitality",                   "icon": "🏨", "color": "#9B59B6",
     "google_types": ["lodging"]},
    {"label": "Industrial & Logistics",        "icon": "🏭", "color": "#F39C12",
     "google_types": ["storage", "moving_company"]},
    {"label": "Major Corporate HQ",            "icon": "🏢", "color": "#2C3E50",
     "google_types": ["corporate_office"]},
    {"label": "Technology & Innovation",       "icon": "💻", "color": "#1A5276",
     "google_types": ["electronics_store"]},
    {"label": "Healthcare",                    "icon": "🏥", "color": "#2ECC71",
     "google_types": ["hospital", "doctor", "pharmacy", "health"]},
    {"label": "School & University",           "icon": "🎓", "color": "#3498DB",
     "google_types": ["school", "university", "secondary_school"]},
    {"label": "Government & Civic",            "icon": "🏛️", "color": "#5D6D7E",
     "google_types": ["city_hall", "local_government_office",
                      "courthouse", "post_office", "fire_station"]},
    {"label": "Military Base",                 "icon": "🪖", "color": "#6D4C41",
     "google_types": ["local_government_office"]},
    {"label": "Sports & Entertainment",        "icon": "🏟️", "color": "#E67E22",
     "google_types": ["stadium", "amusement_park", "movie_theater", "casino"]},
    {"label": "Restaurant & QSR",              "icon": "🍔", "color": "#1ABC9C",
     "google_types": ["restaurant", "meal_takeaway", "cafe", "bakery"]},
    {"label": "Grocery & Specialty Food",      "icon": "🛍️", "color": "#27AE60",
     "google_types": ["grocery_or_supermarket", "supermarket"]},
    {"label": "Gas & Convenience",             "icon": "⛽", "color": "#F1C40F",
     "google_types": ["gas_station", "convenience_store"]},
    {"label": "Financial Services",            "icon": "🏦", "color": "#2E86C1",
     "google_types": ["bank", "atm"]},
    {"label": "Parks & Recreation",            "icon": "🌳", "color": "#229954",
     "google_types": ["park", "campground"]},
    {"label": "Transportation & Infrastructure","icon": "🛣️", "color": "#717D7E",
     "google_types": ["transit_station", "bus_station", "train_station",
                      "airport", "subway_station"]},
]

# ─── Logging ──────────────────────────────────────────────────────

LOG_LEVEL = "INFO"
