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
import sys
from pathlib import Path
from dotenv import load_dotenv

# ─── Project Root ─────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent

# ─── Secrets Folder ───────────────────────────────────────────────
if sys.platform == "win32":
    SECRETS_DIR = Path(r"C:\Users") / os.environ.get("USERNAME", "YourName") / "Vaulter AI" / "confidentials"
else:
    SECRETS_DIR = BASE_DIR / "confidentials"

SECRETS_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(SECRETS_DIR / ".env")

# ─── Data Folders ─────────────────────────────────────────────────

DATA_DIR       = (BASE_DIR / "data").resolve()
WATCH_DIR      = DATA_DIR / "watched_folder"
PROCESSED_DIR  = DATA_DIR / "processed"
CHROMA_DIR     = DATA_DIR / "chroma_db"
LOG_DIR        = DATA_DIR / "logs"
REGISTRY_FILE  = DATA_DIR / "ingested_registry.json"

CHROMA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ─── Chunking Settings ────────────────────────────────────────────

CHUNK_SIZE    = 800
CHUNK_OVERLAP = 100

CHUNK_TIERS = [
    (10,   500,   50),
    (50,   800,  100),
    (100, 1200,  150),
    (999, 1500,  200),
    # Sentinel for Excel/structured data (page_count=9999 set by extractor).
    # Keeps entire rows intact — CoStar rows average 1,600 chars, max ~3,000.
    # Without this, the chunker hard-splits rows at 500-1500 char boundaries,
    # fragmenting pipe-separated data and breaking row extraction.
    (9999, 8000, 200),
]

def get_chunk_settings(page_count: int) -> tuple[int, int]:
    for max_pages, chunk_size, overlap in CHUNK_TIERS:
        if page_count <= max_pages:
            return chunk_size, overlap
    return 1500, 200

# ─── OCR Settings ─────────────────────────────────────────────────

if sys.platform == "win32":
    TESSERACT_PATH = str(Path(r"C:\Users") / os.environ.get("USERNAME", "YourName") / r"Packages\Tesseract-OCR\tesseract.exe")
    POPPLER_PATH   = str(Path(r"C:\Users") / os.environ.get("USERNAME", "YourName") / r"Packages\poppler-26.02.0\Library\bin")
else:
    TESSERACT_PATH = "/opt/homebrew/bin/tesseract"
    POPPLER_PATH   = "/opt/homebrew/bin"

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

# ══════════════════════════════════════════════════════════════════
# Stage 3 — MCP Server
# ══════════════════════════════════════════════════════════════════

# Secret key that Claude.ai must send with every MCP request.
# Set this in confidentials/.env:
#   MCP_API_KEY=vaulter_mcp_your_random_string_here
#
# Generate one with: python -c "import secrets; print(secrets.token_hex(24))"

MCP_API_KEY = os.getenv("MCP_API_KEY", "")
MCP_PORT    = int(os.getenv("MCP_PORT", "8765"))

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
