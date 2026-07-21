"""
ingestion/watcher.py
--------------------
Monitors the watched_folder for new files and triggers the ingestion
pipeline automatically when a new supported file is detected.

Folder structure (required):
  data/watched_folder/
    <State>/
      <Property Name>/
        file.pdf

Examples:
  data/watched_folder/Arizona/Magic Ranch 10/survey.pdf
  data/watched_folder/New Mexico/Mesa Del Sol/ESA.pdf
  data/watched_folder/California/Cabazon/alta.pdf

The state and property are read directly from the folder path — no fuzzy
filename matching. Both are validated against the Project Master to get the
correct category tag and catch folder name typos.

Folder routing after ingestion:
  Active properties  → processed/<state>/
  Sold properties    → processed/sold/<state>/
  Unknown/unmatched  → processed/unknown/
"""

import logging
import shutil
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import safe_io
from config import WATCH_DIR, PROCESSED_DIR, get_chunk_settings
from ingestion.extractor import extract, is_supported
from ingestion.chunker import chunk_text
from ingestion.embedder import store_chunks
from ingestion.registry import (
    get_file_hash,
    is_already_ingested,
    record_ingestion,
)

log = logging.getLogger("vaulter.watcher")

# ─── Property List Cache ──────────────────────────────────────────────────────
# Cached against the Project Master file's mtime rather than forever: a team
# member re-exporting an updated Smartsheet Project Master into
# data/project_master/ (new/renamed/sold properties) is picked up on the next
# file event without needing a full watcher/MCP-server restart, while repeat
# calls between edits still hit the cache instead of re-parsing every time.
_PROPERTIES:              list[dict] | None = None
_SOLD_PROPERTIES:         list[dict] | None = None
_PROPERTIES_SOURCE_MTIME: float | None = "unset"  # sentinel distinct from a real None mtime

# Valid state folder names — derived dynamically from the property list.
_VALID_STATES_CACHE: set | None = None


def _project_master_mtime():
    """Current Project Master file's mtime, or None if no file exists yet."""
    try:
        from pipeline.property_scraper import find_project_file
        file = find_project_file()
        return file.stat().st_mtime if file else None
    except Exception:
        return None


def _load_properties() -> tuple[list[dict], list[dict]]:
    global _PROPERTIES, _SOLD_PROPERTIES, _PROPERTIES_SOURCE_MTIME, _VALID_STATES_CACHE

    current_mtime = _project_master_mtime()
    if _PROPERTIES is not None and current_mtime == _PROPERTIES_SOURCE_MTIME:
        return _PROPERTIES, _SOLD_PROPERTIES

    try:
        from pipeline.property_scraper import load_all_properties
        _PROPERTIES, _SOLD_PROPERTIES = load_all_properties()
        log.info(f"Properties loaded: {len(_PROPERTIES)} active, {len(_SOLD_PROPERTIES)} sold")
    except FileNotFoundError:
        log.warning("No Project Master found — category will be tagged as unknown.")
        _PROPERTIES      = []
        _SOLD_PROPERTIES = []
    except Exception as e:
        log.warning(f"Could not load properties: {e}")
        _PROPERTIES      = []
        _SOLD_PROPERTIES = []

    _PROPERTIES_SOURCE_MTIME = current_mtime
    _VALID_STATES_CACHE      = None  # derived from the property list -- recompute alongside it
    return _PROPERTIES, _SOLD_PROPERTIES


def _get_valid_states() -> set:
    """Return the set of valid state folder names from the live property list."""
    global _VALID_STATES_CACHE
    if _VALID_STATES_CACHE is not None:
        return _VALID_STATES_CACHE
    active, sold = _load_properties()
    states = {p["state"].lower() for p in active + sold if p.get("state")}
    # Fallback if Project Master not yet available
    _VALID_STATES_CACHE = states or {"arizona", "california", "new mexico", "colorado", "texas"}
    return _VALID_STATES_CACHE


# ─── Folder-Based Property Resolution ────────────────────────────────────────

def _resolve_from_path(path: Path) -> dict:
    """
    Read state and property directly from the folder structure:
      watched_folder / <State> / <Property Name> / file.pdf

    Validates both against the Project Master to get category.
    Returns a match dict compatible with the old _match_property() output.
    """
    parts = path.parts  # e.g. [..., 'watched_folder', 'Arizona', 'Magic Ranch 10', 'file.pdf']

    # Find watched_folder in the path
    try:
        wf_idx = next(i for i, p in enumerate(parts) if p == "watched_folder")
    except StopIteration:
        log.warning(f"  [WARN] File not inside watched_folder: {path}")
        return _unknown()

    remaining = parts[wf_idx + 1:]  # e.g. ('Arizona', 'Magic Ranch 10', 'file.pdf')

    if len(remaining) < 3:
        # File dropped directly in watched_folder or a state folder — no property folder
        if len(remaining) == 1:
            log.warning(f"  [WARN] Drop files into State/Property subfolders, not directly in watched_folder")
        elif len(remaining) == 2:
            log.warning(f"  [WARN] Drop files into a Property subfolder inside the State folder")
        return _unknown()

    folder_state    = remaining[0]   # e.g. "Arizona"
    folder_property = remaining[1]   # e.g. "Magic Ranch 10"

    # Validate state
    if folder_state.lower() not in _get_valid_states():
        log.warning(f"  [WARN] Unrecognised state folder '{folder_state}' — expected one of: {', '.join(sorted(_get_valid_states()))}")
        return _unknown()

    # Normalise state to lowercase_underscore for storage (matches existing convention)
    state_key = folder_state.lower().replace(" ", "_")

    # Look up property in project master to get category and status
    active_props, sold_props = _load_properties()

    # Case-insensitive match on property name
    def find_prop(prop_list, name):
        name_l = name.lower()
        return next((p for p in prop_list if p["name"].lower() == name_l), None)

    match = find_prop(active_props, folder_property)
    if match:
        return {
            "property": match["name"],
            "state":    state_key,
            "category": match.get("category", "unknown"),
            "status":   "active",
            "matched":  True,
        }

    match = find_prop(sold_props, folder_property)
    if match:
        return {
            "property": match["name"],
            "state":    state_key,
            "category": match.get("category", "unknown"),
            "status":   "sold",
            "matched":  True,
        }

    # Property folder name not in project master — still tag it, just warn
    log.warning(
        f"  [WARN] '{folder_property}' not found in Project Master — "
        f"tagging as-is. Check spelling matches the Project Master exactly."
    )
    return {
        "property": folder_property,
        "state":    state_key,
        "category": "unknown",
        "status":   "active",
        "matched":  True,  # we know state+property from folder, just not category
    }


def _unknown() -> dict:
    return {
        "property": "unknown",
        "state":    "unknown",
        "category": "unknown",
        "status":   "unknown",
        "matched":  False,
    }


# ─── Folder Structure Setup ───────────────────────────────────────────────────

def create_property_folders():
    """
    Create state/property subfolders in watched_folder based on the Project Master.
    Safe to run multiple times — skips folders that already exist.
    """
    active_props, _ = _load_properties()
    if not active_props:
        log.warning("No properties loaded — cannot create folders.")
        return

    created = 0
    for prop in active_props:
        state    = prop.get("state", "").strip()
        name     = prop.get("name", "").strip()
        if not state or not name:
            continue
        folder = WATCH_DIR / state / name
        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=True)
            created += 1

    log.info(f"Property folders ready ({created} new folders created in watched_folder/)")


# ─── Core Ingestion Function ──────────────────────────────────────────────────

def ingest_file(path: Path):
    """
    Full ingestion pipeline for a single file.
    Property identity is read from the folder path, not the filename.
    """
    log.info(f"[INGEST] {path.name}")

    if not is_supported(path):
        log.warning(f"  [SKIP] Unsupported file type: {path.suffix} — {path.name}")
        return

    try:
        # Step 0: Hash file. Deliberately inside this try — a file can
        # vanish between the watchdog event firing and this running (e.g.
        # a duplicate event for a file another event already moved to
        # processed/), which raises FileNotFoundError here. Letting that
        # escape would kill the watchdog dispatch thread silently.
        doc_hash = get_file_hash(path)

        # Step 1: Resolve property from folder path
        match = _resolve_from_path(path)

        # Step 1b: Dedup check, scoped to THIS property -- the same
        # physical file (e.g. a shared county plat map) dropped into a
        # different property's folder is a legitimate new ingestion for
        # that property, not a duplicate to be silently skipped and left
        # sitting in watched_folder forever.
        if is_already_ingested(doc_hash, match["property"], match["state"]):
            log.info(f"  [SKIP] Already ingested for {match['property']}: {path.name}")
            return

        if match["matched"]:
            log.info(f"  Property : {match['property']}")
            log.info(f"  State    : {match['state']}")
            log.info(f"  Category : {match['category']}")
            log.info(f"  Status   : {match['status']}")
        else:
            log.warning(f"  [WARN] Could not resolve property from path — tagged as unknown")

        # Step 2: Extract text
        log.info("  Extracting text...")
        text, metadata = extract(path)

        if not text.strip():
            log.warning(f"  [WARN] No text extracted from {path.name}")
            return

        method     = "OCR" if metadata.get("ocr_used") else "direct"
        page_count = metadata.get("page_count", 1)
        log.info(f"  Extracted {len(text):,} characters via {method} from {page_count} pages")

        # Step 3: Tag metadata with property info
        metadata["property"] = match["property"]
        metadata["state"]    = match["state"]
        metadata["category"] = match["category"]
        metadata["status"]   = match["status"]

        # Step 4: Chunk
        chunk_size, overlap = get_chunk_settings(page_count)
        log.info(f"  Chunking with size={chunk_size}, overlap={overlap} ({page_count} pages)")
        chunks = chunk_text(text, page_count=page_count)
        log.info(f"  Split into {len(chunks)} chunks")

        # Step 5: Store in ChromaDB
        store_chunks(chunks, metadata, doc_hash)

        # Step 6: Route to processed folder
        if match["status"] == "sold":
            dest_dir     = PROCESSED_DIR / "sold" / match["state"]
            folder_label = f"processed/sold/{match['state']}/"
        elif match["state"] != "unknown":
            dest_dir     = PROCESSED_DIR / match["state"] / match["property"]
            folder_label = f"processed/{match['state']}/{match['property']}/"
        else:
            dest_dir     = PROCESSED_DIR / "unknown"
            folder_label = "processed/unknown/"

        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = safe_io.dedupe_path(dest_dir, path.name)
        if dest_path.name != path.name:
            log.warning(
                f"  [WARN] '{path.name}' already exists in {folder_label} — "
                f"saving this one as '{dest_path.name}' instead of overwriting it"
            )
        shutil.move(str(path), str(dest_path))
        log.info(f"  Moved to {folder_label}")

        # Step 7: Record in registry
        record_ingestion(
            file_hash=doc_hash,
            filename=dest_path.name,
            chunks=len(chunks),
            pages=page_count,
            ocr_used=metadata.get("ocr_used", False),
            property_name=match["property"],
            state=match["state"],
            category=match["category"],
        )

        log.info(f"  [DONE] {path.name} ({len(chunks)} chunks, {folder_label.strip('/')}, method={method})\n")

    except Exception as e:
        log.error(f"  [ERROR] Failed to ingest {path.name}: {e}", exc_info=True)


# ─── File Settle Detection ────────────────────────────────────────────────────

def _wait_until_settled(path: Path, poll_interval: float = 0.5, stable_checks: int = 3, timeout: float = 120.0) -> bool:
    """
    Wait until a file's size stops changing before ingesting it.

    A fixed 1-second sleep works for small PDFs but isn't long enough for a
    large file still being copied/synced into watched_folder (e.g. a big
    scanned survey coming in over OneDrive) -- ingestion would then hash
    and extract a half-written file. Polls the file size instead: once it
    reads the same size on `stable_checks` consecutive checks, the file is
    considered done writing. Gives up after `timeout` seconds so a file
    that's genuinely still growing (or was deleted mid-wait) doesn't hang
    the watchdog dispatch thread forever.

    Returns False if the file disappeared before settling (e.g. a
    duplicate event for a file another event already moved to processed/).
    """
    deadline = time.monotonic() + timeout
    last_size = -1
    stable_count = 0

    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False

        if size == last_size:
            stable_count += 1
            if stable_count >= stable_checks:
                return True
        else:
            stable_count = 0
            last_size = size

        time.sleep(poll_interval)

    log.warning(f"  [WARN] {path.name} never stopped growing after {timeout}s — ingesting anyway")
    return True


# ─── Watchdog Event Handler ───────────────────────────────────────────────────

class FileHandler(FileSystemEventHandler):
    """
    Every handler method is wrapped in try/except — these run synchronously
    inside watchdog's own internal dispatch thread, which nothing else here
    supervises. Any exception that escapes a handler silently kills that
    thread: the process keeps running and looks healthy, but no further
    file events are ever delivered until a full restart.
    """

    def on_created(self, event):
        try:
            if event.is_directory:
                return
            path = Path(event.src_path)
            # Must be inside State/Property subfolder — skip files in state folder root
            if is_supported(path) and len(path.relative_to(WATCH_DIR).parts) >= 3:
                if _wait_until_settled(path):
                    ingest_file(path)
        except Exception as e:
            log.error(f"  [ERROR] on_created handler failed for {event.src_path}: {e}", exc_info=True)

    def on_moved(self, event):
        try:
            if event.is_directory:
                return
            path = Path(event.dest_path)
            if is_supported(path) and len(path.relative_to(WATCH_DIR).parts) >= 3:
                if _wait_until_settled(path):
                    ingest_file(path)
        except Exception as e:
            log.error(f"  [ERROR] on_moved handler failed for {event.dest_path}: {e}", exc_info=True)


# ─── Startup Processing ───────────────────────────────────────────────────────

def process_existing_files():
    """Process any files already sitting in State/Property subfolders."""
    existing = [
        f for f in WATCH_DIR.rglob("*")
        if f.is_file()
        and is_supported(f)
        and len(f.relative_to(WATCH_DIR).parts) >= 3  # must be in State/Property/file
    ]
    if existing:
        log.info(f"Found {len(existing)} existing file(s) — ingesting now...")
        for file_path in existing:
            ingest_file(file_path)
    else:
        log.info("Watched folder is empty — drop files into State/Property subfolders to ingest")


# ─── Watcher Entry Point ──────────────────────────────────────────────────────

def _start_observer() -> Observer:
    """
    Internal: set up folders, ingest existing files, start the observer.
    Returns the running Observer so the caller can manage its lifecycle.
    """
    _load_properties()
    create_property_folders()
    process_existing_files()

    observer = Observer()
    observer.schedule(FileHandler(), str(WATCH_DIR), recursive=True)
    observer.start()
    log.info("[ACTIVE] Watcher running — drop files into State/Property subfolders")
    log.info("[SUPPORTED] .pdf  .xlsx  .xls  .csv  .txt")
    log.info("[STRUCTURE] watched_folder / <State> / <Property Name> / file.pdf")
    return observer


def start_watcher():
    """
    Blocking mode — used by 'python main.py ingest'.
    Runs until Ctrl+C. Checks the observer's own internal thread is still
    alive every 30s and restarts it if it died silently (e.g. an
    unhandled exception in watchdog's dispatch thread) — without this, a
    dead observer looks identical to a healthy one from the outside.
    """
    observer = _start_observer()
    checks_since_restart = 0
    try:
        while True:
            time.sleep(2)
            checks_since_restart += 1
            if checks_since_restart >= 15:  # ~30s
                checks_since_restart = 0
                if not observer.is_alive():
                    log.error("[WATCHER] Observer thread died — restarting it now.")
                    observer = _start_observer()
    except KeyboardInterrupt:
        log.info("Shutting down watcher...")
        observer.stop()
    observer.join()


def start_watcher_background(supervise: bool = False):
    """
    Non-blocking mode. Returns the running Observer immediately.

    If supervise=True, also starts a lightweight daemon thread that checks
    every 30s whether the observer's internal thread is still alive and
    restarts it if not. Callers that don't hold onto and monitor the
    returned Observer themselves (e.g. mcp_server.py) should pass
    supervise=True — otherwise a silently-dead observer is never noticed.
    """
    observer = _start_observer()

    if supervise:
        import threading

        def _supervise():
            nonlocal observer
            while True:
                time.sleep(30)
                try:
                    if not observer.is_alive():
                        log.error("[WATCHER] Observer thread died — restarting it now.")
                        observer = _start_observer()
                except Exception as e:
                    log.warning(f"[WATCHER] Supervisor check failed (continuing): {e}")

        threading.Thread(target=_supervise, daemon=True).start()

    return observer
